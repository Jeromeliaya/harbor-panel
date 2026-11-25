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
from appearance import AppearanceStore
from terminal import TerminalManager
from sites import SiteManager
from host_settings import HEALTH_PATH, HostOperations, HostSettings
from mfa import MFA, webauthn_user_handle


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
        self.mfa = MFA(self.auth_path)

    def _hash(self, password):
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), self.salt, 210000)

    def _check_password(self, address, username, password, *, reauth=False):
        now = time.monotonic()
        with self.lock:
            self.attempts = defaultdict(deque, {ip: deque(t for t in times if now - t < 60) for ip, times in self.attempts.items() if times and now - times[-1] < 60})
            if address not in self.attempts and len(self.attempts) >= 1024:
                raise PanelError("Too many login attempts. Try again in one minute.", 429)
            attempts = self.attempts[address]
            if len(attempts) >= 5:
                raise PanelError("Too many login attempts. Try again in one minute.", 429)
            attempts.append(now)
            expected_hash = self.password_hash if self.auth_path else self.legacy_password_hash
            salt, expected_user = self.salt, self.username
        try:
            password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210000) if isinstance(password, str) and len(password) <= 1024 and expected_hash is not None else None
            valid = password_hash is not None and hmac.compare_digest(password_hash, expected_hash)
            if self.auth_path and not reauth:
                valid = valid and isinstance(username, str) and expected_user is not None and hmac.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8"))
        except UnicodeError:
            valid = False
        if not valid:
            raise PanelError("Invalid administrator password." if reauth else "Invalid administrator username or password.", 403 if reauth else 401)
        with self.lock:
            self.attempts.pop(address, None)

    def _issue_session(self, *, mfa=False):
        now = time.monotonic()
        self.sessions = {token: session for token, session in self.sessions.items() if session["expires"] > now}
        if len(self.sessions) >= 32:
            self.logout(min(self.sessions, key=lambda token: self.sessions[token]["expires"]))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions[token] = {"csrf": csrf, "expires": now + self.TTL, "mfa": mfa}
        return token, csrf

    @staticmethod
    def _require_mfa_origin(origin):
        if not origin:
            raise PanelError("Second factors require a verified same-origin panel address.", 403)

    def _verification_start(self, purpose, owner, origin, rp_id, *, scope="mfa"):
        self._require_mfa_origin(origin)
        methods = self.mfa.status(rp_id)["methods"]
        if not methods:
            raise PanelError("No enrolled second factor is usable here. Use the Passkey enrollment address or a configured authenticator.", 403)
        return {"challenge": self.mfa.begin(purpose, owner, origin, scope=scope), "methods": methods}

    def login(self, username, password, address, origin=None, rp_id=None):
        self._check_password(address, username, password)
        with self.lock:
            if self.mfa.enabled():
                return self._verification_start("login", address, origin, rp_id)
            return self._issue_session()

    def mfa_login(self, action, address, origin, rp_id, body):
        self._require_mfa_origin(origin)
        with self.lock:
            challenge = body.get("challenge")
            if action == "options":
                return self.mfa.assertion_options(challenge, "login", address, origin, rp_id)
            self.mfa.verify(challenge, "login", address, origin, rp_id, webauthn_user_handle(self.salt), body.get("method"), body)
            return self._issue_session(mfa=True)

    def mfa_status(self, token, origin, rp_id):
        with self.lock:
            self.session(token)
            status = self.mfa.status(rp_id)
            status.pop("methods")
            return {**status, "available": bool(origin and self.username), "passkey_available": bool(origin and rp_id and self.username)}

    def mfa_settings(self, action, token, address, origin, rp_id, body):
        self._require_mfa_origin(origin)
        if action == "reauth":
            self._check_password(address, None, body.get("password"), reauth=True)
        with self.lock:
            self.session(token)
            if not self.username:
                raise PanelError("MFA requires a registered administrator.", 403)
            user_handle = webauthn_user_handle(self.salt)
            if action == "reauth":
                self.mfa.clear_owner(token)
                scope = body.get("scope", "mfa")
                if scope not in ("mfa", "listener"):
                    raise PanelError("Unknown verification operation.")
                if self.mfa.enabled():
                    return self._verification_start("reauth", token, origin, rp_id, scope=scope), None
                return {"grant": self.mfa.begin("grant", token, origin, scope=scope)}, None
            if action == "reauth/options":
                return self.mfa.assertion_options(body.get("challenge"), "reauth", token, origin, rp_id), None
            if action == "reauth/verify":
                scope = self.mfa.verify(body.get("challenge"), "reauth", token, origin, rp_id, user_handle, body.get("method"), body)
                return {"grant": self.mfa.begin("grant", token, origin, scope=scope)}, None
            if action == "enroll/start":
                return self.mfa.start_enrollment(body.get("grant"), token, origin, rp_id, self.username, user_handle, body.get("method")), None
            if action == "enroll/finish":
                self.mfa.finish_enrollment(body.get("enrollment_id"), token, origin, rp_id, body)
            elif action == "remove":
                self.mfa.remove(body.get("grant"), token, origin, body.get("method"), body.get("id"))
            else:
                raise PanelError("API route not found.", 404)
            revoked = tuple(self.sessions)
            self.sessions.clear()
            self.mfa.clear()
            return {"signed_out": True}, revoked

    def authorize_listener_change(self, token, address, origin, body):
        if origin and ("password" not in body or body.get("grant")):
            with self.lock:
                self.session(token)
                self.mfa.take_grant(body.get("grant"), token, origin, "listener")
            return
        with self.lock:
            self.session(token)
            if self.mfa.enabled():
                raise PanelError("Verify the enrolled second factor before changing listeners.", 403)
            self._check_password(address, None, body.get("password"), reauth=True)

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
            if self.mfa.enabled():
                raise PanelError("Existing second factors must be recovered by the host administrator.", 403)
            return self._issue_session()

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

    def has_session(self, token):
        with self.lock:
            session = self.sessions.get(token)
            if not session or session["expires"] <= time.monotonic():
                self.sessions.pop(token, None)
                return False
            return True


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

    def __init__(self, address, password, root, state_dir, public_origin=None, bootstrap_token_file=None, full_host=False):
        state = Path(state_dir).resolve()
        file_root = Path(root).resolve()
        bootstrap = Path(bootstrap_token_file).resolve() if bootstrap_token_file else None
        if state.is_relative_to(file_root) and not full_host:
            raise ValueError("The state directory must be outside the managed file root.")
        protected_paths = (state, bootstrap) if full_host and bootstrap else ((state,) if full_host else ())
        self.files = FileStore(file_root, protected_paths=protected_paths, allow_symlinks=full_host)
        self.state_dir = state
        self.full_host = full_host
        self.full_host_root = full_host and hasattr(os, "geteuid") and os.geteuid() == 0
        self.auth = Auth(password if not bootstrap_token_file else None, bootstrap_token_file, state)
        self.audit = Audit(state)
        self.appearance = AppearanceStore(state)
        self.terminal = TerminalManager(state, native_root=self.full_host_root, audit=self.audit)
        self.workers = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)
        port = self.server_address[1]
        self.public_bind = self.server_address[0] not in ("127.0.0.1", "::1")
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        self.secure_cookie = False
        self.public_origin = None
        self.rp_id = None
        if public_origin:
            try:
                parsed = urlsplit(public_origin)
                if (parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/")
                        or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None):
                    raise ValueError
                hostname = parsed.hostname.encode("idna").decode("ascii")
                origin_port = parsed.port
                authority = f"[{hostname}]" if ":" in hostname else hostname
                if origin_port is not None and origin_port != 443:
                    authority += f":{origin_port}"
                self.public_origin = f"https://{authority}"
                try:
                    ipaddress.ip_address(hostname)
                except ValueError:
                    self.rp_id = hostname
            except (ValueError, UnicodeError):
                self.server_close()
                raise ValueError("--public-origin must be an HTTPS origin, e.g. https://panel.example.com") from None
            self.origins = {self.public_origin}
            self.secure_cookie = True
        self.allowed_hosts = {urlsplit(origin).netloc.lower() for origin in self.origins}
        try:
            from panel_proxy import PanelProxy
            self.managed_tls_domains = set()
            self.managed_origins = set()
            self.domain_lock = threading.Lock()
            self.panel_http_domain = None
            self.panel_proxy = PanelProxy(state, port, self.full_host_root and not self.public_origin and HostOperations().available(),
                                          changed=self._panel_domain_changed)
            self._panel_domain_changed()
            self.sites = SiteManager(state, self.full_host_root, self.audit, port,
                                     {urlsplit(origin).hostname for origin in self.origins}, self.secure_cookie,
                                     panel_proxy=self.panel_proxy)
            self.host = Host(self.files.root, full_host=full_host)
            self.host_settings = HostSettings(state, full_host=self.full_host_root, panel_port=port,
                                              public_origin=self.public_origin, panel_host=self.server_address[0])
        except Exception:
            self.server_close()
            raise

    def _panel_domain_changed(self):
        if self.panel_http_domain:
            self.allowed_hosts.discard(self.panel_http_domain)
            self.origins.discard(f"http://{self.panel_http_domain}")
            if hasattr(self, "sites"):
                self.sites.panel_hosts.discard(self.panel_http_domain)
        site = self.panel_proxy.site
        self.panel_http_domain = site["domain"] if site and site["enabled"] else None
        if self.panel_http_domain:
            self.allowed_hosts.add(self.panel_http_domain)
            self.origins.add(f"http://{self.panel_http_domain}")
            if hasattr(self, "sites"):
                self.sites.panel_hosts.add(self.panel_http_domain)

    def refresh_domains(self):
        if not self.full_host_root:
            return
        from panel_domains import load
        with self.domain_lock:
            previous = self.panel_proxy.site
            self.panel_proxy.reload()
            if previous != self.panel_proxy.site:
                self._panel_domain_changed()
            domains = {item["domain"] for item in load(self.state_dir) if item["enabled"]}
            for origin in self.managed_origins:
                self.origins.discard(origin)
                self.allowed_hosts.discard(urlsplit(origin).netloc)
                self.sites.panel_hosts.discard(urlsplit(origin).hostname)
            self.managed_tls_domains = domains
            self.managed_origins = {f"https://{domain}" for domain in domains}
            self.origins.update(self.managed_origins)
            self.allowed_hosts.update(domains)
            self.sites.panel_hosts.update(domains)

    def server_close(self):
        if hasattr(self, "terminal"):
            self.terminal.close_all()
        super().server_close()

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

    @staticmethod
    def has_registered_administrator(state_dir):
        auth_path = Path(state_dir) / "auth.sqlite3"
        if not auth_path.is_file():
            return False
        with closing(sqlite3.connect(auth_path)) as connection:
            table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='administrator'").fetchone()
            if not table:
                return False
            return connection.execute("SELECT 1 FROM administrator WHERE id=1").fetchone() is not None

    @classmethod
    def prepare_bootstrap_token(cls, state_dir):
        if cls.has_registered_administrator(state_dir):
            return None
        token_file = Path(state_dir) / "bootstrap.token"
        token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(base64.b64encode(secrets.token_bytes(32)).decode("ascii"))
        os.chmod(token_file, 0o600)
        return token_file


