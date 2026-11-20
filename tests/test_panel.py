"""Real HTTP integration tests; Docker mutations are mocked, never sent to a daemon."""

from http.client import HTTPConnection
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from files import FileStore, PanelError
from host import Host
from server import Auth, PanelServer


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "managed"
        self.files = FileStore(self.root)

    def test_create_read_update_and_external_conflict(self):
        created = self.files.write("note.txt", "hello\r\n世界", None)
        read = self.files.read("note.txt")
        self.assertEqual(read["content"], "hello\r\n世界")
        self.assertEqual(read["etag"], created["etag"])
        self.files.write("note.txt", "new", read["etag"])
        (self.root / "note.txt").write_text("changed outside the panel", encoding="utf-8")
        with self.assertRaises(PanelError) as context:
            self.files.write("note.txt", "overwrite", read["etag"])
        self.assertEqual(context.exception.status, 409)
        self.assertEqual((self.root / "note.txt").read_text(), "changed outside the panel")
        self.assertFalse(list(self.root.glob(".harbor-*")))

    def test_create_never_overwrites_existing_file(self):
        self.files.write("existing.txt", "original", None)
        with self.assertRaises(PanelError) as context:
            self.files.write("existing.txt", "replacement", None)
        self.assertEqual(context.exception.status, 409)
        self.assertEqual(self.files.read("existing.txt")["content"], "original")

    def test_paths_reject_traversal_absolute_devices_and_streams(self):
        for path in ("../outside", "a/../../outside", "/etc/passwd", "C:\\Windows", "a\\..\\outside", "file:stream", "CON", "NUL.txt", "file.", "file\x00name"):
            with self.subTest(path=path), self.assertRaises(PanelError):
                self.files.resolve(path)

    def test_symbolic_link_cannot_escape_root(self):
        outside = Path(self.directory.name) / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        link = self.root / "linked.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            with mock.patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaises(PanelError) as context:
                    self.files.read("linked.txt")
                self.assertEqual(context.exception.status, 403)
        else:
            with self.assertRaises(PanelError) as context:
                self.files.read("linked.txt")
            self.assertEqual(context.exception.status, 403)

    def test_binary_and_size_limits(self):
        (self.root / "binary.dat").write_bytes(b"\xff\x00")
        with self.assertRaises(PanelError) as context:
            self.files.read("binary.dat")
        self.assertEqual(context.exception.status, 415)
        with self.assertRaises(PanelError) as context:
            self.files.write("large.txt", "x" * (self.files.MAX_TEXT + 1), None)
        self.assertEqual(context.exception.status, 413)
        self.assertFalse((self.root / "large.txt").exists())

    def test_list_and_directory_creation(self):
        self.files.mkdir("docs")
        self.files.write("a.txt", "a", None)
        listing = self.files.listing()
        self.assertEqual(listing["path"], "")
        self.assertEqual([entry["name"] for entry in listing["entries"]], ["docs", "a.txt"])
        self.assertTrue(listing["entries"][0]["directory"])
        self.assertEqual(self.files.listing("docs")["entries"], [])


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        cls.server = PanelServer(("127.0.0.1", 0), "test-password-long-enough", root / "files", root / "state")
        cls.server.auth.username = "administrator"
        cls.server.auth.password_hash = cls.server.auth.legacy_password_hash
        cls.port = cls.server.server_address[1]
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.host.close()
        cls.server.server_close()
        cls.directory.cleanup()

    def setUp(self):
        with self.server.auth.lock:
            self.server.auth.attempts.clear()
            self.server.auth.sessions.clear()
        self.cookie = ""
        self.csrf = ""

    def request(self, method, path, body=None, headers=None, raw=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        request_headers = {"Origin": self.origin}
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        if self.csrf:
            request_headers["X-CSRF-Token"] = self.csrf
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        try:
            connection.request(method, path, body=raw, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            result_headers = dict(response.getheaders())
            if "application/json" in result_headers.get("Content-Type", ""):
                data = json.loads(data)
            return response.status, data, result_headers
        finally:
            connection.close()

    def login(self):
        status, data, headers = self.request("POST", "/api/login", {"username": "administrator", "password": "test-password-long-enough"})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = data["csrf"]
        return headers

    def test_unauthenticated_api_and_public_login_assets(self):
        self.assertEqual(self.request("GET", "/api/metrics")[0], 401)
        for path in ("/", "/app.js", "/style.css", "/favicon.svg"):
            with self.subTest(path=path):
                status, body, headers = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertGreater(len(body), 20)
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("GET", "/server.py")[0], 404)

    def test_session_cookie_logout_and_expiration(self):
        headers = self.login()
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        status, data, _ = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(data["csrf"], self.csrf)
        self.assertEqual(self.request("POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.request("GET", "/api/session")[0], 401)
        self.login()
        token = self.cookie.split("=", 1)[1]
        self.server.auth.sessions[token]["expires"] = time.monotonic() - 1
        self.assertEqual(self.request("GET", "/api/session")[0], 401)

    def test_brute_force_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.request("POST", "/api/login", {"username": "administrator", "password": "wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"username": "administrator", "password": "wrong"})[0], 429)

    def test_origin_host_and_csrf_are_required(self):
        self.assertEqual(self.request("POST", "/api/login", {"username": "administrator", "password": "wrong"}, {"Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.request("GET", "/", headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("GET", "/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})[0], 200)
        self.assertEqual(self.request("GET", "/api/session", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})[0], 403)
        self.login()
        self.assertEqual(self.request("POST", "/api/terminal", {"command": "help"}, {"X-CSRF-Token": ""})[0], 403)
        self.assertEqual(self.request("POST", "/api/terminal", {"command": "help"}, {"X-CSRF-Token": "é"})[0], 403)
        self.assertEqual(self.request("POST", "/api/terminal", {"command": "help"}, {"Origin": ""})[0], 403)

    def test_malformed_and_oversized_requests(self):
        self.assertEqual(self.request("POST", "/api/login", raw=b"{", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/login", raw=b"[]", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/login", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/login", raw=b"{}", headers={"Content-Type": "application/json", "Content-Length": "3000000"})[0], 413)

    def test_metrics_and_processes_are_real(self):
        self.login()
        status, metrics, _ = self.request("GET", "/api/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(metrics["hostname"], socket.gethostname())
        self.assertGreater(metrics["memory"]["total"], 0)
        self.assertGreaterEqual(metrics["cpu_percent"], 0)
        self.assertLessEqual(metrics["cpu_percent"], 100)
        self.assertTrue(metrics["history"])
        status, data, _ = self.request("GET", "/api/processes")
        self.assertEqual(status, 200)
        self.assertGreater(data["total"], 0)
        self.assertEqual(data["processes"], sorted(data["processes"], key=lambda item: item["memory"], reverse=True))

    def test_file_round_trip_and_audit_excludes_content(self):
        self.login()
        content = "sensitive-test-content-123"
        status, created, _ = self.request("PUT", "/api/files/write", {"path": "http-test.txt", "content": content, "etag": None})
        self.assertEqual(status, 200)
        self.assertEqual(created["etag"], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(self.request("GET", "/api/files/read?path=http-test.txt")[1]["content"], content)
        status, downloaded, headers = self.request("GET", "/api/files/download?path=http-test.txt")
        self.assertEqual(status, 200)
        self.assertEqual(downloaded.decode(), content)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.request("PUT", "/api/files/write", {"path": "http-test.txt", "content": "conflict", "etag": "stale"})[0], 409)
        _, audit, _ = self.request("GET", "/api/audit")
        self.assertTrue(any(event["action"] == "files.write" and event["success"] for event in audit["events"]))
        self.assertTrue(any(event["action"] == "files.write" and not event["success"] for event in audit["events"]))
        self.assertNotIn(content, json.dumps(audit))
        self.assertNotIn("test-password-long-enough", json.dumps(audit))

    def test_missing_etag_and_traversal_are_rejected(self):
        self.login()
        self.assertEqual(self.request("PUT", "/api/files/write", {"path": "invalid.txt", "content": "oops"})[0], 400)
        self.assertEqual(self.request("GET", "/api/files/read?path=..%2Fserver.py")[0], 403)
        self.assertEqual(self.request("POST", "/api/files/mkdir", {"path": "../escape"})[0], 403)

    def test_diagnostics_reject_shell_and_return_real_hostname(self):
        self.login()
        status, data, _ = self.request("POST", "/api/terminal", {"command": "hostname"})
        self.assertEqual(status, 200)
        self.assertEqual(data["output"], socket.gethostname())
        for command in ("hostname; whoami", "ps | sh", "rm -rf /", "$(whoami)", None):
            with self.subTest(command=command):
                self.assertEqual(self.request("POST", "/api/terminal", {"command": command})[0], 400)

    def test_docker_parsing_actions_validation_and_service_error(self):
        self.login()
        item = {"ID": "a" * 64, "Names": "example", "Image": "example:latest", "State": "running", "Status": "Up"}
        with mock.patch.object(self.server.host, "docker_command", return_value=json.dumps(item)) as docker:
            status, data, _ = self.request("GET", "/api/containers")
            self.assertEqual(status, 200)
            self.assertEqual(data["containers"][0]["name"], "example")
            status, _, _ = self.request("POST", "/api/containers/action", {"id": "a" * 64, "action": "restart"})
            self.assertEqual(status, 200)
            docker.assert_called_with("restart", "--time", "5", "a" * 64)
            calls = docker.call_count
            self.assertEqual(self.request("POST", "/api/containers/action", {"id": "--help", "action": "start"})[0], 400)
            self.assertEqual(self.request("POST", "/api/containers/action", {"id": "a" * 64, "action": "rm"})[0], 400)
            self.assertEqual(docker.call_count, calls)
        with mock.patch.object(self.server.host, "docker_command", side_effect=PanelError("Docker offline", 503)):
            status, data, _ = self.request("GET", "/api/containers")
            self.assertEqual(status, 503)
            self.assertEqual(data["error"], "Docker offline")


class ConfigurationTests(unittest.TestCase):
    def test_registration_consumes_token_and_persists_account(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_file = root / "bootstrap.token"
            token_file.write_text("Zmlyc3QtdXNlLXRva2Vu", encoding="ascii")
            server = PanelServer(("127.0.0.1", 0), None, root / "files", root / "state", bootstrap_token_file=token_file)
            try:
                auth = server.auth
                self.assertTrue(auth.setup_required())
                with self.assertRaises(PanelError):
                    auth.register("wrong", "admin", "long-enough-password", "long-enough-password", "127.0.0.1")
                with self.assertRaises(PanelError):
                    auth.register("Zmlyc3QtdXNlLXRva2Vu", "admin", "long-enough-password", "different-password", "127.0.0.1")
                auth.register("Zmlyc3QtdXNlLXRva2Vu", "admin", "long-enough-password", "long-enough-password", "127.0.0.1")
                self.assertFalse(auth.setup_required())
                self.assertFalse(token_file.exists())
                with self.assertRaises(PanelError):
                    auth.register("Zmlyc3QtdXNlLXRva2Vu", "admin", "long-enough-password", "long-enough-password", "127.0.0.1")
                restarted = Auth(state_dir=root / "state")
                self.assertEqual(restarted.username, "admin")
                restarted.login("admin", "long-enough-password", "127.0.0.1")
                with self.assertRaises(PanelError):
                    restarted.login("other", "long-enough-password", "127.0.0.1")
            finally:
                server.host.close()
                server.server_close()

    def test_short_password_rejected(self):
        with self.assertRaises(ValueError):
            Auth("short")

    def test_state_database_cannot_be_browsed_as_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                PanelServer(("127.0.0.1", 0), "long-enough-password", directory, Path(directory) / "state")

    def test_https_proxy_sets_secure_cookie_and_exact_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = PanelServer(("127.0.0.1", 0), "long-enough-password", root / "files", root / "state", "https://panel.example.com")
            try:
                self.assertTrue(server.secure_cookie)
                self.assertEqual(server.origins, {"https://panel.example.com"})
                self.assertEqual(server.allowed_hosts, {"panel.example.com"})
            finally:
                server.host.close()
                server.server_close()

    def test_docker_capture_is_bounded_without_daemon(self):
        host = Host.__new__(Host)
        with mock.patch("host.shutil.which", return_value=sys.executable):
            self.assertEqual(host.docker_command("-c", "print('ok')"), "ok")
            with self.assertRaises(PanelError) as context:
                host.docker_command("-c", "import sys; sys.stdout.write('x' * (2 * 1024 * 1024))")
            self.assertEqual(context.exception.status, 413)


if __name__ == "__main__":
    unittest.main()
