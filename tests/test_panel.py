"""Real HTTP integration tests; Docker mutations are mocked, never sent to a daemon."""

from http.client import HTTPConnection
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from wsproto import WSConnection, ConnectionType
from wsproto.events import Request, AcceptConnection, TextMessage, CloseConnection


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

    def test_protected_state_is_hidden_and_inaccessible(self):
        state = self.root / "var" / "lib" / "harbor-panel"
        state.mkdir(parents=True)
        (state / "auth.sqlite3").write_text("private", encoding="utf-8")
        files = FileStore(self.root, protected_paths=(state,))
        self.assertEqual(files.listing("var/lib")["entries"], [])
        with self.assertRaises(PanelError) as context:
            files.read("var/lib/harbor-panel/auth.sqlite3")
        self.assertEqual(context.exception.status, 403)

    def test_full_host_links_stay_in_root_and_cannot_alias_protected_state(self):
        target = self.root / "ordinary.txt"
        target.write_text("visible", encoding="utf-8")
        state = self.root / "state"
        state.mkdir()
        (state / "auth.sqlite3").write_text("private", encoding="utf-8")
        try:
            (self.root / "ordinary-link").symlink_to(target)
            (self.root / "state-link").symlink_to(state, target_is_directory=True)
        except OSError:
            self.skipTest("Symbolic links are unavailable on this system.")

        files = FileStore(self.root, protected_paths=(state,), allow_symlinks=True)
        self.assertEqual(files.read("ordinary-link")["content"], "visible")
        self.assertNotIn("state-link", [entry["name"] for entry in files.listing()["entries"]])
        with self.assertRaises(PanelError) as context:
            files.read("state-link/auth.sqlite3")
        self.assertEqual(context.exception.status, 403)

    def test_updating_file_preserves_mode(self):
        if os.name == "nt":
            self.skipTest("POSIX permission bits are not reliable on Windows.")
        target = self.root / "config.txt"
        target.write_text("old", encoding="utf-8")
        target.chmod(0o640)
        current = self.files.read("config.txt")
        self.files.write("config.txt", "new", current["etag"])
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)


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

    def test_listener_requires_session_csrf_and_password(self):
        path = "/api/host-settings/change"
        body = {"kind": "panel", "port": 8089, "firewall_ready": True}
        self.assertEqual(self.request("GET", "/api/host-settings")[0], 401)
        self.assertEqual(self.request("POST", path, body)[0], 401)
        self.login()
        with mock.patch.object(self.server.host_settings, "start", return_value={"status": "queued"}) as start:
            self.assertEqual(self.request("POST", path, body, {"X-CSRF-Token": ""})[0], 403)
            self.assertEqual(self.request("POST", path, {**body, "password": "wrong"})[0], 403)
            start.assert_not_called()
            self.assertEqual(self.request("POST", path, {**body, "password": "test-password-long-enough"})[0], 202)
            start.assert_called_once_with("panel", 8089, firewall_ready=True)

    def test_https_totp_scoped_listener_verification_and_local_health(self):
        import pyotp
        from host_settings import HEALTH_PATH

        server = self.server
        origin = "https://panel.example.test"
        host = "panel.example.test"
        headers = {"Host": host, "Origin": origin, "X-Forwarded-Proto": "https"}
        guest = {"username": "administrator", "password": "test-password-long-enough"}
        change = {"kind": "panel", "port": 8089, "firewall_ready": True}
        with mock.patch.multiple(server, secure_cookie=True, public_origin=origin, rp_id=host,
                                 allowed_hosts={host}, origins={origin}):
            try:
                status, login, cookie = self.request("POST", "/api/login", guest, headers)
                self.assertEqual(status, 200)
                self.cookie, self.csrf = cookie["Set-Cookie"].split(";", 1)[0], login["csrf"]
                status, grant, _ = self.request("POST", "/api/mfa/reauth", guest, headers)
                self.assertEqual(status, 200)
                self.assertEqual(self.request("POST", "/api/host-settings/change", {**change, **grant}, headers)[0], 403)
                status, draft, _ = self.request("POST", "/api/mfa/enroll/start", {**grant, "method": "totp"}, headers)
                self.assertEqual(status, 200)
                totp = pyotp.TOTP(draft["secret"])
                with mock.patch("mfa.time.time", return_value=1_800_000_000):
                    status, result, _ = self.request("POST", "/api/mfa/enroll/finish", {"enrollment_id": draft["enrollment_id"], "code": totp.at(1_800_000_000)}, headers)
                    self.assertEqual((status, result), (200, {"signed_out": True}))
                self.assertEqual(self.request("GET", "/api/session", headers=headers)[0], 401)
                self.cookie = self.csrf = ""
                self.assertEqual(self.request("POST", "/api/login", guest, {**headers, "X-Forwarded-Proto": "http"})[0], 403)
                status, flow, issued = self.request("POST", "/api/login", guest, headers)
                self.assertEqual(status, 202)
                self.assertNotIn("Set-Cookie", issued)
                path = "/api/login/mfa/verify"
                factors = {"method": "totp", "challenge": flow["challenge"]}
                with mock.patch("mfa.time.time", return_value=1_800_000_060):
                    self.assertEqual(self.request("POST", path, {**factors, "code": totp.at(1_800_000_000)}, headers)[0], 403)
                    status, login, cookie = self.request("POST", path, {**factors, "code": totp.at(1_800_000_060)}, headers)
                    self.assertEqual(status, 200)
                self.cookie, self.csrf = cookie["Set-Cookie"].split(";", 1)[0], login["csrf"]
                status, flow, _ = self.request("POST", "/api/mfa/reauth", {**guest, "scope": "listener"}, headers)
                self.assertEqual(status, 200)
                self.assertNotIn("grant", flow)
                with mock.patch("mfa.time.time", return_value=1_800_000_120):
                    status, grant, _ = self.request("POST", "/api/mfa/reauth/verify", {"challenge": flow["challenge"], "method": "totp", "code": totp.at(1_800_000_120), "scope": "mfa"}, headers)
                self.assertEqual(status, 200)
                self.assertEqual(self.request("POST", "/api/mfa/remove", {**grant, "method": "totp"}, headers)[0], 403)
                with mock.patch.object(server.host_settings, "start", return_value={"status": "queued"}) as start:
                    self.assertEqual(self.request("POST", "/api/host-settings/change", {**change, **guest}, headers)[0], 410)
                    self.assertEqual(self.request("POST", "/api/host-settings/change", {**change, **grant}, headers)[0], 202)
                    self.assertEqual(self.request("POST", "/api/host-settings/change", {**change, **grant}, headers)[0], 410)
                    start.assert_called_once()
                status, events, _ = self.request("GET", "/api/audit", headers=headers)
                self.assertEqual(status, 200)
                serialized = json.dumps(events)
                self.assertIn("listener.change.requested", serialized)
                for secret in (guest["password"], draft["secret"], draft["enrollment_id"],
                               flow["challenge"], grant["grant"], totp.at(1_800_000_120)):
                    self.assertNotIn(secret, serialized)
                with mock.patch.object(server.host_settings, "health", return_value={"proof": "b" * 64}) as health:
                    path = f"{HEALTH_PATH}?nonce={'a' * 32}"
                    local = self.request("GET", path, headers={"Origin": ""})
                    proxied = self.request("GET", path, headers={**headers, "Origin": ""})
                    self.assertEqual((local[0], proxied[0]), (200, 200))
                    self.assertEqual(proxied[2]["Cache-Control"], "no-store")
                    self.assertEqual(self.request("GET", path, headers={**headers, "Origin": "", "X-Forwarded-Proto": "http"})[0], 404)
                    self.assertEqual(health.call_count, 2)
            finally:
                with server.auth.mfa._db(write=True) as db:
                    db.execute("DELETE FROM mfa_totp")
                server.auth.mfa.clear()

    def test_website_routes_require_login_and_csrf(self):
        for path in ("", "/environment", "/job", "/scan"):
            self.assertEqual(self.request("GET", "/api/sites" + path)[0], 401)
        self.login()
        status, body, headers = self.request("GET", "/api/sites")
        self.assertEqual((status, body), (200, {"supported": False, "sites": [], "job": None}))
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(self.request("GET", "/api/sites/scan")[0], 403)
        body = {"domain": "test.example.org", "kind": "proxy", "upstream": "127.0.0.1:3000"}
        self.assertEqual(self.request("POST", "/api/sites", body, headers={"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("POST", "/api/sites", body)[0], 403)
        job = {"job": {"id": "job-id", "status": "running"}}
        with mock.patch.object(self.server.sites, "create", return_value=job) as create:
            self.assertEqual(self.request("POST", "/api/sites", body)[:2], (202, job))
        create.assert_called_once_with(body["domain"], body["kind"], body["upstream"], "127.0.0.1")

    def test_appearance_routes_require_login_and_csrf(self):
        self.assertEqual(self.request("GET", "/api/appearance")[0], 401)
        self.login()
        self.assertEqual(self.request("POST", "/api/appearance", {"theme": "nocturne"}, headers={"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("POST", "/api/appearance", {"theme": "nocturne"})[1]["theme"], "nocturne")
        self.assertEqual(self.request("POST", "/api/appearance", {"theme": "invalid"})[0], 400)
        from test_terminal_appearance import image_bytes
        self.assertEqual(self.request("PUT", "/api/appearance/background", raw=image_bytes(), headers={"X-Image-Type": "image/png"})[0], 200)
        status, data, headers = self.request("GET", "/api/appearance/background")
        self.assertEqual(status, 200)
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(self.request("POST", "/api/appearance/background/remove", {})[0], 200)
        self.assertEqual(self.request("GET", "/api/appearance/background")[0], 404)

    def test_websocket_rejects_unauthenticated_or_invalid_csrf(self):
        upgrade = {"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Protocol": "wrong"}
        self.assertEqual(self.request("GET", "/api/terminal/ws", headers=upgrade)[0], 401)
        self.login()
        self.assertEqual(self.request("GET", "/api/terminal/ws", headers=upgrade)[0], 403)
        upgrade["Sec-WebSocket-Protocol"] = self.csrf
        upgrade["Origin"] = "https://untrusted.example"
        self.assertEqual(self.request("GET", "/api/terminal/ws", headers=upgrade)[0], 403)

    def test_websocket_fragmented_control_limit_and_logout(self):
        self.login()
        def open_socket():
            connection = socket.create_connection(("127.0.0.1", self.port), timeout=4)
            self.addCleanup(connection.close)
            protocol = WSConnection(ConnectionType.CLIENT)
            connection.sendall(protocol.send(Request(host=f"127.0.0.1:{self.port}", target="/api/terminal/ws", subprotocols=[self.csrf], extra_headers=[(b"origin", self.origin.encode()), (b"cookie", self.cookie.encode())])))
            events = []
            while not events:
                protocol.receive_data(connection.recv(65536))
                events.extend(protocol.events())
            self.assertIsInstance(events[0], AcceptConnection)
            return connection, protocol
        def receive(connection, protocol):
            events = []
            while not events:
                data = connection.recv(65536)
                protocol.receive_data(data or None)
                events.extend(protocol.events())
            return events
        connection, protocol = open_socket()
        connection.sendall(protocol.send(TextMessage(data='{"type":"connect","target":', message_finished=False)))
        connection.sendall(protocol.send(TextMessage(data='"remote","host":"example.com","password":"secret"}')))
        events = receive(connection, protocol)
        self.assertIn("HTTPS", json.loads(events[0].data)["message"])
        connection.sendall(protocol.send(TextMessage(data="x" * 10000, message_finished=False)))
        connection.sendall(protocol.send(TextMessage(data="x" * 10000)))
        events = receive(connection, protocol)
        self.assertTrue(any(isinstance(event, TextMessage) and "size limit" in event.data for event in events))
        connection, protocol = open_socket()
        with self.server.terminal.lock:
            session = next(item for item in self.server.terminal.sessions.values() if item.peer.connection.getpeername()[1] == connection.getsockname()[1])
        closed = threading.Event()
        channel, client = mock.Mock(), mock.Mock()
        channel.recv_ready.return_value = False
        channel.closed = False
        client.close.side_effect = closed.set
        with session.lock:
            session.channel, session.client = channel, client
        connection.sendall(protocol.send(CloseConnection(code=1000, reason="leaving terminal")))
        self.assertTrue(closed.wait(3), "Browser close must also close SSH")
        channel.close.assert_called_once()
        connection, protocol = open_socket()
        self.assertEqual(self.request("POST", "/api/logout", {})[0], 200)
        events = receive(connection, protocol)
        self.assertTrue(any(isinstance(event, CloseConnection) for event in events))

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

    def test_public_bind_accepts_matching_http_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = PanelServer(("0.0.0.0", 0), "long-enough-password", root / "files", root / "state")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                connection = HTTPConnection("127.0.0.1", port, timeout=10)
                connection.request(
                    "POST",
                    "/api/login",
                    json.dumps({"username": "administrator", "password": "wrong"}),
                    {"Content-Type": "application/json", "Host": f"198.51.100.10:{port}", "Origin": f"http://198.51.100.10:{port}"},
                )
                self.assertEqual(connection.getresponse().status, 401)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                server.host.close()

    def test_secure_public_origin_rejects_direct_http_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = PanelServer(("0.0.0.0", 0), "long-enough-password", root / "files", root / "state", "https://panel.example.com")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                connection = HTTPConnection("127.0.0.1", port, timeout=10)
                connection.request(
                    "POST",
                    "/api/login",
                    json.dumps({"username": "administrator", "password": "wrong"}),
                    {"Content-Type": "application/json", "Host": f"198.51.100.10:{port}", "Origin": f"http://198.51.100.10:{port}"},
                )
                self.assertEqual(connection.getresponse().status, 403)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                server.host.close()

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

    def test_full_host_shell_audit_redacts_commands_and_marks_nonzero_exit(self):
        self.login()
        original_full_host = self.server.full_host
        original_host_full_host = self.server.host.full_host
        self.server.full_host = True
        self.server.host.full_host = True
        command = "secret-command-argument"
        try:
            with mock.patch.object(self.server.host, "terminal", return_value={"command": command, "output": "failed", "exit_code": 7}):
                status, result, _ = self.request("POST", "/api/terminal", {"command": command})
            self.assertEqual(status, 200)
            self.assertEqual(result["exit_code"], 7)
            _, audit, _ = self.request("GET", "/api/audit")
            shell_events = [event for event in audit["events"] if event["action"] == "terminal" and event["target"] == "host shell command"]
            self.assertEqual(len(shell_events), 1)
            self.assertFalse(shell_events[0]["success"])
            self.assertEqual(shell_events[0]["target"], "host shell command")
            self.assertNotIn(command, json.dumps(audit))
        finally:
            self.server.full_host = original_full_host
            self.server.host.full_host = original_host_full_host

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
            self.assertEqual(data, {"error": "Docker offline"})

    def test_container_socket_permission_error(self):
        self.login()
        error = PanelError("The panel service cannot access the Docker socket.", 503, code="docker_socket_permission")
        with mock.patch.object(self.server.host, "docker_command", side_effect=error):
            status, data, _ = self.request("GET", "/api/containers")
        self.assertEqual(status, 503)
        self.assertEqual(data, {"error": str(error), "code": "docker_socket_permission"})


class ConfigurationTests(unittest.TestCase):
    def test_full_host_mode_allows_state_inside_root_but_hides_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "var" / "lib" / "harbor-panel"
            state.mkdir(parents=True)
            server = PanelServer(("127.0.0.1", 0), "long-enough-password", root, state, full_host=True)
            try:
                self.assertTrue(server.full_host)
                self.assertEqual(server.files.listing("var/lib")["entries"], [])
                with self.assertRaises(PanelError):
                    server.files.listing("var/lib/harbor-panel")
            finally:
                server.host.close()
                server.server_close()

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
                self.assertIsNone(PanelServer.prepare_bootstrap_token(root / "state"))
                restarted.login("admin", "long-enough-password", "127.0.0.1")
                with self.assertRaises(PanelError):
                    restarted.login("other", "long-enough-password", "127.0.0.1")
                restarted_server = PanelServer(
                    ("127.0.0.1", 0), None, root / "files", root / "state",
                    bootstrap_token_file=token_file, full_host=True,
                )
                try:
                    self.assertFalse(restarted_server.auth.setup_required())
                finally:
                    restarted_server.host.close()
                    restarted_server.server_close()
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

    def test_full_host_terminal_runs_bounded_shell_command(self):
        host = Host.__new__(Host)
        host.full_host = True
        process = mock.Mock()
        process.stdout = BytesIO(b"root\n")
        process.wait.return_value = 0
        process.returncode = 0
        with mock.patch("host.subprocess.Popen", return_value=process) as popen:
            result = host.terminal("whoami")
        self.assertEqual(result, {"command": "whoami", "output": "root\n", "exit_code": 0})
        arguments = popen.call_args.args[0]
        if sys.platform == "win32":
            self.assertEqual(arguments[1:3], ["/d", "/s"])
        else:
            self.assertEqual(arguments[:2], ["/bin/sh", "-c"])
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)

    def test_full_host_terminal_reports_nonzero_exit_code(self):
        host = Host.__new__(Host)
        host.full_host = True
        process = mock.Mock()
        process.pid = 123
        process.stdout = BytesIO(b"failed\n")
        process.wait.return_value = 0
        process.returncode = 7
        with mock.patch("host.subprocess.Popen", return_value=process):
            result = host.terminal("false")
        self.assertEqual(result["output"], "failed\n")
        self.assertEqual(result["exit_code"], 7)

    def test_full_host_terminal_enforces_output_limit(self):
        host = Host.__new__(Host)
        host.full_host = True
        host.MAX_COMMAND_OUTPUT = 4
        process = mock.Mock()
        process.pid = 123
        process.stdout = BytesIO(b"too much output")
        process.wait.return_value = 0
        process.returncode = 0
        with mock.patch("host.subprocess.Popen", return_value=process), mock.patch("host.subprocess.run"), mock.patch("host.os.killpg", create=True):
            with self.assertRaises(PanelError) as context:
                host.terminal("generate output")
        self.assertEqual(context.exception.status, 413)

    def test_full_host_terminal_reports_output_reader_errors(self):
        host = Host.__new__(Host)
        host.full_host = True
        stream = mock.Mock()
        stream.__enter__ = mock.Mock(return_value=stream)
        stream.__exit__ = mock.Mock(return_value=False)
        stream.read.side_effect = OSError("stream failed")
        process = mock.Mock()
        process.pid = 123
        process.stdout = stream
        process.wait.return_value = 0
        process.returncode = 0
        with mock.patch("host.subprocess.Popen", return_value=process):
            with self.assertRaises(PanelError) as context:
                host.terminal("whoami")
        self.assertEqual(context.exception.status, 502)
        self.assertIn("stream failed", str(context.exception))

    def test_full_host_terminal_rejects_empty_command(self):
        host = Host.__new__(Host)
        host.full_host = True
        with self.assertRaises(PanelError):
            host.terminal("  ")

    @unittest.skipIf(os.name == "nt", "The native VPS service uses POSIX shell process groups.")
    def test_full_host_shell_executes_commands_and_enforces_limits(self):
        host = Host.__new__(Host)
        host.MAX_COMMAND_OUTPUT = 1024
        host.COMMAND_TIMEOUT = 0.1
        success = host.run_command("printf 'hello'")
        self.assertEqual((success["output"], success["exit_code"]), ("hello", 0))
        failure = host.run_command("printf 'failed'; exit 7")
        self.assertEqual((failure["output"], failure["exit_code"]), ("failed", 7))
        host.MAX_COMMAND_OUTPUT = 4
        with self.assertRaises(PanelError) as context:
            host.run_command("printf 12345")
        self.assertEqual(context.exception.status, 413)
        with self.assertRaises(PanelError) as context:
            host.run_command("sleep 2")
        self.assertEqual(context.exception.status, 504)

    def test_docker_cli_failure_diagnostics(self):
        host = Host.__new__(Host)
        executable = sys.executable
        with mock.patch("host.shutil.which", return_value=None):
            with self.assertRaises(PanelError) as context:
                host.docker_command("ps")
            self.assertEqual((context.exception.status, context.exception.code), (503, "docker_cli_missing"))

        cases = (
            ("permission denied while trying to connect to the docker API at unix:///var/run/docker.sock", 503, "docker_socket_permission"),
            ("dial unix /var/run/docker.sock: connect: connection refused", 503, "docker_daemon_unavailable"),
            ("Cannot connect to the Docker daemon. Is the docker daemon running?", 503, "docker_daemon_unavailable"),
            ("Error response from daemon: permission denied opening container log file", 502, None),
        )
        with mock.patch("host.shutil.which", return_value=executable):
            for diagnostic, status, code in cases:
                with self.subTest(diagnostic=diagnostic):
                    with self.assertRaises(PanelError) as context:
                        host.docker_command("-c", f"raise SystemExit({diagnostic!r})")
                    self.assertEqual((context.exception.status, context.exception.code), (status, code))
                    if code is None:
                        self.assertEqual(str(context.exception), diagnostic)

    def test_container_replacement_recovers_when_docker_returns_empty_output(self):
        host = Host.__new__(Host)
        calls = []
        responses = iter((
            '[{"Name":"/web","State":{"Running":true}}]',
            "",
            "",
            "",
            "",
            "",
        ))

        def docker_command(*arguments):
            calls.append(arguments)
            return next(responses)

        config = {"name": "web", "image": "example:latest"}
        with mock.patch.object(host, "docker_command", side_effect=docker_command):
            with self.assertRaises(PanelError) as context:
                host.update_container("a" * 64, config)
        self.assertEqual(str(context.exception), "Docker returned an invalid replacement container ID.")
        self.assertEqual(calls[-2:], [("rename", "harbor-backup-aaaaaaaaaaaa", "web"), ("start", "aaaaaaaa" * 8)])

    def test_container_replacement_reports_recovery_failure(self):
        host = Host.__new__(Host)
        responses = iter(('[{"Name":"/web","State":{"Running":false}}]', "", "", ""))

        def docker_command(*arguments):
            if arguments == ("rename", "harbor-backup-aaaaaaaaaaaa", "web"):
                raise PanelError("rename failed", 502)
            return next(responses)

        with mock.patch.object(host, "docker_command", side_effect=docker_command):
            with self.assertRaises(PanelError) as context:
                host.update_container("a" * 64, {"name": "web", "image": "example:latest"})
        self.assertIn("Automatic recovery also failed: rename failed", str(context.exception))

    def test_container_replacement_reports_backup_cleanup_failure(self):
        host = Host.__new__(Host)
        responses = iter(('[{"Name":"/web","State":{"Running":false}}]', "", "b" * 64))

        def docker_command(*arguments):
            if arguments == ("rm", "-f", "harbor-backup-aaaaaaaaaaaa"):
                raise PanelError("backup is busy", 502)
            return next(responses)

        with mock.patch.object(host, "docker_command", side_effect=docker_command):
            with self.assertRaises(PanelError) as context:
                host.update_container("a" * 64, {"name": "web", "image": "example:latest"})
        self.assertIn("New container " + "b" * 64 + " is running", str(context.exception))
        self.assertIn("backup is busy", str(context.exception))


if __name__ == "__main__":
    unittest.main()
