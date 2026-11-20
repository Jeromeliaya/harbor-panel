"""Harbor Panel: a single-host operations workspace. Python 3.10+."""

import argparse
import base64
from collections import defaultdict, deque
from contextlib import closing
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from files import FileStore, PanelError
from host import Host


BASE = Path(__file__).resolve().parent
LOG = logging.getLogger("harbor")


class Auth:
    TTL = 8 * 3600

    def __init__(self, password=None, bootstrap_token_file=None, state_dir=None):
        self.salt = secrets.token_bytes(32)
        self.password_hash = None
        self.legacy_password_hash = None
        self.username = None
        self.bootstrap_token_file = Path(bootstrap_token_file) if bootstrap_token_file else None
        self.bootstrap_token_hash = None
        self.bootstrap_salt = secrets.token_bytes(32)
        self.auth_path = Path(state_dir) / "auth.sqlite3" if state_dir else None
        if password is not None:
            if len(password) < 12:
                raise ValueError("HARBOR_PASSWORD must contain at least 12 characters.")
            self.legacy_password_hash = self._hash(password)
        if self.auth_path:
            self.auth_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.auth_path)) as connection, connection:
                connection.execute("CREATE TABLE IF NOT EXISTS administrator (id INTEGER PRIMARY KEY CHECK (id=1), username TEXT NOT NULL, salt BLOB NOT NULL, password_hash BLOB NOT NULL)")
                row = connection.execute("SELECT username, salt, password_hash FROM administrator WHERE id=1").fetchone()
            os.chmod(self.auth_path, 0o600)
            if row:
                self.username, self.salt, self.password_hash = row
            elif self.bootstrap_token_file and self.bootstrap_token_file.is_file():
                token = self.bootstrap_token_file.read_text(encoding="ascii").strip()
                self.bootstrap_token_hash = hashlib.sha256(token.encode("utf-8")).digest()
        self.sessions = {}
        self.attempts = defaultdict(deque)
        self.lock = threading.RLock()

    def _hash(self, password):
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), self.salt, 210000)

    def _create_session(self, address, username, password):
        now = time.monotonic()
        with self.lock:
            self.attempts = defaultdict(deque, {ip: deque(t for t in times if now - t < 60) for ip, times in self.attempts.items() if times and now - times[-1] < 60})
            attempts = self.attempts[address]
            if len(attempts) >= 5 or len(self.attempts) > 1024:
                raise PanelError("Too many login attempts. Try again in one minute.", 429)
            attempts.append(now)
        using_legacy = self.auth_path is None
        expected_hash = self.legacy_password_hash if using_legacy else self.password_hash
        password_hash = self._hash(password) if isinstance(password, str) and expected_hash is not None else None
        valid = password_hash is not None and hmac.compare_digest(password_hash, expected_hash)
        if self.auth_path:
            valid = valid and isinstance(username, str) and hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        if not valid:
            raise PanelError("Invalid administrator username or password.", 401)
        with self.lock:
            self.attempts.pop(address, None)
            self.sessions = {token: session for token, session in self.sessions.items() if session["expires"] > now}
            if len(self.sessions) >= 32:
                oldest = min(self.sessions, key=lambda token: self.sessions[token]["expires"])
                del self.sessions[oldest]
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[token] = {"csrf": csrf, "expires": now + self.TTL}
        return token, csrf

    def login(self, username, password, address):
        return self._create_session(address, username, password)

    def setup_required(self):
        return bool(self.auth_path and self.username is None)

    def register(self, credential, username, password, confirmation, address):
        with self.lock:
            if not self.setup_required():
                raise PanelError("Administrator registration is unavailable.", 409)
            if self.bootstrap_token_hash is None:
                raise PanelError("The one-time credential is unavailable. Contact the server administrator.", 503)
            if not isinstance(username, str) or not 3 <= len(username.strip()) <= 64 or any(ord(char) < 32 for char in username):
                raise PanelError("Username must contain 3 to 64 printable characters.", 400)
            username = username.strip()
            if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
                raise PanelError("Password must contain 12 to 1024 characters.", 400)
            if password != confirmation:
                raise PanelError("Passwords do not match.", 400)
            if not isinstance(credential, str) or not hmac.compare_digest(hashlib.sha256(credential.encode("utf-8")).digest(), self.bootstrap_token_hash):
                raise PanelError("Invalid one-time credential.", 401)
            salt = secrets.token_bytes(32)
            password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210000)
            try:
                with closing(sqlite3.connect(self.auth_path, timeout=5)) as connection, connection:
                    connection.execute("INSERT INTO administrator (id, username, salt, password_hash) VALUES (1, ?, ?, ?)", (username, salt, password_hash))
            except sqlite3.IntegrityError:
                raise PanelError("Administrator registration has already been completed.", 409)
            self.username, self.salt, self.password_hash = username, salt, password_hash
            self.bootstrap_token_hash = None
            if self.bootstrap_token_file:
                if self.bootstrap_token_file.exists():
                    self.bootstrap_token_file.unlink()
        return self._create_session(address, username, password)

    def session(self, token):
        with self.lock:
            session = self.sessions.get(token)
            if not session or session["expires"] <= time.monotonic():
                self.sessions.pop(token, None)
                raise PanelError("Please sign in to continue.", 401)
            return session.copy()

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)


