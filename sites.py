"""Single-host site management using a dedicated Caddy container and local Docker."""

from dataclasses import dataclass
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

import psutil

from files import PanelError


LOG = logging.getLogger(__name__)
CADDY = "harbor-panel-caddy"
CADDY_IMAGE = "docker.io/library/caddy:2"
WP_IMAGE = "docker.io/library/wordpress:php8.3-apache"
DB_IMAGE = "docker.io/library/mariadb:11.4"
OWNER_LABEL = "io.harbor-panel.website"
ADMIN_ADDRESS = "unix//run/harbor-sites-admin.sock"
CADDYFILE = "/etc/harbor-sites/Caddyfile"
LE_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
STATE_PREFIX = "# harbor-sites-v1 "
DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
CONTAINER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
SITE_ID = re.compile(r"[0-9a-f]{16}\Z")
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def domain_name(value):
    if not isinstance(value, str) or len(value) > 253:
        raise PanelError("请输入有效的域名，不要带协议、端口或路径。")
    try:
        name = value.strip().removesuffix(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise PanelError("请输入有效的域名。") from exc
    labels = name.split(".")
    if (len(name) > 253 or len(labels) < 2 or labels[-1].isdigit()
            or not all(re.fullmatch(DNS_LABEL, label) for label in labels)):
        raise PanelError("请输入有效的域名，不要带协议、端口或路径。")
    return name


def authority(host, port):
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@dataclass(frozen=True)
class Upstream:
    scheme: str
    host: str
    port: int

    @property
    def address(self):
        return authority(self.host, self.port)

    @property
    def url(self):
        return f"{self.scheme}://{self.address}"


def parse_upstream(value, kind):
    if (not isinstance(value, str) or not value.strip() or len(value) > 400
            or any(ch.isspace() or ord(ch) < 32 or ch in '\\%{}?#' for ch in value.strip())):
        raise PanelError("请输入有效的上游地址，例如 127.0.0.1:3000 或 https://origin.example.com。")
    value = value.strip()
    try:
        parsed = urlsplit(value if "://" in value else f"{'https' if kind == 'https' else 'http'}://{value}")
        port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise PanelError("上游主机或端口无效；IPv6 地址须放在方括号里。") from exc
    if (parsed.scheme not in ("http", "https") or not host or parsed.username is not None
            or parsed.path not in ("", "/") or parsed.netloc.endswith(":") or port == 0):
        raise PanelError("上游仅支持 HTTP/HTTPS 主机和端口，不支持凭证、路径或查询参数。")
    if "[" in parsed.netloc and not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parsed.netloc):
        raise PanelError("IPv6 上游的格式应为 [地址]:端口。")
    if kind == "https":
        if parsed.scheme != "https":
            raise PanelError("域名反代的上游必须使用 HTTPS。")
        host = domain_name(host)
    else:
        if port is None:
            raise PanelError("IP/端口反代必须填写端口，例如 127.0.0.1:3000。")
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            if "." in host:
                host = domain_name(host)
            elif not CONTAINER_NAME.fullmatch(host):
                raise PanelError("上游不是有效的 IP、域名或 Docker 服务名。")
    return Upstream(parsed.scheme, host, port or 443)


