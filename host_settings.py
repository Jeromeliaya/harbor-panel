"""Opt-in native listener changes. The transient systemd worker owns host mutations."""

import argparse
from contextlib import contextmanager
import errno
import hashlib
import hmac
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import psutil

if __name__ == "__main__" and sys.flags.isolated:
    sys.path.insert(0, str(Path(__file__).absolute().parent))

from files import PanelError

if sys.platform == "linux":
    import fcntl


PORT_FILE = Path("/etc/harbor-panel.port")
NGINX_FILE = Path("/etc/harbor-panel.nginx-upstream")
SSH_CONFIG = Path("/etc/ssh/sshd_config")
SSH_FILE = Path("/etc/ssh/harbor-panel-ports.conf")
PANEL_UNIT = "harbor-panel.service"
ACTIVE = {"queued", "running", "awaiting_confirmation"}
HEALTH_PATH = "/api/host-settings/health"
CONFIRM_SECONDS = 300
HEX_TOKEN = re.compile(r"[0-9a-f]{32}")
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
LOG = logging.getLogger(__name__)


def _port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise PanelError("Enter a whole TCP port from 1 to 65535.", code="invalid_port")
    return value


def _directory(path):
    for entry in (path, *path.parents):
        info = entry.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise PanelError("Listener management requires trusted root-owned directories.", 409)


def _regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
        raise PanelError("Listener management requires root-owned, non-linked files that others cannot write.", 409)


def _open_file(path, flags):
    _directory(path.parent)
    # Reject devices/FIFOs before opening; O_NOFOLLOW and fstat check the opened inode as well.
    try:
        _regular(path.lstat())
    except FileNotFoundError:
        if not flags & os.O_CREAT:
            raise
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _regular(os.fstat(fd))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_file(path, limit=65536):
    with os.fdopen(_open_file(path, os.O_RDONLY), "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise PanelError("A listener configuration file is too large.", 409)
    return data.decode("utf-8")


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path, text):
    _directory(path.parent)
    try:
        _regular(path.lstat())
    except FileNotFoundError:
        pass
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _swap(path, old, new):
    current = _read_file(path)
    if current == new:
        return
    if current != old:
        raise PanelError(f"{path.name} was changed outside Harbor Panel; refusing to overwrite it.", 409)
    _atomic_write(path, new)


def _panel_file(port):
    return f"HARBOR_PANEL_PORT={port}\n"


def _nginx_file(port):
    return f"proxy_pass http://127.0.0.1:{port};\n"


def _ssh_file(*ports):
    return "".join(f"Port {port}\n" for port in ports)


def _matches(text, expected):
    return text in (expected, expected.rstrip("\n"))


def _error(message, status=409):
    return PanelError(message, status, code="host_settings_unavailable")


def _interface_address(text):
    address = ipaddress.ip_address(text.split("%", 1)[0])
    return getattr(address, "ipv4_mapped", None) or address


def _local_https_endpoints(hostname, port):
    interfaces = {_interface_address(item.address) for group in psutil.net_if_addrs().values() for item in group
                  if item.family in (socket.AF_INET, socket.AF_INET6)}
    endpoints = set()
    for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM):
        address = _interface_address(sockaddr[0])
        if (family not in (socket.AF_INET, socket.AF_INET6) or address.is_unspecified or address.is_multicast
                or (not address.is_loopback and address not in interfaces)):
            raise _error("The HTTPS origin must resolve only to addresses on this VPS.")
        endpoints.add((family, sockaddr))
    if not 1 <= len(endpoints) <= 8:
        raise _error("The HTTPS origin must resolve to one or more addresses on this VPS.")
    return sorted(endpoints)