class Audit:
    def __init__(self, directory):
        directory = Path(directory).resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "audit.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, address TEXT NOT NULL, success INTEGER NOT NULL)")
        os.chmod(self.path, 0o600)

    def add(self, action, target, address, success=True):
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.execute("INSERT INTO events (time,action,target,address,success) VALUES (?,?,?,?,?)", (time.time(), action, str(target)[:500], address, int(success)))
            connection.execute("DELETE FROM events WHERE id <= (SELECT COALESCE(MAX(id),0)-10000 FROM events)")

    def recent(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            return {"events": [dict(row) for row in connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")]}


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, password, root, state_dir, public_origin=None, bootstrap_token_file=None):
        self.files = FileStore(root)
        state = Path(state_dir).resolve()
        if state.is_relative_to(self.files.root):
            raise ValueError("The state directory must be outside the managed file root.")
        self.auth = Auth(password if not bootstrap_token_file else None, bootstrap_token_file, state)
        self.audit = Audit(state)
        self.workers = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)
        port = self.server_address[1]
        self.public_bind = self.server_address[0] not in ("127.0.0.1", "::1")
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        self.secure_cookie = False
        if public_origin:
            parsed = urlsplit(public_origin)
            if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
                self.server_close()
                raise ValueError("--public-origin must be an HTTPS origin, e.g. https://panel.example.com")
            self.origins = {public_origin.rstrip("/")}
            self.secure_cookie = True
        self.allowed_hosts = {urlsplit(origin).netloc.lower() for origin in self.origins}
        try:
            self.host = Host(self.files.root)
        except Exception:
            self.server_close()
            raise

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "HarborPanel/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, format, *args):
        LOG.info("%s %s %s", self.client_address[0], self.command, urlsplit(self.path).path)

    def _respond(self, status, data, content_type="application/json; charset=utf-8", headers=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
        if self.server.secure_cookie:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise PanelError("Transfer-Encoding is not supported.")
        if self.headers.get_content_type() != "application/json":
            raise PanelError("Expected application/json.", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise PanelError("Invalid Content-Length.") from exc
        if length < 0 or length > 2 * 1024 * 1024:
            raise PanelError("Request body exceeds 2 MiB.", 413)
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PanelError("Invalid JSON request.") from exc
        if not isinstance(value, dict):
            raise PanelError("Expected a JSON object.")
        return value

    def _token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        return cookie["harbor_session"].value if "harbor_session" in cookie else ""

    def _cookie(self, token, expired=False):
        value = f"harbor_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={0 if expired else Auth.TTL}"
        return value + ("; Secure" if self.server.secure_cookie else "")

    def do_GET(self):
        self._dispatch(False)

    def do_POST(self):
        self._dispatch(True)

    def do_PUT(self):
        self._dispatch(True)

    def _dispatch(self, mutation):
        try:
            host = self.headers.get("Host", "").lower()
            if host not in self.server.allowed_hosts:
                try:
                    ipaddress.ip_address(urlsplit(f"//{host}").hostname or "")
                except ValueError:
                    if not self.server.public_bind:
                        raise PanelError("Unrecognized Host header.", 403)
                    raise PanelError("Public access requires an IP address Host header or configured origin.", 403)
                if not self.server.public_bind:
                    raise PanelError("Unrecognized Host header.", 403)
            navigation = self.command == "GET" and self.headers.get("Sec-Fetch-Mode") == "navigate" and not urlsplit(self.path).path.startswith("/api/")
            if self.headers.get("Sec-Fetch-Site") == "cross-site" and not navigation:
                raise PanelError("Cross-site requests are not allowed.", 403)
            origin = self.headers.get("Origin")
            public_origin = f"http://{host}"
            if origin and origin not in self.server.origins and not (self.server.public_bind and origin == public_origin):
                raise PanelError("Origin is not allowed.", 403)
            if mutation and origin not in self.server.origins and not (self.server.public_bind and origin == public_origin):
                raise PanelError("An allowed Origin header is required.", 403)
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            relative = query.get("path", [""])[0]
            address = self.client_address[0]
            if path == "/api/setup" and self.command == "GET":
                self._respond(200, {"required": self.server.auth.setup_required()})
                return
            if path == "/api/login" and self.command == "POST":
                body = self._body()
                try:
                    token, csrf = self.server.auth.login(body.get("username"), body.get("password"), address)
                except PanelError:
                    self.server.audit.add("login", "administrator", address, False)
                    raise
                self.server.auth.logout(self._token())
                self.server.audit.add("login", "administrator", address)
                self._respond(200, {"csrf": csrf}, headers={"Set-Cookie": self._cookie(token)})
                return
            if path == "/api/register" and self.command == "POST":
                body = self._body()
                try:
                    token, csrf = self.server.auth.register(body.get("credential"), body.get("username"), body.get("password"), body.get("confirmation"), address)
                except PanelError:
                    self.server.audit.add("register", "administrator", address, False)
                    raise
                self.server.audit.add("register", body.get("username", "administrator"), address)
                self._respond(200, {"csrf": csrf}, headers={"Set-Cookie": self._cookie(token)})
                return
            if path.startswith("/api/"):
                token = self._token()
                session = self.server.auth.session(token)
                if mutation and not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode("utf-8"), session["csrf"].encode("ascii")):
                    raise PanelError("CSRF token is missing or invalid.", 403)
                if not mutation:
                    self._get_api(path, relative, query, session)
                else:
                    self._write_api(path, address, token)
                return
            if mutation:
                raise PanelError("Route not found.", 404)
            assets = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg"}
            if path not in assets:
                raise PanelError("Page not found.", 404)
            asset = BASE / "web" / assets[path]
            content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            self._respond(200, asset.read_bytes(), content_type + "; charset=utf-8")
        except PanelError as exc:
            error = {"error": str(exc)}
            if exc.code:
                error["code"] = exc.code
            self._respond(exc.status, error)
        except PermissionError:
            self._respond(403, {"error": "The panel process does not have permission for this operation."})
        except FileNotFoundError:
            self._respond(404, {"error": "The requested resource no longer exists."})
        except (BrokenPipeError, ConnectionResetError):
            LOG.warning("Client disconnected during response")
        except (OSError, sqlite3.Error):
            LOG.exception("Host operation failed")
            self._respond(500, {"error": "Host operation failed. Check the server log for details."})
        except Exception:
            LOG.exception("Unexpected request failure")
            self._respond(500, {"error": "Unexpected server error. Check the server log for details."})

    def _get_api(self, path, relative, query, session):
        if path == "/api/session":
            result = {"csrf": session["csrf"], "version": "0.1.0", "file_root": str(self.server.files.root), "platform": os.name, "commands": list(Host.COMMANDS)}
        elif path == "/api/metrics":
            result = self.server.host.metrics()
        elif path == "/api/processes":
            result = self.server.host.processes()
        elif path == "/api/containers":
            result = self.server.host.containers()
        elif path == "/api/containers/config":
            result = self.server.host.container_config(query.get("id", [""])[0])
        elif path == "/api/containers/logs":
            result = self.server.host.container_logs(query.get("id", [""])[0])
        elif path == "/api/files":
            result = self.server.files.listing(relative)
        elif path == "/api/files/read":
            result = self.server.files.read(relative)
        elif path == "/api/files/download":
            data = self.server.files.download(relative)
            self._respond(200, data, "application/octet-stream", {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(Path(relative).name, safe="")})
            return
        elif path == "/api/audit":
            result = self.server.audit.recent()
        else:
            raise PanelError("API route not found.", 404)
        self._respond(200, result)

    def _write_api(self, path, address, token):
        routes = {("POST", "/api/logout"), ("POST", "/api/terminal"), ("POST", "/api/containers/action"), ("PUT", "/api/containers/config"), ("PUT", "/api/files/write"), ("POST", "/api/files/mkdir")}
        if (self.command, path) not in routes:
            raise PanelError("API route or method not found.", 404)
        body = self._body()
        target = body.get("path", body.get("id", body.get("command", "administrator")))
        action = path.removeprefix("/api/").replace("/", ".")
        if path == "/api/containers/action":
            target = f"{body.get('action', '')}:{body.get('id', '')}"
        elif path == "/api/containers/config":
            target = body.get("id", "")
        # Record intent before changing resources, then record the result separately.
        self.server.audit.add(action + ".requested", target, address)
        try:
            if path == "/api/logout":
                self.server.auth.logout(token)
                result = {"ok": True}
            elif path == "/api/terminal":
                result = self.server.host.terminal(body.get("command"))
            elif path == "/api/containers/action":
                result = self.server.host.container_action(body.get("id"), body.get("action"))
            elif path == "/api/containers/config":
                if "id" not in body or "config" not in body:
                    raise PanelError("id and config are required.")
                result = self.server.host.update_container(body["id"], body["config"])
            elif path == "/api/files/write":
                if "path" not in body or "content" not in body or "etag" not in body:
                    raise PanelError("path, content and etag are required (etag=null creates a file).")
                result = self.server.files.write(body["path"], body["content"], body["etag"])
            else:
                result = self.server.files.mkdir(body.get("path"))
        except (PanelError, OSError):
            self.server.audit.add(action, target, address, False)
            raise
        self.server.audit.add(action, target, address)
        headers = {"Set-Cookie": self._cookie("", expired=True)} if path == "/api/logout" else None
        self._respond(200, result, headers=headers)


def main():
    parser = argparse.ArgumentParser(description="Harbor Panel - independent single-host operations workspace")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--root", type=Path, default=BASE / "workspace")
    parser.add_argument("--state-dir", type=Path, default=BASE / ".state")
    parser.add_argument("--public-origin", help="HTTPS origin when deployed behind a trusted reverse proxy")
    parser.add_argument("--bootstrap-token-file", type=Path, help="One-time administrator registration credential file")
    args = parser.parse_args()
    password = os.environ.get("HARBOR_PASSWORD")
    if not args.bootstrap_token_file and not password:
        token_file = Path(args.state_dir) / "bootstrap.token"
        if not token_file.exists():
            token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            credential = secrets.token_bytes(32)
            token_file.write_text(base64.b64encode(credential).decode("ascii"), encoding="ascii")
            os.chmod(token_file, 0o600)
        args.bootstrap_token_file = token_file
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        server = PanelServer((args.host, args.port), password, args.root, args.state_dir, args.public_origin, args.bootstrap_token_file)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = args.public_origin or f"http://{display_host}:{server.server_address[1]}"
    print(f"\n  HARBOR PANEL 0.1.0\n  URL: {url}\n  File root: {server.files.root}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping Harbor Panel.", flush=True)
    finally:
        server.host.close()
        server.server_close()


if __name__ == "__main__":
    main()