def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".harbor-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class SiteManager:
    MAX_SITES = 50
    MAX_SCAN = 200
    ADMIN = "site-admin"

    def __init__(self, state, enabled, audit, panel_port, panel_hosts, secure=False):
        self.directory = Path(state) / "websites"
        self.config_dir = self.directory / "proxy"
        self.config = self.config_dir / "Caddyfile"
        self.docker_config = self.directory / "docker-client"
        self.supported = bool(enabled and os.name == "posix")
        self.audit = audit
        self.panel_port = panel_port
        self.panel_hosts = set(panel_hosts)
        self.secure = secure
        self.lock = threading.Lock()
        self.job = None
        self.credentials_expire = 0
        self.sites = []
        if self.supported and self.config.exists():
            with self.config.open(encoding="utf-8") as file:
                line = file.readline(100_000)
            if not line.startswith(STATE_PREFIX):
                raise ValueError("The Harbor website configuration is missing managed state.")
            self.sites = json.loads(line[len(STATE_PREFIX):])

    def overview(self):
        with self.lock:
            return {"sites": [self._public(site) for site in self.sites], "supported": self.supported,
                    "job": self._job(False)}

    @staticmethod
    def _public(site):
        return {key: site[key] for key in ("id", "domain", "kind", "upstream", "enabled")}

    def _job(self, credentials):
        if self.job is None:
            return None
        if time.monotonic() >= self.credentials_expire:
            self.job.pop("credentials", None)
        return {key: value for key, value in self.job.items() if credentials or key != "credentials"}

    def job_status(self, reveal=False):
        with self.lock:
            job = self._job(reveal and self.secure)
            if job is not None and self.secure and "credentials" in self.job:
                job["has_credentials"] = True
            return {"job": job}

    def _require_supported(self):
        if not self.supported:
            raise PanelError("网站管理需要原生 Linux root 部署；默认容器及 Windows 不会修改宿主机。", 403)

    def _submit(self, name, target, address, function, *args):
        self._require_supported()
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise PanelError("已有网站任务正在运行，请等待完成。", 409)
            job = {"id": secrets.token_hex(8), "status": "running", "message": "正在执行，请稍候…"}
            self.audit.add(f"sites.{name}.requested", target, address)
            self.job = job
            worker = threading.Thread(target=self._work, args=(job, name, target, address, function, args),
                                      daemon=True, name="harbor-sites")
            try:
                worker.start()
            except RuntimeError as exc:
                self.job = None
                raise PanelError("无法启动网站任务。", 503) from exc
            return {"job": dict(job)}

    def _work(self, job, name, target, address, function, args):
        credentials = None
        try:
            message, credentials = function(*args)
            status = "done"
        except PanelError as exc:
            message, status = str(exc), "failed"
        except Exception:
            LOG.exception("Managed site operation failed: %s", name)
            message, status = "任务失败，请查看 Harbor Panel 服务日志。", "failed"
        with self.lock:
            job.update(status=status, message=message)
            if credentials:
                job["credentials"] = credentials
                self.credentials_expire = time.monotonic() + 600
        try:
            self.audit.add(f"sites.{name}", target, address, status == "done")
        except Exception:
            LOG.exception("Could not audit site operation: %s", name)

    @staticmethod
    def _command(arguments, operation, timeout=12, data=None, optional=False):
        command = shutil.which(arguments[0], path=SYSTEM_PATH)
        if not command:
            if optional:
                return None
            raise PanelError(f"未找到 {arguments[0]}，请先准备网站环境。", 503)
        try:
            result = subprocess.run([command, *arguments[1:]], input=data if data is not None else "",
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                    errors="replace", timeout=timeout, check=False,
                                    env={"PATH": SYSTEM_PATH, "LC_ALL": "C", "HOME": "/root",
                                         "DEBIAN_FRONTEND": "noninteractive"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            if optional:
                return None
            raise PanelError(f"{operation}失败或超时，请检查宿主机环境。", 503) from exc
        if result.returncode:
            if optional:
                return None
            LOG.warning("Website operation %s failed (exit %d)", operation, result.returncode)
            raise PanelError(f"{operation}失败，请检查环境状态及相应的 Docker 或系统服务日志。", 503)
        return result.stdout

    def _docker(self, *args, operation="Docker 操作", timeout=12, data=None, optional=False):
        return self._command(["docker", "--config", str(self.docker_config), "--host", "unix:///var/run/docker.sock", *args],
                             operation, timeout=timeout, data=data, optional=optional)

    @staticmethod
    def _manager():
        if Path("/run/systemd/system").is_dir() and shutil.which("systemctl", path=SYSTEM_PATH):
            return "systemd"
        if shutil.which("rc-service", path=SYSTEM_PATH):
            return "openrc"
        return None

    @staticmethod
    def _package_manager(manager):
        if manager == "openrc" and shutil.which("apk", path=SYSTEM_PATH):
            return "apk"
        if manager and shutil.which("apt-get", path=SYSTEM_PATH):
            return "apt-get"
        return None

    def _docker_running(self):
        return self._docker("info", "--format", "{{.ServerVersion}}", optional=True, timeout=5) is not None

    def _inspect_caddy(self):
        template = ('{"owner":{{json (index .Config.Labels "' + OWNER_LABEL
                    + '")}},"running":{{json .State.Running}},"mounts":{{json .Mounts}},"pid":{{json .State.Pid}}}')
        result = self._docker("container", "inspect", "--format", template, CADDY, optional=True, timeout=5)
        if result is None:
            return False, False, 0
        info = json.loads(result)
        if info["owner"] != "proxy" or not any(
            mount["Source"] == str(self.config_dir) and mount["Destination"] == "/etc/harbor-sites" and not mount["RW"]
            for mount in info["mounts"]
        ):
            return False, True, 0
        return True, bool(info["running"]), info["pid"]

    @staticmethod
    def _listening():
        try:
            return [connection for connection in psutil.net_connections(kind="tcp")
                    if connection.status == psutil.CONN_LISTEN and connection.laddr]
        except (OSError, psutil.Error) as exc:
            raise PanelError("无法读取本机 TCP 监听列表，请检查面板进程权限。", 503) from exc

    @staticmethod
    def _project(pid):
        if pid is not None:
            try:
                return psutil.Process(pid).name()
            except psutil.Error:
                pass
        return "未知进程"

    def _entry_ports(self, pid=0):
        return [{"port": row.laddr.port, "address": row.laddr.ip, "project": self._project(row.pid)}
                for row in self._listening() if row.laddr.port in (80, 443) and (not pid or row.pid != pid)]

    def environment(self):
        installed = bool(self.supported and shutil.which("docker", path=SYSTEM_PATH))
        running = installed and self._docker_running()
        caddy, active, pid = self._inspect_caddy() if running else (False, False, 0)
        manager = self._manager() if self.supported else None
        return {"supported": self.supported,
                "docker": {"installed": installed, "running": running, "manager": manager,
                           "package_manager": self._package_manager(manager)},
                "caddy": {"installed": caddy, "running": caddy and active},
                "ports": self._port_conflicts(pid, running) if self.supported else [], "job": self.overview()["job"]}

    def _containers(self, strict=False):
        result = self._docker("ps", "-q", optional=not strict, timeout=5)
        if not result:
            return []
        template = ('{"name":{{json .Name}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
                    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                    '"owner":{{json (index .Config.Labels "' + OWNER_LABEL + '")}},"ports":{{json .NetworkSettings.Ports}}}')
        containers = []
        ids = result.splitlines()
        if strict and len(ids) > 400:
            raise PanelError("正在运行的容器超过 400 个，无法可靠检查端口归属。", 503)
        ids = ids[:400]
        for start in range(0, len(ids), 50):
            batch = ids[start:start + 50]
            if not all(re.fullmatch(r"[0-9a-f]{12,64}", cid) for cid in batch):
                if strict:
                    raise PanelError("Docker 返回了无效的容器编号。", 503)
                continue
            details = self._docker("container", "inspect", "--format", template, *batch, optional=not strict, timeout=5)
            if details:
                containers.extend(json.loads(line) for line in details.splitlines())
        return containers

    @staticmethod
    def _bindings(container):
        for key, addresses in (container["ports"] or {}).items():
            number, _, protocol = key.partition("/")
            if protocol == "tcp":
                for binding in addresses or []:
                    host = binding["HostIp"]
                    if host in ("", "0.0.0.0"):
                        host = "127.0.0.1"
                    elif host == "::":
                        host = "::1"
                    yield int(number), host, int(binding["HostPort"])

    def _port_conflicts(self, pid, inspect_docker):
        busy = self._entry_ports(pid)
        if inspect_docker:
            for item in self._containers(strict=True):
                for _, host, port in self._bindings(item):
                    if port in (80, 443) and not any(row["port"] == port for row in busy):
                        busy.append({"port": port, "address": host, "project": item["name"].lstrip("/")})
        return busy

    def _check_entry_ports(self, pid):
        busy = self._port_conflicts(pid, True)
        if busy:
            hint = "、".join(f'{item["port"]} ({item["project"]})' for item in busy[:4])
            raise PanelError(f"80/443 已被现有服务占用：{hint}。面板不会停止或覆盖现有服务。", 409)

    def scan(self):
        self._require_supported()
        rows = {}
        for item in self._listening():
            host, port = item.laddr.ip, item.laddr.port
            if port in (80, 443, self.panel_port) or "%" in host:
                continue
            if host == "0.0.0.0":
                host = "127.0.0.1"
            elif host == "::":
                host = "::1"
            rows.setdefault((host, port), self._project(item.pid))
        for container in self._containers():
            project = "/".join(filter(None, (container["project"], container["service"]))) or container["name"].lstrip("/")
            for _, host, port in self._bindings(container):
                if container["owner"]:
                    rows.pop((host, port), None)
                elif port not in (80, 443, self.panel_port) and "%" not in host:
                    rows[host, port] = project
        ordered = sorted(rows.items(), key=lambda item: (item[0][1], item[0][0]))
        return {"services": [{"port": port, "address": host, "project": project, "upstream": authority(host, port)}
                             for (host, port), project in ordered[:self.MAX_SCAN]],
                "truncated": len(ordered) > self.MAX_SCAN}

    def set_environment(self, service, action, address):
        if service not in ("docker", "caddy") or action not in ("install", "start", "restart"):
            raise PanelError("请选择支持的环境服务和操作。")
        return self._submit("environment", f"{service}:{action}", address, self._set_environment, service, action)

    def _set_environment(self, service, action):
        if service == "docker":
            manager = self._manager()
            if not manager:
                raise PanelError("未找到 systemd 或 OpenRC，请在 VPS 上手动管理 Docker。", 503)
            if action == "install" and not shutil.which("docker", path=SYSTEM_PATH):
                package_manager = self._package_manager(manager)
                if package_manager == "apk":
                    self._command(["apk", "add", "docker"], "安装 Docker", timeout=600)
                elif package_manager == "apt-get":
                    self._command(["apt-get", "update"], "刷新软件源", timeout=300)
                    self._command(["apt-get", "install", "-y", "--no-install-recommends", "docker.io"], "安装 Docker", timeout=600)
                else:
                    raise PanelError("此发行版尚不支持面板内安装 Docker，请先在 VPS 上安装本地 Docker。", 503)
            verb = "restart" if action == "restart" else "start"
            if manager == "systemd":
                command = ["systemctl", verb, "docker.service"]
            else:
                command = ["rc-service", "docker", verb]
            self._command(command, f"{verb} Docker", timeout=120)
            for attempt in range(10):
                if self._docker_running():
                    return "本机 Docker 已就绪。", None
                time.sleep(0.3)
            raise PanelError("Docker 启动后未能连接本机 Docker socket。", 503)
        if not self._docker_running():
            raise PanelError("请先安装并启动本机 Docker。", 503)
        installed, running, pid = self._inspect_caddy()
        if running and not installed:
            raise PanelError("已有同名 Docker 容器，面板不会接管或覆盖。", 409)
        if installed and running and action in ("install", "start"):
            return "专用 Caddy 已经在运行。", None
        if not installed and action != "install":
            raise PanelError("请先安装面板专用 Caddy。", 409)
        self._check_entry_ports(pid)
        if not installed:
            self._install_caddy()
        else:
            self._docker("restart" if action == "restart" else "start", CADDY, operation="启动专用 Caddy", timeout=45)
        self._await_caddy()
        return "专用 Caddy 已就绪；证书申请将在创建站点后由 Let’s Encrypt 处理。", None

    def _install_caddy(self):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.config_dir.mkdir(mode=0o700, exist_ok=True)
        self.docker_config.mkdir(mode=0o700, exist_ok=True)
        if not self.config.exists():
            atomic_write(self.config, self._render([]))
        self._docker("pull", CADDY_IMAGE, operation="下载 Caddy 镜像", timeout=600)
        for suffix in ("data", "config"):
            name = f"harbor-panel-caddy-{suffix}"
            owner = self._docker("volume", "inspect", "--format", '{{index .Labels "' + OWNER_LABEL + '"}}', name, optional=True)
            if owner is not None and owner.strip() != "proxy":
                raise PanelError("检测到同名 Docker 数据卷，面板不会接管。", 409)
            self._docker("volume", "create", "--label", f"{OWNER_LABEL}=proxy", name, operation="创建 Caddy 数据卷")
        self._check_entry_ports(0)
        self._docker("run", "-d", "--name", CADDY, "--label", f"{OWNER_LABEL}=proxy", "--network", "host",
                     "--restart", "unless-stopped", "--cap-drop", "ALL", "--cap-add", "NET_BIND_SERVICE",
                     "--security-opt", "no-new-privileges", "-v", f"{self.config_dir}:/etc/harbor-sites:ro",
                     "-v", "harbor-panel-caddy-data:/data", "-v", "harbor-panel-caddy-config:/config",
                     CADDY_IMAGE, "caddy", "run", "--config", CADDYFILE, "--adapter", "caddyfile",
                     operation="安装专用 Caddy", timeout=45)

    def _await_caddy(self):
        for _ in range(15):
            if self._docker("exec", CADDY, "caddy", "reload", "--address", ADMIN_ADDRESS, "--config", CADDYFILE, "--adapter", "caddyfile",
                            optional=True, timeout=3) is not None:
                return
            time.sleep(0.4)
        raise PanelError("专用 Caddy 尚未就绪。请检查 Docker 中 harbor-panel-caddy 的日志。", 503)

    def _require_caddy(self):
        installed, running, pid = self._inspect_caddy()
        if not (installed and running):
            raise PanelError("请到「环境管理」安装并启动面板专用 Caddy。", 503)
        return pid

    def create(self, domain, kind, upstream, address):
        domain = domain_name(domain)
        if domain in self.panel_hosts:
            raise PanelError("该域名是 Harbor Panel 本身的访问域名，不能作为站点。")
        if kind not in ("wordpress", "proxy", "https"):
            raise PanelError("请选择有效的站点服务。")
        upstream = None if kind == "wordpress" else parse_upstream(upstream, kind)
        if upstream and (upstream.host == domain or (upstream.host in self.panel_hosts and
                         upstream.port in (80, 443, self.panel_port))):
            raise PanelError("上游不能是当前主域名或 Harbor Panel 本身。")
        return self._submit("create", domain, address, self._create, domain, kind, upstream)

    def action(self, site_id, action, address):
        if not isinstance(site_id, str) or not SITE_ID.fullmatch(site_id) or action not in ("enable", "disable", "remove"):
            raise PanelError("无效的网站或操作。")
        return self._submit(action, site_id, address, self._action, site_id, action)

    @staticmethod
    def _resolve(host, port):
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            pass
        try:
            return list(dict.fromkeys(ipaddress.ip_address(info[4][0].split("%")[0]) for info in
                                      socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except (OSError, ValueError) as exc:
            raise PanelError("无法解析上游。请检查域名、容器名或端口映射。") from exc

    def _target(self, upstream):
        target = upstream
        if upstream.host != "localhost" and CONTAINER_NAME.fullmatch(upstream.host) and not upstream.host.isnumeric():
            containers = self._containers()
            matches = [item for item in containers if item["name"].lstrip("/") == upstream.host]
            if not matches:
                matches = [item for item in containers if item["service"] == upstream.host]
            if len(matches) > 1:
                raise PanelError("该 Docker 服务名匹配多个容器，请改用唯一容器名或已发布的 IP:端口。")
            if matches:
                if matches[0]["owner"]:
                    raise PanelError("不可直接代理面板托管的内部容器。")
                ports = [(host, port) for inner, host, port in self._bindings(matches[0]) if inner == upstream.port]
                if not ports:
                    raise PanelError("该容器端口未发布到宿主机，请先映射到固定的 127.0.0.1 端口，或填写已发布的地址。")
                ports.sort(key=lambda item: (not item[0].startswith("127."), item[0] != "::1"))
                target = Upstream(upstream.scheme, *ports[0])
        local = {address.address.split("%")[0] for addresses in psutil.net_if_addrs().values()
                 for address in addresses if address.family in (socket.AF_INET, socket.AF_INET6)}
        if target.port in (self.panel_port, 2375, 2376, 6443, 10250, 2019):
            raise PanelError("该上游端口保留给面板或基础设施控制接口。")
        if target.port in (80, 443) and upstream.host in {site["domain"] for site in self.sites}:
            raise PanelError("不能将已有的面板站点入口用作上游。")
        addresses = self._resolve(target.host, target.port)
        for ip in addresses:
            checked = getattr(ip, "ipv4_mapped", None) or ip
            if (checked.is_unspecified or checked.is_multicast or checked.is_link_local or checked.is_reserved
                    or str(checked) in {"100.100.100.200", "168.63.129.16", "fd00:ec2::254"}
                    or (checked.version == 4 and (checked.packed[0] == 0 or str(checked) == "255.255.255.255"))):
                raise PanelError("上游不能使用保留、链路本地或云元数据地址。")
            if target.port in (80, 443) and (checked.is_loopback or str(checked) in local or str(ip) in local):
                raise PanelError("上游不能指向该 VPS 的网站入口，请填写应用的实际端口。")
        if not addresses:
            raise PanelError("未找到上游的 IPv4 或 IPv6 地址。")
        return Upstream(target.scheme, str(addresses[0]), target.port)

    def _create(self, domain, kind, upstream):
        pid = self._require_caddy()
        self._check_entry_ports(pid)
        if any(site["domain"] == domain for site in self.sites):
            raise PanelError("此主域名已存在。", 409)
        if len(self.sites) >= self.MAX_SITES:
            raise PanelError("站点数量已达 50 个。", 409)
        site_id = secrets.token_hex(8)
        if upstream is not None:
            target, credentials = self._target(upstream), None
        else:
            target, credentials = self._wordpress(site_id, domain)
        site = {"id": site_id, "domain": domain, "kind": kind, "upstream": upstream.url if upstream else None,
                "enabled": True, "target": target.url, "origin_host": upstream.host if upstream else None,
                "origin_port": upstream.port if upstream else None}
        try:
            self._check_entry_ports(self._require_caddy())
            self._apply([*self.sites, site])
        except Exception:
            if kind == "wordpress":
                self._remove_wp_containers(site_id)
            raise
        message = "站点已创建。请确认 DNS 和 80/443 可达；Let’s Encrypt 证书可能需要几分钟。"
        if kind == "wordpress":
            message += f" 可通过可信 SSH 读取状态目录 websites/wordpress/{site_id}/admin-login。"
        return message, credentials if self.secure else None

    def _action(self, site_id, action):
        pid = self._require_caddy()
        site = next((item for item in self.sites if item["id"] == site_id), None)
        if site is None:
            raise PanelError("未找到该站点。", 404)
        if action == "remove":
            self._apply([item for item in self.sites if item is not site])
            if site["kind"] == "wordpress":
                self._remove_wp_containers(site_id)
            return "已移除站点入口；WordPress 数据卷不会删除。", None
        updated = {**site, "enabled": action == "enable"}
        if updated["enabled"]:
            self._check_entry_ports(pid)
            if updated["upstream"]:
                updated["target"] = self._target(parse_upstream(updated["upstream"], updated["kind"])).url
        self._apply([updated if item is site else item for item in self.sites])
        return ("已启用站点入口。" if updated["enabled"] else "已停用站点入口；不会停止原应用。"), None

    def _apply(self, sites):
        previous = self.config.read_text(encoding="utf-8")
        candidate = self._render(sites)
        self._docker("exec", "-i", CADDY, "caddy", "validate", "--config", "-", "--adapter", "caddyfile",
                     data=candidate, operation="验证站点配置", timeout=30)
        atomic_write(self.config, candidate)
        try:
            self._docker("exec", CADDY, "caddy", "reload", "--address", ADMIN_ADDRESS, "--config", CADDYFILE, "--adapter", "caddyfile",
                         operation="加载站点配置", timeout=30)
        except PanelError:
            atomic_write(self.config, previous)
            self._docker("exec", CADDY, "caddy", "reload", "--address", ADMIN_ADDRESS, "--config", CADDYFILE, "--adapter", "caddyfile", optional=True)
            raise
        with self.lock:
            self.sites = sites

    @staticmethod
    def _render(sites):
        lines = [STATE_PREFIX + json.dumps(sites, ensure_ascii=True, separators=(",", ":")), "{",
                 f"    admin {ADMIN_ADDRESS}", "}"]
        for site in sites:
            if not site["enabled"]:
                continue
            lines += ["", f'{site["domain"]} {{', "    tls {", "        issuer acme {", f"            dir {LE_DIRECTORY}",
                      "        }", "    }"]
            if site["origin_host"]:
                target = parse_upstream(site["target"], "proxy")
                host, port = site["origin_host"], site["origin_port"]
                default_port = 443 if target.scheme == "https" else 80
                virtual_host = authority(host, port) if port != default_port else (f"[{host}]" if ":" in host else host)
                lines += [f"    reverse_proxy {target.url} {{", f"        header_up Host {virtual_host}"]
                if target.scheme == "https":
                    lines += ["        transport http {", f"            tls_server_name {host}", "        }"]
                lines.append("    }")
            else:
                lines.append(f'    reverse_proxy {site["target"]}')
            lines.append("}")
        return "\n".join([*lines, ""])

    def _wordpress(self, site_id, domain):
        prefix = f"harbor-wp-{site_id}"
        private = self.directory / "wordpress" / site_id
        private.parent.mkdir(mode=0o700, exist_ok=True)
        private.mkdir(mode=0o700)
        password = secrets.token_urlsafe(27)
        database_password, root_password = (secrets.token_urlsafe(32) for _ in range(2))
        db_environment, wp_environment = private / "database.env", private / "wordpress.env"
        label = f"{OWNER_LABEL}={site_id}"
        try:
            atomic_write(db_environment, f"MARIADB_DATABASE=wordpress\nMARIADB_USER=wordpress\nMARIADB_PASSWORD={database_password}\nMARIADB_ROOT_PASSWORD={root_password}\n")
            extra = f"define('WP_HOME', 'https://{domain}'); define('WP_SITEURL', 'https://{domain}');"
            atomic_write(wp_environment, f"WORDPRESS_DB_HOST=db:3306\nWORDPRESS_DB_NAME=wordpress\nWORDPRESS_DB_USER=wordpress\nWORDPRESS_DB_PASSWORD={database_password}\nWORDPRESS_CONFIG_EXTRA={extra}\n")
            for image, desc in ((DB_IMAGE, "MariaDB"), (WP_IMAGE, "WordPress")):
                self._docker("pull", image, operation=f"下载 {desc} 镜像", timeout=600)
            self._docker("network", "create", "--label", label, prefix, operation="创建站点 Docker 网络")
            self._docker("run", "-d", "--name", prefix + "-db", "--label", label, "--network", prefix,
                         "--network-alias", "db", "--restart", "unless-stopped", "--env-file", str(db_environment),
                         "-v", f"{prefix}-database:/var/lib/mysql", DB_IMAGE, operation="创建 MariaDB", timeout=60)
            shared = ("--label", label, "--network", prefix, "--env-file", str(wp_environment),
                      "-v", f"{prefix}-files:/var/www/html")
            # The official entrypoint copies WordPress for Apache commands; -t exits without serving HTTP.
            self._docker("run", "--rm", *shared, WP_IMAGE, "apache2ctl", "-t", operation="准备 WordPress 文件", timeout=60)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if self._docker("exec", prefix + "-db", "healthcheck.sh", "--connect", "--innodb_initialized",
                                optional=True, timeout=3) is not None:
                    break
                time.sleep(1)
            else:
                raise PanelError("MariaDB 未能完成初始化，请检查 VPS 资源和 Docker 日志。", 503)
            php = """<?php
            $_SERVER['HTTPS'] = 'on';
            define('WP_INSTALLING', true);
            require '/var/www/html/wp-load.php';
            require_once ABSPATH . 'wp-admin/includes/upgrade.php';
            if (is_blog_installed()) { exit(1); }
            add_filter('pre_wp_mail', '__return_false');
            wp_install(%s, %s, %s, true, '', %s);
            $user = get_user_by('login', %s);
            if (!$user || !user_can($user, 'manage_options')) { exit(1); }
            echo 'HARBOR_WP_READY';
            """ % tuple(json.dumps(value) for value in (domain, self.ADMIN, f"wordpress@{domain}", password, self.ADMIN))
            initialized = self._docker("run", "--rm", "-i", "--user", "www-data", *shared, "--entrypoint", "php",
                                       WP_IMAGE, data=php, operation="初始化 WordPress 管理员", timeout=60)
            if "HARBOR_WP_READY" not in initialized:
                raise PanelError("WordPress 管理员未能初始化；没有公开站点。", 503)
            atomic_write(private / "admin-login", f"URL=https://{domain}/wp-admin/\nUsername={self.ADMIN}\nPassword={password}\n")
            self._docker("run", "-d", "--name", prefix, *shared, "--restart", "unless-stopped", "-p", "127.0.0.1::80",
                         WP_IMAGE, operation="启动已初始化的 WordPress", timeout=60)
            published = self._docker("port", prefix, "80/tcp", operation="读取 WordPress 本机端口")
            if not re.fullmatch(r"127\.0\.0\.1:\d{1,5}\n?", published):
                raise PanelError("WordPress 没有映射到预期的回环端口；没有公开站点。", 503)
            return parse_upstream(published.strip(), "proxy"), {"username": self.ADMIN, "password": password}
        except Exception:
            self._remove_wp_containers(site_id)
            raise
        finally:
            db_environment.unlink(missing_ok=True)
            wp_environment.unlink(missing_ok=True)

    def _remove_wp_containers(self, site_id):
        prefix = f"harbor-wp-{site_id}"
        template = '{{index .Config.Labels "' + OWNER_LABEL + '"}}'
        for name in (prefix, prefix + "-db"):
            owner = self._docker("container", "inspect", "--format", template, name, optional=True)
            if owner and owner.strip() == site_id:
                self._docker("rm", "-f", name, optional=True, timeout=30)
        network_template = '{{index .Labels "' + OWNER_LABEL + '"}}'
        owner = self._docker("network", "inspect", "--format", network_template, prefix, optional=True)
        if owner and owner.strip() == site_id:
            self._docker("network", "rm", prefix, optional=True)