class Handler(BaseHTTPRequestHandler):
    server_version = "HarborPanel/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

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
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
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
        return value + ("; Secure" if self._secure_transport() else "")

    def _secure_transport(self):
        return self.server.secure_cookie or bool(
            self.headers.get("Host", "").lower() in self.server.managed_tls_domains
            and ipaddress.ip_address(self.client_address[0]).is_loopback
            and self.headers.get_all("X-Forwarded-Proto") == ["https"])

    def _mfa_origin(self, mutation):
        origin = self.server.public_origin
        host = self.headers.get("Host", "").lower()
        if host in self.server.managed_tls_domains:
            origin = f"https://{host}"
            if not self._secure_transport():
                return None
            return origin if not mutation or self.headers.get_all("Origin") == [origin] else None
        if not origin:
            origin = f"http://{host}"
            if mutation and self.headers.get_all("Origin") != [origin]:
                return None
            return origin
        if not origin or self.headers.get("Host", "").lower() not in self.server.allowed_hosts:
            return None
        if mutation and self.headers.get_all("Origin") != [origin]:
            return None
        # TLS terminates at a same-host reverse proxy. It must overwrite this header.
        if not ipaddress.ip_address(self.client_address[0]).is_loopback:
            return None
        return origin if self.headers.get_all("X-Forwarded-Proto") == ["https"] else None

    def _rp_id(self, origin):
        if not origin:
            return None
        parsed = urlsplit(origin)
        if parsed.scheme == "http":
            return "localhost" if parsed.hostname == "localhost" else None
        return parsed.hostname if parsed.hostname in self.server.managed_tls_domains else self.server.rp_id

    def _respond_login(self, result, address):
        previous_token = self._token()
        self.server.auth.logout(previous_token)
        self.server.terminal.close_owner(previous_token)
        if isinstance(result, dict):
            headers = {"Set-Cookie": self._cookie("", expired=True)} if previous_token else None
            self._respond(202, result, headers=headers)
            return
        token, csrf = result
        self.server.audit.add("login", "administrator", address)
        self._respond(200, {"csrf": csrf}, headers={"Set-Cookie": self._cookie(token)})

    def do_GET(self):
        self._dispatch(False)

    def do_POST(self):
        self._dispatch(True)

    def do_PUT(self):
        self._dispatch(True)

    def _dispatch(self, mutation):
        try:
            self.server.refresh_domains()
            host = self.headers.get("Host", "").lower()
            parsed = urlsplit(self.path)
            if host in self.server.managed_tls_domains and not self._secure_transport():
                raise PanelError("Use the managed HTTPS domain through its local Nginx proxy.", 403)
            local_health = (self.command == "GET" and parsed.path == HEALTH_PATH
                            and host == f"127.0.0.1:{self.server.server_port}"
                            and ipaddress.ip_address(self.client_address[0]).is_loopback)
            if not local_health and host not in self.server.allowed_hosts:
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
            if host in self.server.managed_tls_domains and (origin or mutation) and origin != f"https://{host}":
                raise PanelError("An exact HTTPS domain Origin is required.", 403)
            public_origin = f"http://{host}"
            if origin and origin not in self.server.origins and not (self.server.public_bind and not self.server.secure_cookie and origin == public_origin):
                raise PanelError("Origin is not allowed.", 403)
            if mutation and origin not in self.server.origins and not (self.server.public_bind and not self.server.secure_cookie and origin == public_origin):
                raise PanelError("An allowed Origin header is required.", 403)
            path = parsed.path
            query = parse_qs(parsed.query)
            relative = query.get("path", [""])[0]
            address = self.client_address[0]
            if path == HEALTH_PATH and self.command == "GET":
                nonce = query.get("nonce", [])
                if self.server.full_host_root and (local_health or (host in self.server.managed_tls_domains and self._secure_transport())) and len(nonce) == 1:
                    from panel_domains import health
                    proof = health(self.server.state_dir, nonce[0])
                    if proof:
                        self._respond(200, proof)
                        return
                managed_probe = (host == self.server.panel_http_domain
                                 and ipaddress.ip_address(self.client_address[0]).is_loopback)
                allowed_probe = (local_health or (self._mfa_origin(False) if self.server.public_origin else False)
                                 or managed_probe or (host in self.server.managed_tls_domains and self._secure_transport()))
                pending = self.server.panel_proxy.probe
                if managed_probe and pending and len(nonce) == 1 and hmac.compare_digest(nonce[0], pending["nonce"]):
                    proof = {"proof": pending["proof"]}
                else:
                    proof = self.server.host_settings.health(nonce[0]) if allowed_probe and len(nonce) == 1 else None
                self._respond(200 if proof else 404, proof or {"error": "Not found."})
                return
            if path == "/api/setup" and self.command == "GET":
                self._respond(200, {"required": self.server.auth.setup_required(), "full_host": self.server.full_host, "full_host_root": self.server.full_host_root, "public_http": self.server.public_bind and not self._secure_transport()})
                return
            mfa_origin = self._mfa_origin(mutation)
            if self.command == "POST" and path in ("/api/login", "/api/login/mfa/options", "/api/login/mfa/verify"):
                body = self._body()
                try:
                    if path == "/api/login":
                        result = self.server.auth.login(body.get("username"), body.get("password"), address, mfa_origin, self._rp_id(mfa_origin))
                    else:
                        result = self.server.auth.mfa_login(path.rsplit("/", 1)[1], address, mfa_origin, self._rp_id(mfa_origin), body)
                except PanelError:
                    if not path.endswith("/options"):
                        self.server.audit.add("login", "administrator", address, False)
                    raise
                if path.endswith("/options"):
                    self._respond(200, result)
                else:
                    self._respond_login(result, address)
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
                if session["mfa"]:
                    self.server.auth._require_mfa_origin(mfa_origin)
                if mutation and not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode("utf-8"), session["csrf"].encode("ascii")):
                    raise PanelError("CSRF token is missing or invalid.", 403)
                if path == "/api/mfa" and self.command == "GET":
                    self._respond(200, self.server.auth.mfa_status(token, mfa_origin, self._rp_id(mfa_origin)))
                    return
                if path.startswith("/api/mfa/") and self.command == "POST":
                    self._write_mfa(path.removeprefix("/api/mfa/"), token, address, mfa_origin)
                    return
                if path == "/api/terminal/ws" and self.command == "GET":
                    self.server.terminal.serve(self, token, session, self._secure_transport())
                    return
                if not mutation:
                    self._get_api(path, relative, query, session)
                else:
                    self._write_api(path, address, token)
                return
            if mutation:
                raise PanelError("Route not found.", 404)
            assets = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/security-settings.js": "security-settings.js", "/host-settings.js": "host-settings.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg", "/vendor/xterm.js": "vendor/xterm.js", "/vendor/xterm.css": "vendor/xterm.css", "/vendor/xterm-addon-fit.js": "vendor/xterm-addon-fit.js"}
            if path not in assets:
                raise PanelError("Page not found.", 404)
            asset = BASE / "web" / assets[path]
            content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            if content_type.startswith(("text/", "application/javascript")):
                content_type += "; charset=utf-8"
            self._respond(200, asset.read_bytes(), content_type)
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

    def _write_mfa(self, action, token, address, origin):
        if action not in {"reauth", "reauth/options", "reauth/verify", "enroll/start", "enroll/finish", "remove"}:
            raise PanelError("API route not found.", 404)
        body = self._body()
        audit_action = "mfa." + action.replace("/", ".")
        try:
            result, revoked = self.server.auth.mfa_settings(action, token, address, origin, self._rp_id(origin), body)
        except PanelError:
            self.server.audit.add(audit_action, "administrator", address, False)
            raise
        if revoked is not None:
            for old_token in revoked:
                self.server.terminal.close_owner(old_token)
        self.server.audit.add(audit_action, "administrator", address)
        headers = {"Set-Cookie": self._cookie("", expired=True)} if revoked is not None else None
        self._respond(200, result, headers=headers)

    def _get_api(self, path, relative, query, session):
        if path == "/api/session":
            result = {"csrf": session["csrf"], "version": "0.1.0", "file_root": str(self.server.files.root), "platform": os.name, "commands": list(Host.COMMANDS), "full_host": self.server.full_host, "full_host_root": self.server.full_host_root, "public_http": self.server.public_bind and not self._secure_transport(), "https_remote_ssh": self._secure_transport()}
        elif path == "/api/host-settings":
            result = self.server.host_settings.status()
        elif path == "/api/host-settings/job":
            result = self.server.host_settings.job(query.get("id", [""])[0])
        elif path == "/api/sites":
            result = self.server.sites.overview()
        elif path == "/api/sites/environment":
            result = self.server.sites.environment()
        elif path == "/api/sites/scan":
            result = self.server.sites.scan()
        elif path == "/api/sites/job":
            result = self.server.sites.job_status(reveal=query.get("reveal") == ["1"])
        elif path == "/api/appearance":
            result = self.server.appearance.get()
        elif path == "/api/appearance/background":
            content_type, data = self.server.appearance.background()
            self._respond(200, data, content_type, {"Content-Disposition": "inline"})
            return
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
        if self.command == "POST" and path in ("/api/host-settings/change", "/api/host-settings/ssh/confirm"):
            body = self._body()
            operation = "listener.confirm" if path.endswith("/confirm") else "listener.change"
            target = "system SSH" if path.endswith("/confirm") or body.get("kind") == "ssh" else "Harbor Panel"
            origin = self._mfa_origin(True)
            try:
                if self.server.public_origin and not origin:
                    raise PanelError("Use the configured HTTPS address to verify this change.", 403)
                self.server.auth.authorize_listener_change(token, address, origin, body)
                self.server.audit.add(operation + ".requested", target, address)
                if path.endswith("/confirm"):
                    result = self.server.host_settings.confirm_ssh(body.get("id"))
                else:
                    result = self.server.host_settings.start(body.get("kind"), body.get("port"),
                                                             firewall_ready=body.get("firewall_ready"))
            except PanelError:
                self.server.audit.add(operation, target, address, False)
                raise
            self._respond(202, result)
            return
        routes = {("POST", "/api/logout"), ("POST", "/api/terminal"), ("POST", "/api/containers/action"), ("PUT", "/api/containers/config"), ("PUT", "/api/files/write"), ("POST", "/api/files/mkdir"), ("POST", "/api/appearance"), ("PUT", "/api/appearance/background"), ("POST", "/api/appearance/background/remove")}
        if self.command == "POST" and path in ("/api/sites", "/api/sites/action", "/api/sites/environment"):
            body = self._body()
            if path == "/api/sites":
                result = self.server.sites.create(body.get("domain"), body.get("kind"), body.get("upstream"), address)
            elif path == "/api/sites/action":
                result = self.server.sites.action(body.get("id"), body.get("action"), address)
            else:
                result = self.server.sites.set_environment(body.get("service"), body.get("action"), address)
            self._respond(202, result)
            return
        if (self.command, path) not in routes:
            raise PanelError("API route or method not found.", 404)
        if path == "/api/appearance/background" and self.command == "PUT":
            body = {}
        else:
            body = self._body()
        target = body.get("path", body.get("id", body.get("command", "administrator")))
        action = path.removeprefix("/api/").replace("/", ".")
        if path == "/api/terminal" and self.server.full_host:
            target = "host shell command"
        if path == "/api/containers/action":
            target = f"{body.get('action', '')}:{body.get('id', '')}"
        elif path == "/api/containers/config":
            target = body.get("id", "")
        elif path == "/api/appearance":
            target = "panel theme"
        elif path == "/api/appearance/background":
            target = "panel background image"
        elif path == "/api/appearance/background/remove":
            target = "panel background image"
        # Record intent before changing resources, then record the result separately.
        self.server.audit.add(action + ".requested", target, address)
        try:
            if path == "/api/logout":
                self.server.auth.logout(token)
                self.server.terminal.close_owner(token)
                result = {"ok": True}
            elif path == "/api/terminal":
                result = self.server.host.terminal(body.get("command"))
            elif path == "/api/containers/action":
                result = self.server.host.container_action(body.get("id"), body.get("action"))
            elif path == "/api/containers/config":
                if "id" not in body or "config" not in body:
                    raise PanelError("id and config are required.")
                result = self.server.host.update_container(body["id"], body["config"])
            elif path == "/api/appearance":
                result = self.server.appearance.set_theme(body.get("theme"))
            elif path == "/api/appearance/background":
                content_type = self.headers.get("X-Image-Type", "")
                if self.headers.get("Transfer-Encoding"):
                    raise PanelError("Transfer-Encoding is not supported.")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise PanelError("Invalid Content-Length.") from exc
                if length < 0 or length > self.server.appearance.MAX_BACKGROUND:
                    raise PanelError("Background images must be no larger than 1.5 MB (1,500,000 bytes).", 413)
                image_data = self.rfile.read(length)
                if len(image_data) != length:
                    raise PanelError("Incomplete background image upload.")
                result = self.server.appearance.set_background(content_type, image_data)
            elif path == "/api/appearance/background/remove":
                result = self.server.appearance.remove_background()
            elif path == "/api/files/write":
                if "path" not in body or "content" not in body or "etag" not in body:
                    raise PanelError("path, content and etag are required (etag=null creates a file).")
                result = self.server.files.write(body["path"], body["content"], body["etag"])
            else:
                result = self.server.files.mkdir(body.get("path"))
        except (PanelError, OSError):
            self.server.audit.add(action, target, address, False)
            raise
        success = not (path == "/api/terminal" and result.get("exit_code", 0) != 0)
        self.server.audit.add(action, target, address, success)
        headers = {"Set-Cookie": self._cookie("", expired=True)} if path == "/api/logout" else None
        self._respond(200, result, headers=headers)


def main():
    parser = argparse.ArgumentParser(description="Harbor Panel - independent single-host operations workspace")
    parser.add_argument("--host", default=os.environ.get("HARBOR_PANEL_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--root", type=Path, default=BASE / "workspace")
    parser.add_argument("--state-dir", type=Path, default=BASE / ".state")
    parser.add_argument("--public-origin", default=os.environ.get("HARBOR_PANEL_PUBLIC_ORIGIN"),
                        help="HTTPS origin when deployed behind a trusted reverse proxy")
    parser.add_argument("--bootstrap-token-file", type=Path, help="One-time administrator registration credential file")
    parser.add_argument("--full-host", action="store_true", help="Grant file and terminal APIs full host access; use only for trusted administrators")
    args = parser.parse_args()
    password = os.environ.get("HARBOR_PASSWORD")
    if not args.bootstrap_token_file and not password:
        args.bootstrap_token_file = PanelServer.prepare_bootstrap_token(args.state_dir)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        server = PanelServer((args.host, args.port), password, args.root, args.state_dir, args.public_origin, args.bootstrap_token_file, args.full_host)
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