class _LocalHTTPSConnection(HTTPSConnection):
    def __init__(self, hostname, port, endpoint, context):
        super().__init__(hostname, port, timeout=2, context=context)
        self.endpoint = endpoint
        self.context = context

    def connect(self):
        family, sockaddr = self.endpoint
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(sockaddr)
            self.sock = self.context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class HostOperations:
    def available(self):
        return (sys.platform == "linux" and os.geteuid() == 0 and Path("/run/systemd/system").is_dir()
                and all(shutil.which(name, path=SAFE_PATH) for name in ("systemctl", "systemd-run")))

    def command(self, name, *args, check=True, timeout=35):
        executable = shutil.which(name, path=SAFE_PATH)
        if not executable:
            raise _error(f"{name} is required for this listener change.")
        try:
            result = subprocess.run([executable, *args], capture_output=True, text=True,
                                    errors="replace", stdin=subprocess.DEVNULL, cwd="/", timeout=timeout,
                                    env={"PATH": SAFE_PATH, "LANG": "C", "SYSTEMD_PAGER": "cat"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _error(f"{name} did not complete; inspect the service journal.") from exc
        if check and result.returncode:
            LOG.warning("Host listener operation %s %s exited %d", name, args[0], result.returncode)
            raise _error(f"{name} {args[0]} failed; inspect the service journal and configuration.")
        return result

    def unit(self, name, *keys):
        args = [arg for key in keys for arg in ("--property", key)]
        result = self.command("systemctl", "show", *args, "--", name, check=False)
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def launch(self, state, job_id):
        script = Path(__file__).absolute()
        _directory(script.parent)
        _regular(script.lstat())
        # Isolated Python ignores caller-provided Python paths and the user site directory. The worker adds its own trusted directory.
        interpreter = Path(sys.executable).resolve(strict=True)
        _directory(interpreter.parent)
        if interpreter.stat().st_uid != 0 or interpreter.stat().st_mode & 0o022:
            raise _error("The worker's Python interpreter is not trusted.")
        venv_python = Path(sys.executable).absolute()
        _directory(venv_python.parent)
        self.command("systemd-run", "--quiet", "--no-block", "--unit", self.worker_unit(job_id),
                     "--property=Type=exec", "--property=Restart=on-failure", "--property=RestartSec=3s",
                     "--property=RestartForceExitStatus=SIGHUP SIGINT SIGTERM SIGPIPE",
                     "--property=UMask=0077", str(venv_python), "-IB", str(script), "--worker", str(state), job_id)

    @staticmethod
    def worker_unit(job_id):
        return f"harbor-panel-listener-{job_id}.service"

    def free_port(self, port):
        listeners = []
        try:
            for family, host in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
                try:
                    probe = socket.socket(family, socket.SOCK_STREAM)
                    listeners.append(probe)
                    if family == socket.AF_INET6:
                        probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    probe.bind((host, port))
                except OSError as exc:
                    if family == socket.AF_INET6 and exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT,
                                                                  errno.EADDRNOTAVAIL):
                        continue
                    raise PanelError(f"TCP port {port} is occupied or unavailable.", 409, code="port_in_use") from exc
        finally:
            for probe in listeners:
                probe.close()

    def https_proxy(self, port, text):
        if not _matches(text, _nginx_file(port)):
            raise _error(f"Bootstrap {NGINX_FILE} with only: {_nginx_file(port).strip()}")
        result = self.command("nginx", "-T")
        if f"# configuration file {NGINX_FILE}:" not in result.stdout.splitlines():
            raise _error(f"The live Nginx configuration must include {NGINX_FILE} in the panel location.")

    def reload_nginx(self):
        self.command("nginx", "-t")
        self.command("systemctl", "reload", "nginx.service")

    def probe_panel(self, base, nonce, proof):
        verified = threading.Event()

        def request(connection, host=None):
            try:
                headers = {"Cache-Control": "no-cache"}
                if host:
                    headers["Host"] = host
                connection.request("GET", f"{HEALTH_PATH}?nonce={nonce}", headers=headers)
                with connection.getresponse() as response:
                    return response.status == 200 and json.loads(response.read(256)) == {"proof": proof}
            finally:
                connection.close()

        def fetch():
            try:
                origin = urlsplit(base)
                if origin.scheme == "https" and origin.hostname:
                    hostname = origin.hostname.encode("idna").decode("ascii")
                    port = origin.port or 443
                    endpoints = _local_https_endpoints(hostname, port)
                    context = ssl.create_default_context()
                    if all(request(_LocalHTTPSConnection(hostname, port, endpoint, context)) for endpoint in endpoints):
                        verified.set()
                elif origin.scheme == "http" and origin.hostname == "127.0.0.1" and origin.port:
                    if request(HTTPConnection("127.0.0.1", origin.port, timeout=2)):
                        verified.set()
                elif origin.scheme == "http" and origin.hostname and origin.port in (None, 80):
                    if request(HTTPConnection("127.0.0.1", 80, timeout=2), origin.hostname):
                        verified.set()
            except (OSError, ValueError, HTTPException, PanelError, psutil.Error):
                pass

        # The socket timeout does not bound DNS lookup or a slow stream of bytes.
        thread = threading.Thread(target=fetch, daemon=True)
        thread.start()
        thread.join(timeout=4)
        return verified.is_set()

    def ssh_unit(self):
        for name in ("ssh.socket", "sshd.socket"):
            socket_unit = self.unit(name, "LoadState", "ActiveState", "UnitFileState")
            if (socket_unit.get("ActiveState") != "inactive"
                    or (socket_unit.get("LoadState") not in ("not-found", "masked")
                        and socket_unit.get("UnitFileState") not in ("disabled", "masked"))):
                raise _error("Socket-activated OpenSSH port changes are not supported.")
        active = {}
        for name in ("ssh.service", "sshd.service"):
            info = self.unit(name, "Id", "ActiveState", "CanReload", "MainPID", "ExecStart", "TriggeredBy",
                             "NeedDaemonReload")
            if info.get("ActiveState") == "active":
                active[info["Id"]] = info
        if len(active) != 1:
            raise _error("A single running, non-socket-activated OpenSSH systemd service is required.")
        name, info = next(iter(active.items()))
        start = re.search(r"(?:^|[;{])\s*argv\[\]=(.*?)\s*;", info.get("ExecStart", ""))
        if (name not in ("ssh.service", "sshd.service") or info.get("CanReload") != "yes"
                or info.get("NeedDaemonReload") != "no" or info.get("TriggeredBy") or not start
                or not re.fullmatch(r"/(?:usr/)?sbin/sshd -D(?: \$(?:SSHD_OPTS|OPTIONS))?", start[1])):
            raise _error("Customized or socket-activated OpenSSH units are not supported.")
        try:
            process = psutil.Process(int(info["MainPID"]))
            environment = process.environ()
            if process.pid < 2 or any(environment.get(key) for key in ("SSHD_OPTS", "OPTIONS", "LISTEN_FDS", "LISTEN_PID")):
                raise ValueError
        except (psutil.Error, OSError, KeyError, ValueError) as exc:
            raise _error("OpenSSH must run with its standard configuration and no command-line overrides.") from exc
        return name

    def ssh_config(self, ports):
        active_lines = [line.partition("#")[0].strip() for line in _read_file(SSH_CONFIG).splitlines()]
        global_lines = []
        for line in active_lines:
            if re.match(r"match(?:\s|=)", line, flags=re.IGNORECASE):
                break
            global_lines.append(line)
        if not any(re.fullmatch(rf"(?i:include)\s+{re.escape(str(SSH_FILE))}", line) for line in global_lines):
            raise PanelError(f"Add a global Include {SSH_FILE} to OpenSSH before any Match blocks.",
                             409, code="ssh_include_missing")
        return self._ssh_effective(ports)

    def _ssh_effective(self, ports):
        self.command("sshd", "-t", "-f", str(SSH_CONFIG))
        result = self.command("sshd", "-T", "-f", str(SSH_CONFIG))
        effective = set()
        addresses = set()
        other = []
        try:
            for line in result.stdout.splitlines():
                key, _, value = line.partition(" ")
                if key == "port":
                    effective.add(int(value))
                elif key == "listenaddress":
                    addr, _, port = value.rpartition(":")
                    addresses.add((str(ipaddress.ip_address(addr.strip("[]"))), int(port)))
                else:
                    other.append(line)
        except ValueError as exc:
            raise _error("Unsupported OpenSSH listen address configuration.") from exc
        hosts = {address for address, _ in addresses}
        if (effective != set(ports) or not hosts or not hosts <= {"0.0.0.0", "::"}
                or addresses != {(address, port) for address in hosts for port in ports}):
            raise _error("Only ports from the managed OpenSSH include and standard wildcard listeners are supported.")
        digest = hashlib.sha256("\n".join(sorted(other)).encode("utf-8")).hexdigest()
        return sorted(hosts), digest

    def ssh_bootstrap(self, *, apply=False):
        original = _read_file(SSH_CONFIG)
        result = self.command("sshd", "-T", "-f", str(SSH_CONFIG))
        ports = re.findall(r"(?m)^port (\d+)$", result.stdout)
        if len(ports) != 1:
            raise _error("Automatic SSH setup requires a single current OpenSSH port.")
        old = _port(int(ports[0]))
        baseline = self._ssh_effective([old])
        try:
            existing = _read_file(SSH_FILE)
        except FileNotFoundError:
            existing = None
        if existing is not None and existing != _ssh_file(old):
            raise _error("The existing SSH port snippet differs from the current listener.")
        if not apply:
            return old, baseline
        candidate = [f"Include {SSH_FILE}"]
        global_section = True
        for line in original.splitlines():
            directive = line.partition("#")[0].strip()
            if re.match(r"(?i:match)(?:\s|=)", directive):
                global_section = False
            if global_section and re.fullmatch(rf"(?i:include)\s+{re.escape(str(SSH_FILE))}", directive):
                continue
            if global_section and re.match(r"(?i:port)(?:\s|=)", directive):
                line = "# Harbor Panel adopted: " + line
            candidate.append(line)
        text = "\n".join(candidate) + "\n"
        if existing is None:
            with os.fdopen(_open_file(SSH_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w") as handle:
                handle.write(_ssh_file(old))
                handle.flush()
                os.fsync(handle.fileno())
            _sync_dir(SSH_FILE.parent)
        try:
            _swap(SSH_CONFIG, original, text)
            if self.ssh_config([old]) != baseline:
                raise _error("Automatic SSH setup changed settings beyond the listener port.")
        except BaseException:
            _swap(SSH_CONFIG, text, original)
            if existing is None:
                SSH_FILE.unlink(missing_ok=True)
                _sync_dir(SSH_FILE.parent)
            raise
        return old, baseline

    def ssh_listening(self, unit, ports, hosts):
        info = self.unit(unit, "MainPID", "ActiveState")
        if info.get("ActiveState") != "active":
            return False
        try:
            connections = psutil.Process(int(info["MainPID"])).net_connections(kind="tcp")
            bound = {(c.laddr.ip, c.laddr.port) for c in connections if c.status == psutil.CONN_LISTEN}
            # Require the advertised IPv4/IPv6 wildcard ingress; verify the SSH banner on each.
            if bound != {(host, port) for host in hosts for port in ports}:
                return False
            for host, port in bound:
                with socket.create_connection(("127.0.0.1" if host == "0.0.0.0" else "::1", port), timeout=2) as sock:
                    with sock.makefile("rb") as banner:
                        if not banner.readline(256).startswith((b"SSH-2.0-", b"SSH-1.99-")):
                            return False
            return True
        except (psutil.Error, OSError, KeyError, ValueError):
            return False


class HostSettings:
    def __init__(self, state_dir, *, full_host, panel_port, public_origin, panel_host="0.0.0.0", operations=None):
        self.state = Path(state_dir).absolute()
        self.full_host = full_host
        self.panel_port = panel_port
        self.panel_host = panel_host
        self.public_origin = public_origin
        self.ops = operations if operations is not None else HostOperations()
        self.data = self.state / "host-settings.jobs.json"

    def _supported(self):
        return self.full_host and self.ops.available()

    def _proxy_origin(self):
        if self.public_origin:
            return self.public_origin.rstrip("/")
        from panel_proxy import PanelProxy
        site = PanelProxy(self.state, self.panel_port, enabled=self.full_host).site
        return f"http://{site['domain']}" if site and site["enabled"] else None

    def _require(self):
        if not self._supported():
            raise _error("Listener management requires the native root systemd deployment (not Docker or OpenRC).", 403)

    @contextmanager
    def _lock(self, name="host-settings.lock", *, nonblocking=False):
        fd = _open_file(self.state / name, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            yield
        finally:
            os.close(fd)

    def _jobs(self):
        try:
            return json.loads(_read_file(self.data, limit=1024 * 1024))
        except FileNotFoundError:
            return []

    def _save(self, jobs):
        _atomic_write(self.data, json.dumps(jobs[-20:], ensure_ascii=True))

    @staticmethod
    def _public(job):
        fields = ("id", "kind", "status", "old_port", "port", "message", "confirmation_deadline", "public_origin", "mode")
        return {key: job[key] for key in fields if key in job}

    @staticmethod
    def _find(jobs, job_id):
        if isinstance(job_id, str) and HEX_TOKEN.fullmatch(job_id):
            for job in reversed(jobs):
                if job["id"] == job_id:
                    return job
        raise PanelError("Listener change job not found.", 404)

    def _update(self, job_id, **updates):
        with self._lock():
            jobs = self._jobs()
            job = self._find(jobs, job_id)
            job.update(updates)
            self._save(jobs)
            return job

    def _preflight_panel(self, old_port, *, require_current_process=False):
        if self.panel_host not in ("127.0.0.1", "0.0.0.0"):
            raise _error("Only the native IPv4 wildcard or loopback Harbor Panel listener is supported.")
        if self.public_origin:
            try:
                origin = urlsplit(self.public_origin)
                if (origin.scheme != "https" or not origin.hostname or origin.username is not None
                        or origin.password is not None or origin.path not in ("", "/") or origin.query or origin.fragment
                        or origin.port == 0):
                    raise ValueError
            except ValueError as exc:
                raise _error("An HTTPS public origin is required for proxied port changes.") from exc
        unit = self.ops.unit(PANEL_UNIT, "ActiveState", "MainPID", "NeedDaemonReload")
        if (unit.get("ActiveState") != "active" or unit.get("NeedDaemonReload") != "no"
                or (require_current_process and unit.get("MainPID") != str(os.getpid()))):
            raise _error("The running panel must be managed by harbor-panel.service.")
        try:
            env = _read_file(PORT_FILE)
        except FileNotFoundError:
            env = None
        if not _matches(env, _panel_file(old_port)):
            raise _error(f"Bootstrap {PORT_FILE} with only HARBOR_PANEL_PORT={old_port}.")
        files = {"panel": env}
        if self._proxy_origin():
            try:
                snippet = _read_file(NGINX_FILE)
            except FileNotFoundError as exc:
                raise _error(f"Bootstrap {NGINX_FILE} and include it from the HTTPS panel location.") from exc
            self.ops.https_proxy(old_port, snippet)
            files["nginx"] = snippet
        return files

    def _preflight_ssh(self, *, prepare=False):
        unit = self.ops.ssh_unit()
        try:
            text = _read_file(SSH_FILE)
        except FileNotFoundError:
            old, (hosts, baseline) = self.ops.ssh_bootstrap(apply=prepare)
            if not self.ops.ssh_listening(unit, [old], hosts):
                raise _error("The current OpenSSH listener could not be verified.")
            return old, unit, _ssh_file(old), hosts, baseline
        match = re.fullmatch(r"Port ([0-9]{1,5})\n?", text)
        if not match:
            raise _error(f"Bootstrap {SSH_FILE} with only Port <current-port>.")
        old = _port(int(match[1]))
        try:
            hosts, baseline = self.ops.ssh_config([old])
        except PanelError as exc:
            if exc.code != "ssh_include_missing":
                raise
            adopted, (hosts, baseline) = self.ops.ssh_bootstrap(apply=prepare)
            if adopted != old:
                raise _error("The existing SSH snippet differs from the current listener.")
        if not self.ops.ssh_listening(unit, [old], hosts):
            raise _error("The current OpenSSH listener could not be verified.")
        return old, unit, text, hosts, baseline

    @staticmethod
    def _reason(exc):
        if isinstance(exc, PanelError):
            return str(exc)
        if isinstance(exc, FileNotFoundError):
            return "Operator setup is incomplete (a required listener file is missing)."
        LOG.exception("Host listener settings operation failed")
        return "Listener management failed; inspect the service journal."

    def status(self):
        proxy_origin = self._proxy_origin()
        mode = "https_proxy" if self.public_origin else ("http_proxy" if proxy_origin else "direct")
        panel = {"port": self.panel_port, "mode": mode, "public_origin": self.public_origin,
                 "proxy_origin": proxy_origin, "available": False, "reason": None}
        ssh = {"ports": [], "available": False, "reason": None}
        if not self._supported():
            for item in (panel, ssh):
                item["reason"] = "Requires the native root systemd deployment."
            return {"panel": panel, "ssh": ssh, "job": None}
        try:
            with self._lock():
                jobs = self._jobs()
                latest = jobs[-1] if jobs else None
            if latest and latest["status"] == "rollback_failed":
                panel["reason"] = ssh["reason"] = "Previous rollback failed; recover from the VPS console first."
                return {"panel": panel, "ssh": ssh, "job": self._public(latest)}
            if latest and latest["status"] in ACTIVE:
                panel["reason"] = ssh["reason"] = "A listener change is already in progress."
                if latest["kind"] == "ssh" and latest["status"] == "awaiting_confirmation":
                    ssh["ports"] = [latest["old_port"], latest["port"]]
                return {"panel": panel, "ssh": ssh, "job": self._public(latest)}
        except (OSError, PanelError, ValueError) as exc:
            panel["reason"] = ssh["reason"] = self._reason(exc)
            return {"panel": panel, "ssh": ssh, "job": None}
        for item, check in ((panel, lambda: self._preflight_panel(self.panel_port, require_current_process=True)),
                            (ssh, self._preflight_ssh)):
            try:
                result = check()
                if item is ssh:
                    ssh["ports"] = [result[0]]
                item["available"] = True
            except (OSError, PanelError, ValueError) as exc:
                item["reason"] = self._reason(exc)
        return {"panel": panel, "ssh": ssh, "job": self._public(latest) if latest else None}

    def start(self, kind, port, *, firewall_ready=False):
        if kind not in ("panel", "ssh"):
            raise PanelError("Unknown listener setting.")
        port = _port(port)
        self._require()
        try:
            with self._lock():
                jobs = self._jobs()
                if jobs and jobs[-1]["status"] == "rollback_failed":
                    raise _error("Previous rollback failed; recover from the VPS console first.")
                if jobs and jobs[-1]["status"] in ACTIVE:
                    raise PanelError("A listener change is already in progress.", 409, code="listener_busy")
                mode = "ssh" if kind == "ssh" else ("https_proxy" if self.public_origin else
                                                  ("http_proxy" if self._proxy_origin() else "direct"))
                if kind == "panel":
                    old = self.panel_port
                    if old != port:
                        self._preflight_panel(old, require_current_process=True)
                else:
                    old, *_ = self._preflight_ssh()
                job = {"id": secrets.token_hex(16), "kind": kind, "old_port": old, "port": port,
                       "mode": mode, "status": "queued", "message": "Listener change queued.",
                       "panel_host": self.panel_host, "queued_at": time.time(),
                       "public_origin": self.public_origin if kind == "panel" else None}
                if old == port:
                    job.update(status="succeeded", message=f"TCP port {port} is already configured; nothing changed.")
                else:
                    if mode not in ("https_proxy", "http_proxy") and firewall_ready is not True:
                        raise PanelError("Open the new TCP port in the cloud security group and host firewall first.",
                                         code="firewall_confirmation_required")
                    self.ops.free_port(port)
                jobs.append(job)
                self._save(jobs)
                if job["status"] == "queued":
                    try:
                        self.ops.launch(self.state, job["id"])
                    except (OSError, PanelError, ValueError) as exc:
                        job.update(status="failed", message=self._reason(exc))
                        self._save(jobs)
                        raise _error(job["message"]) from exc
                return self._public(job)
        except (OSError, ValueError) as exc:
            raise _error(self._reason(exc)) from exc

    def job(self, job_id):
        self._require()
        try:
            with self._lock():
                return self._public(self._find(self._jobs(), job_id))
        except (OSError, ValueError) as exc:
            raise _error(self._reason(exc)) from exc

    def confirm_ssh(self, job_id):
        self._require()
        try:
            with self._lock():
                jobs = self._jobs()
                job = self._find(jobs, job_id)
                if (job["kind"] != "ssh" or job["status"] != "awaiting_confirmation"
                        or time.time() >= job["confirmation_deadline"]):
                    raise PanelError("This SSH change is not awaiting confirmation.", 409)
                job.update(confirmed=True, status="running", message="Finishing the verified SSH port change.")
                self._save(jobs)
                return self._public(job)
        except (OSError, ValueError) as exc:
            raise _error(self._reason(exc)) from exc

    def health(self, nonce):
        if not isinstance(nonce, str) or not HEX_TOKEN.fullmatch(nonce) or not self._supported():
            return None
        try:
            with self._lock():
                jobs = self._jobs()
                probe = jobs[-1].get("probe") if jobs and jobs[-1]["status"] == "running" else None
                if (probe and self.panel_port == probe["port"] and self.public_origin == probe["public_origin"]
                        and time.time() < probe["expires"] and hmac.compare_digest(probe["nonce"], nonce)):
                    return {"proof": probe["proof"]}
        except (OSError, PanelError, ValueError):
            pass
        return None

    def _probe(self, job, port):
        probe = {"nonce": secrets.token_hex(16), "proof": secrets.token_hex(32), "port": port,
                 "public_origin": job["public_origin"], "expires": time.time() + 180}
        self._update(job["id"], probe=probe)
        return probe

    @staticmethod
    def _wait(test, message, seconds=20):
        deadline = time.monotonic() + seconds
        while True:
            if test():
                return
            if time.monotonic() >= deadline:
                raise _error(message)
            time.sleep(0.5)

    def _verify_panel(self, base, probe, message):
        self._wait(lambda: self.ops.probe_panel(base, probe["nonce"], probe["proof"]), message)

    def _switch_panel(self, job):
        job_id, old, port = job["id"], job["old_port"], job["port"]
        files = self._preflight_panel(old)
        origin = self._proxy_origin()
        probe = self._probe(job, old)
        self._verify_panel(f"http://127.0.0.1:{old}", probe, "Could not verify the current local panel listener.")
        if origin:
            self._verify_panel(origin, probe, "Could not verify same-host HTTPS routing and TLS for the configured origin. No files were changed.")
        self.ops.free_port(port)
        probe = self._probe(job, port)
        self._update(job_id, rollback={"files": files, "panel": True}, message="Restarting the panel on the new port.")
        _swap(PORT_FILE, files["panel"], _panel_file(port))
        self.ops.command("systemctl", "restart", PANEL_UNIT)
        self._verify_panel(f"http://127.0.0.1:{port}", probe, "The panel did not come up on its new local port.")
        if origin:
            self._update(job_id, rollback={"files": files, "panel": True, "nginx": True},
                         message="Testing the HTTPS proxy on the new local port.")
            _swap(NGINX_FILE, files["nginx"], _nginx_file(port))
            self.ops.https_proxy(port, _nginx_file(port))
            self.ops.reload_nginx()
            self._verify_panel(origin, probe, "The HTTPS origin did not reach the new local panel listener.")
            return f"Panel now listens on TCP {port}. Your public domain address is unchanged."
        return f"Panel now listens on TCP {port}. Use the new port; cloud and host firewall access was not verified."

    def _switch_ssh(self, job):
        job_id, old, port = job["id"], job["old_port"], job["port"]
        current, unit, text, hosts, baseline = self._preflight_ssh(prepare=True)
        if current != old:
            raise _error("The live OpenSSH port changed while the job was queued.")
        self.ops.free_port(port)
        self._update(job_id, rollback={"ssh": text, "unit": unit, "hosts": hosts, "baseline": baseline})
        _swap(SSH_FILE, text, _ssh_file(old, port))
        self._reload_ssh(unit, [old, port], hosts, baseline)
        deadline = time.time() + CONFIRM_SECONDS
        self._update(job_id, status="awaiting_confirmation", confirmation_deadline=deadline,
                     message=(f"SSH still listens on TCP {old}. Open a separate connection to TCP {port} from outside "
                              "the VPS, verify the host key and sign in, then confirm here before the deadline."))

        def confirmed():
            with self._lock():
                return self._find(self._jobs(), job_id).get("confirmed", False)

        self._wait(confirmed, "The new SSH port was not confirmed in time.", seconds=CONFIRM_SECONDS)
        if self.ops.ssh_unit() != unit or self.ops.ssh_config([old, port]) != (hosts, baseline):
            raise _error("OpenSSH configuration changed during verification.")
        _swap(SSH_FILE, _ssh_file(old, port), _ssh_file(port))
        self._reload_ssh(unit, [port], hosts, baseline)
        return f"OpenSSH now listens on TCP {port}. The old TCP {old} listener was retired."

    def _reload_ssh(self, unit, ports, hosts, baseline):
        if self.ops.ssh_unit() != unit or self.ops.ssh_config(ports) != (hosts, baseline):
            raise _error("OpenSSH settings other than the managed ports changed; no reload was attempted.")
        self.ops.command("systemctl", "reload", unit)
        self._wait(lambda: self.ops.ssh_listening(unit, ports, hosts), "Could not verify the expected OpenSSH listeners.")

    def _rollback(self, job):
        changes, old, port = job["rollback"], job["old_port"], job["port"]
        if job["kind"] == "ssh":
            current = _read_file(SSH_FILE)
            if current != changes["ssh"] and current not in (_ssh_file(old, port), _ssh_file(port)):
                raise _error("The managed OpenSSH file changed outside Harbor Panel; restore it from the VPS console.")
            _swap(SSH_FILE, current, changes["ssh"])
            self._reload_ssh(changes["unit"], [old], changes["hosts"], changes["baseline"])
            return
        files = changes["files"]
        errors = []
        if changes.get("nginx"):
            try:
                _swap(NGINX_FILE, _nginx_file(port), files["nginx"])
                self.ops.https_proxy(old, files["nginx"])
                self.ops.reload_nginx()
            except (OSError, PanelError, ValueError) as exc:
                errors.append(self._reason(exc))
        try:
            _swap(PORT_FILE, _panel_file(port), files["panel"])
            probe = self._probe(job, old)
            self.ops.command("systemctl", "restart", PANEL_UNIT)
            self._verify_panel(f"http://127.0.0.1:{old}", probe, "The old local panel listener was not verified.")
            if self._proxy_origin():
                self._verify_panel(self._proxy_origin(), probe, "The old proxy route was not verified.")
        except (OSError, PanelError, ValueError) as exc:
            errors.append(self._reason(exc))
        if errors:
            raise _error(" ".join(errors))

    def _run(self, job, *, recover=False):
        job_id = job["id"]
        try:
            if recover or job["status"] != "queued" or time.time() - job["queued_at"] > 90:
                raise _error("The listener change worker was interrupted.")
            self._update(job_id, status="running", message="Verifying listener configuration.")
            message = self._switch_panel(job) if job["kind"] == "panel" else self._switch_ssh(job)
            self._update(job_id, status="succeeded", message=message, rollback=None, probe=None)
        except Exception as exc:
            message = self._reason(exc)
            with self._lock():
                task = self._find(self._jobs(), job_id)
            if not task.get("rollback"):
                self._update(job_id, status="failed", message=message, probe=None)
                return
            self._update(job_id, status="running", message="Restoring the previous listener.", probe=None)
            try:
                self._rollback(task)
            except Exception as rollback_exc:
                self._update(job_id, status="rollback_failed", probe=None,
                             message=f"{message} Rollback failed: {self._reason(rollback_exc)} Use the VPS console.")
            else:
                self._update(job_id, status="rolled_back", rollback=None, probe=None,
                             message=f"{message} The previous listener was restored and verified.")

    def run_worker(self, job_id, *, recover=False):
        self._require()
        with self._lock("host-settings.worker.lock", nonblocking=True):
            with self._lock():
                jobs = self._jobs()
                job = self._find(jobs, job_id)
            if job is not jobs[-1] or (job["status"] not in ACTIVE
                                      and not (recover and job["status"] == "rollback_failed")):
                return
            self.panel_host, self.public_origin = job["panel_host"], job["public_origin"]
            self._run(job, recover=recover)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recover", action="store_true", help="Only roll back the latest interrupted job; never apply it")
    parser.add_argument("--worker", nargs=2, required=True, metavar=("STATE_DIR", "JOB_ID"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    state, job = args.worker
    HostSettings(state, full_host=True, panel_port=None, public_origin=None).run_worker(job, recover=args.recover)


if __name__ == "__main__":
    main()
