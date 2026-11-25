"""Native menu HTTPS aliases with private Certbot state and an owned renewal timer."""

import argparse
from contextlib import contextmanager
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import time
from urllib.request import urlopen

if __name__ == "__main__" and sys.flags.isolated:
    sys.path.insert(0, str(Path(__file__).absolute().parent))

from files import PanelError
import host_settings as hs
from sites import SiteManager, SYSTEM_PATH, domain_name


CONF_DIR = Path("/etc/nginx/conf.d")
ACME_ROOT = Path("/run/harbor-panel-acme")
SERVICE = Path("/etc/systemd/system/harbor-panel-certificates.service")
TIMER = SERVICE.with_suffix(".timer")
LE_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"


def certificate_name(domain):
    return "harbor-panel-" + hashlib.sha256(domain.encode("ascii")).hexdigest()[:24]


def config_path(domain):
    return CONF_DIR / (certificate_name(domain) + ".conf")


def load(state):
    record = Path(state) / "panel-domains.json"
    if not os.path.lexists(record):
        return []
    try:
        data = json.loads(hs._read_file(record))
    except FileNotFoundError:
        return []
    if (not isinstance(data, list) or len(data) > 50
            or any(not isinstance(item, dict) or set(item) != {"domain", "enabled", "config_sha256"}
                   or domain_name(item["domain"]) != item["domain"] or type(item["enabled"]) is not bool
                   or item["config_sha256"] != hashlib.sha256(render(item["domain"], Path(state)).encode()).hexdigest()
                   for item in data)
            or len({item["domain"] for item in data}) != len(data)):
        raise PanelError("面板域名记录无效，请检查 panel-domains.json。", 409)
    return data


def origins(state):
    return [f"https://{item['domain']}" for item in load(state) if item["enabled"]]


def health(state, nonce):
    record = Path(state) / "panel-domain-probe.json"
    if not os.path.lexists(record):
        return None
    try:
        data = json.loads(hs._read_file(record))
    except FileNotFoundError:
        return None
    if (isinstance(nonce, str) and hs.HEX_TOKEN.fullmatch(nonce) and data["expires"] > time.time()
            and hmac.compare_digest(data["nonce"], nonce)):
        return {"proof": data["proof"]}
    return None


def timer_text():
    return ("# Harbor Panel managed certificate renewal\n[Unit]\nDescription=Renew Harbor Panel HTTPS certificates\n"
            "[Timer]\nOnCalendar=*-*-* 03,15:00:00\nRandomizedDelaySec=3600\nPersistent=true\n"
            "[Install]\nWantedBy=timers.target\n")


def service_text(app, state):
    return ("# Harbor Panel managed certificate renewal\n[Unit]\nDescription=Renew Harbor Panel HTTPS certificates\n"
            "After=network-online.target nginx.service\nWants=network-online.target\n"
            "[Service]\nType=oneshot\nUser=root\nUMask=0077\n"
            f"ExecStart={app}/.venv/bin/python -IB {app}/panel_domains.py renew {app} {state}\n")


def render(domain, state, *, tls=True):
    name = certificate_name(domain)
    text = ("# Harbor Panel managed HTTPS domain\nserver {\n    listen 80;\n    listen [::]:80;\n"
            f"    server_name {domain};\n"
            "    location ^~ /.well-known/acme-challenge/ {\n"
            f"        root {ACME_ROOT};\n        default_type text/plain;\n    }}\n")
    if tls:
        text += f"    location / {{ return 301 https://{domain}$request_uri; }}\n}}\n"
        text += ("server {\n    listen 443 ssl;\n    listen [::]:443 ssl;\n"
                 f"    server_name {domain};\n"
                 f"    ssl_certificate {state}/tls/config/live/{name}/fullchain.pem;\n"
                 f"    ssl_certificate_key {state}/tls/config/live/{name}/privkey.pem;\n"
                 "    ssl_protocols TLSv1.2 TLSv1.3;\n    ssl_session_cache shared:HarborTLS:10m;\n")
    text += ("    location / {\n"
             + (f"        include {hs.NGINX_FILE};\n" if tls else "        return 503;\n"))
    if tls:
        text += ("        proxy_http_version 1.1;\n        proxy_set_header Host $http_host;\n"
                 "        proxy_set_header X-Forwarded-Proto https;\n"
                 "        proxy_set_header Upgrade $http_upgrade;\n"
                 '        proxy_set_header Connection "upgrade";\n'
                 "        proxy_buffering off;\n        proxy_read_timeout 3600s;\n"
                 "        client_max_body_size 2m;\n")
    return text + "    }\n}\n"


class DomainManager:
    def __init__(self, app, state, operations=None):
        self.app, self.state = Path(app), Path(state)
        self.record = self.state / "panel-domains.json"
        self.ops = operations or hs.HostOperations()

    def _installed(self):
        from deploy.menu_admin import Installation, Unsafe
        try:
            Installation(str(self.app), str(self.state)).ownership()
        except Unsafe as exc:
            raise PanelError(f"无法验证面板安装：{exc}", 409) from exc
        if not self.ops.available():
            raise PanelError("域名管理需要已安装并运行的原生 Linux root systemd 面板。", 403)
        text = hs._read_file(hs.PORT_FILE)
        import re
        match = re.fullmatch(r"HARBOR_PANEL_PORT=([0-9]{1,5})\n?", text)
        if not match:
            raise PanelError("无法读取已安装面板的监听端口。", 409)
        return hs._port(int(match[1]))

    @contextmanager
    def _locked(self, *, require_running=True):
        port = self._installed()
        settings = hs.HostSettings(self.state, full_host=True, panel_port=port, public_origin=None, operations=self.ops)
        with settings._lock("host-settings.worker.lock", nonblocking=True), settings._lock():
            if any(job["status"] in hs.ACTIVE | {"rollback_failed"} for job in settings._jobs()):
                raise PanelError("请先完成或恢复正在进行的端口切换。", 409)
            unit = self.ops.unit(hs.PANEL_UNIT, "ActiveState", "NeedDaemonReload", "Environment")
            if (require_running and unit.get("ActiveState") != "active") or unit.get("NeedDaemonReload") != "no":
                raise PanelError("请先启动 Harbor Panel 服务。", 409)
            # Manual public-origin deployments have different access semantics; never silently replace them.
            env_file = Path("/etc/harbor-panel.env")
            try:
                env = hs._read_file(env_file)
            except FileNotFoundError:
                env = ""
            if "HARBOR_PANEL_PUBLIC_ORIGIN=" in env or "HARBOR_PANEL_PUBLIC_ORIGIN=" in unit.get("Environment", ""):
                raise PanelError("请先迁移手动配置的 HARBOR_PANEL_PUBLIC_ORIGIN，再使用自动域名管理。", 409)
            yield port

    def _save(self, entries):
        hs._atomic_write(self.record, json.dumps(entries))

    def _entry(self, domain, enabled):
        return {"domain": domain, "enabled": enabled,
                "config_sha256": hashlib.sha256(render(domain, self.state).encode()).hexdigest()}

    def _legacy(self):
        from panel_proxy import CONFIG, PanelProxy
        panel = PanelProxy(self.state, None, enabled=True)
        if panel.site and panel.site["enabled"] and hs._read_file(CONFIG) != panel.render(panel.site["domain"]):
            raise PanelError("原 HTTP 入口已被修改，不会覆盖。", 409)
        return panel

    def list_domains(self):
        entries = load(self.state)
        legacy = self._legacy()
        return entries + ([{"domain": legacy.site["domain"], "enabled": legacy.site["enabled"], "legacy": True}]
                          if legacy.site else [])

    def _dependencies(self):
        missing = [name for name in ("nginx", "certbot") if not shutil.which(name, path=SYSTEM_PATH)]
        if missing:
            manager = SiteManager._package_manager("systemd")
            if manager == "apt-get":
                self.ops.command(manager, "update", timeout=300)
                self.ops.command(manager, "install", "-y", "--no-install-recommends", *missing, timeout=600)
            elif manager in ("dnf", "yum"):
                self.ops.command(manager, "install", "-y", *missing, timeout=600)
            else:
                raise PanelError("请先安装 nginx 和 certbot，再重试域名管理。", 503)
        for name in ("nginx", "certbot"):
            if not shutil.which(name, path=SYSTEM_PATH):
                raise PanelError(f"{name} 安装未完成，请检查软件源后重试。", 503)

    @staticmethod
    def _dns(domain):
        answers = {ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)}
        local = {ipaddress.ip_address(row.address.split("%")[0])
                 for group in hs.psutil.net_if_addrs().values() for row in group
                 if row.family in (socket.AF_INET, socket.AF_INET6)}
        if not answers or any(not address.is_global for address in answers):
            raise PanelError("域名必须解析到本机公网 IP；不支持内网地址、CDN 或代理解析。", 409)
        if not answers <= local:
            # A VPS behind NAT may not have its public address on a local interface.
            for url in ("https://api.ipify.org", "https://api6.ipify.org"):
                try:
                    with urlopen(url, timeout=5) as response:
                        local.add(ipaddress.ip_address(response.read(64).decode("ascii").strip()))
                except (OSError, ValueError):
                    continue
            if not answers <= local:
                raise PanelError("域名的全部 A / AAAA 记录必须指向本机公网 IP，请检查 DNS 后重试。", 409)

    def _certbot(self, *arguments):
        base = self.state / "tls"
        base.mkdir(mode=0o700, exist_ok=True)
        hs._directory(base)
        mask = os.umask(0o022)
        try:
            result = self.ops.command("certbot", *arguments, "--config-dir", str(base / "config"),
                                      "--work-dir", str(base / "work"), "--logs-dir", str(base / "logs"),
                                      "--non-interactive", timeout=600, check=False)
        finally:
            os.umask(mask)
        if result.returncode:
            raise PanelError(f"Certbot 未完成；请检查 DNS、TCP 80 入站及 {base / 'logs'} 中的日志。", 409)
        return result

    @staticmethod
    def _challenge_root():
        try:
            ACME_ROOT.mkdir(mode=0o755)
        except FileExistsError:
            if hs._read_file(ACME_ROOT / ".harbor-panel-acme") != "Harbor Panel ACME webroot\n":
                raise PanelError("ACME 验证目录归属不明，不会覆盖。", 409)
        else:
            hs._atomic_write(ACME_ROOT / ".harbor-panel-acme", "Harbor Panel ACME webroot\n")
        hs._directory(ACME_ROOT)
        os.chmod(ACME_ROOT, 0o755)

    def _reload(self, *, start=False):
        dump = self.ops.command("nginx", "-T").stdout
        if start:
            self.ops.command("systemctl", "enable", "nginx.service")
            self.ops.command("systemctl", "reload-or-restart", "nginx.service")
        else:
            status = self.ops.command("systemctl", "is-active", "nginx.service", check=False).returncode
            if status == 0:
                self.ops.command("systemctl", "reload", "nginx.service")
            elif status != 3:
                raise PanelError("无法确认 Nginx 服务状态，请检查 systemd。", 409)
        return dump

    def _remove_unused_upstream(self, port):
        if f"# configuration file {hs.NGINX_FILE}:" in self.ops.command("nginx", "-T").stdout.splitlines():
            return
        try:
            text = hs._read_file(hs.NGINX_FILE)
        except FileNotFoundError:
            return
        if text != hs._nginx_file(port):
            raise PanelError("面板上游片段已改变，请人工检查残留配置。", 409)
        hs.NGINX_FILE.unlink()

    def _delete_certificate(self, domain):
        renewal = self.state / "tls" / "config" / "renewal" / (certificate_name(domain) + ".conf")
        if os.path.lexists(renewal):
            self._certbot("delete", "--cert-name", certificate_name(domain))

    @staticmethod
    def _certificate_referenced(domain, dump):
        name = certificate_name(domain)
        return any(f"/{kind}/{name}/" in dump for kind in ("live", "archive"))

    def _timer(self, enabled):
        expected = {SERVICE: service_text(self.app, self.state), TIMER: timer_text()}
        existing = False
        for path, text in expected.items():
            try:
                current = hs._read_file(path)
            except FileNotFoundError:
                current = None
            if current is not None and current != text:
                raise PanelError(f"{path} 已被修改，不会覆盖。", 409)
            existing |= current is not None
        if enabled:
            for path, text in expected.items():
                hs._atomic_write(path, text)
            self.ops.command("systemctl", "daemon-reload")
            self.ops.command("systemctl", "enable", "--now", TIMER.name)
        elif existing:
            self.ops.command("systemctl", "disable", "--now", TIMER.name)
            for path in expected:
                path.unlink(missing_ok=True)
            self.ops.command("systemctl", "daemon-reload")

    def _verify(self, domain):
        probe = {"nonce": secrets.token_hex(16), "proof": secrets.token_hex(32), "expires": time.time() + 60}
        path = self.state / "panel-domain-probe.json"
        hs._atomic_write(path, json.dumps(probe))
        try:
            # Use local TLS with SNI and certificate validation, independent of NAT hairpin routing.
            endpoint = (socket.AF_INET, ("127.0.0.1", 443))
            from http.client import HTTPException
            import ssl

            def request():
                conn = hs._LocalHTTPSConnection(domain, 443, endpoint, ssl.create_default_context())
                try:
                    conn.request("GET", f"{hs.HEALTH_PATH}?nonce={probe['nonce']}")
                    response = conn.getresponse()
                    return response.status == 200 and json.loads(response.read(256)) == {"proof": probe["proof"]}
                except (OSError, HTTPException, ValueError):
                    return False
                finally:
                    conn.close()

            hs.HostSettings._wait(request, "HTTPS 未连接到当前面板，域名配置已恢复。")
        finally:
            path.unlink(missing_ok=True)

    def add(self, value):
        domain = domain_name(value)
        with self._locked() as port:
            entries = load(self.state)
            if any(item["domain"] == domain for item in entries):
                raise PanelError("此域名已有记录，请先删除再添加。", 409)
            if len(entries) >= 50:
                raise PanelError("最多支持 50 个面板访问域名。", 409)
            self._dns(domain)
            # Reject occupied ingress before installing packages, which can start their own services.
            for row in SiteManager._listening():
                if row.laddr.port in (80, 443) and SiteManager._project(row.pid) != "nginx":
                    raise PanelError("TCP 80/443 被其他服务占用，请先处理入口冲突。", 409)
            self._dependencies()
            CONF_DIR.mkdir(mode=0o755, exist_ok=True)
            hs._directory(CONF_DIR)
            path = config_path(domain)
            if path.exists() or path.is_symlink():
                raise PanelError("域名配置已存在，不会覆盖。", 409)
            dump = self.ops.command("nginx", "-T").stdout
            legacy = self._legacy()
            migrating = legacy.site and legacy.site["domain"] == domain
            from panel_proxy import CONFIG
            old_legacy = hs._read_file(CONFIG) if migrating and legacy.site["enabled"] else None
            old_record = hs._read_file(legacy.record) if migrating else None
            if old_legacy:
                dump = dump.replace(old_legacy, "")
            import re
            if any(domain in names.split() for names in re.findall(r"(?m)^\s*server_name\s+([^;]+);", dump)):
                raise PanelError("此域名已有 Nginx 配置，请先移除原入口。", 409)
            try:
                upstream = hs._read_file(hs.NGINX_FILE)
            except FileNotFoundError:
                upstream = None
            if upstream not in (None, hs._nginx_file(port)):
                raise PanelError("面板上游片段与当前端口不一致，请先检查。", 409)
            if upstream is not None and not entries and not legacy.site:
                raise PanelError("检测到手动管理的面板上游片段，请先迁移原配置。", 409)
            self._save(entries)
            self._challenge_root()
            issued = False
            try:
                if migrating:
                    CONFIG.unlink(missing_ok=True)
                    legacy.record.unlink()
                hs._atomic_write(path, render(domain, self.state, tls=False))
                if upstream is None:
                    hs._atomic_write(hs.NGINX_FILE, hs._nginx_file(port))
                loaded = self._reload(start=True)
                if f"# configuration file {path}:" not in loaded.splitlines():
                    raise PanelError("Nginx 未加载 conf.d/*.conf，无法申请证书。", 409)
                self._certbot("certonly", "--webroot", "-w", str(ACME_ROOT), "--server", LE_DIRECTORY,
                              "--agree-tos", "--register-unsafely-without-email",
                              "--cert-name", certificate_name(domain), "-d", domain, "--keep-until-expiring")
                issued = True
                hs._atomic_write(path, render(domain, self.state))
                self._reload(start=True)
                self._save(entries + [self._entry(domain, True)])
                self._verify(domain)
                self._timer(True)
            except BaseException:
                path.unlink(missing_ok=True)
                if upstream is None:
                    hs.NGINX_FILE.unlink(missing_ok=True)
                issued |= os.path.lexists(self.state / "tls" / "config" / "renewal" / (certificate_name(domain) + ".conf"))
                # Preserve an issued certificate for explicit retry/removal if later setup fails.
                self._save(entries + ([self._entry(domain, False)] if issued and not migrating else []))
                if migrating:
                    hs._atomic_write(legacy.record, old_record)
                    if old_legacy:
                        hs._atomic_write(CONFIG, old_legacy)
                self._reload()
                raise
            return f"已添加 https://{domain}；证书由 Let's Encrypt 签发并自动续期。"

    def remove(self, value):
        domain = domain_name(value)
        with self._locked(require_running=False) as port:
            entries = load(self.state)
            item = next((item for item in entries if item["domain"] == domain), None)
            if item is None:
                legacy = self._legacy()
                if not legacy.site or legacy.site["domain"] != domain:
                    raise PanelError("没有此面板访问域名。", 404)
                from panel_proxy import CONFIG
                original = hs._read_file(CONFIG) if legacy.site["enabled"] else None
                CONFIG.unlink(missing_ok=True)
                try:
                    self._reload()
                except BaseException:
                    if original:
                        hs._atomic_write(CONFIG, original)
                    self._reload()
                    raise
                hs._atomic_write(legacy.record, json.dumps({**legacy.site, "enabled": False}))
                if self._certificate_referenced(domain, self.ops.command("nginx", "-T").stdout):
                    raise PanelError("其他配置仍引用此证书，请先处理。", 409)
                self._delete_certificate(domain)
                legacy.record.unlink()
                self._save(entries)
                self._remove_unused_upstream(port)
                return f"已删除原 HTTP 域名入口 {domain}。"
            path = config_path(domain)
            original = hs._read_file(path) if item["enabled"] else None
            if original is not None and original != render(domain, self.state):
                raise PanelError("域名配置已被修改，不会删除。", 409)
            remaining = [entry for entry in entries if entry != item]
            if not item["enabled"] and (path.exists() or path.is_symlink()):
                raise PanelError("待清理域名存在未知配置文件，请先人工检查。", 409)
            if item["enabled"]:
                path.unlink()
                try:
                    dump = self.ops.command("nginx", "-T").stdout
                    if self._certificate_referenced(domain, dump):
                        raise PanelError("其他 Nginx 配置仍引用此证书，请先处理。", 409)
                    self._reload()
                except BaseException:
                    hs._atomic_write(path, original)
                    self._reload()
                    raise
            elif self._certificate_referenced(domain, self.ops.command("nginx", "-T").stdout):
                raise PanelError("其他 Nginx 配置仍引用此证书，请先处理。", 409)
            self._save(remaining + [{**item, "enabled": False}])
            self._delete_certificate(domain)
            if not remaining:
                self._timer(False)
            self._save(remaining)
            self._remove_unused_upstream(port)
            return f"已删除 {domain} 的入口及专用证书；其他域名和 IP:端口访问不变。"

    def renew(self):
        with self._locked(require_running=False):
            entries = [item for item in load(self.state) if item["enabled"]]
            if not entries:
                return
            self._challenge_root()
            for item in entries:
                if hs._read_file(config_path(item["domain"])) != render(item["domain"], self.state):
                    raise PanelError("域名配置已修改，续期已停止，请检查 Nginx。", 409)
                self._certbot("renew", "--cert-name", certificate_name(item["domain"]),
                              "--webroot", "-w", str(ACME_ROOT))
            self._reload()

    def menu(self):
        self._installed()
        while True:
            print("\nHarbor Panel 域名管理\n1. 继续添加访问域名\n2. 删除域名\n0. 返回上级菜单")
            try:
                choice = input("请选择: ").strip()
                if choice == "0":
                    return
                if choice not in ("1", "2"):
                    print("无效选项，请输入 1、2 或 0。")
                    continue
                if choice == "2":
                    entries = self.list_domains()
                    if not entries:
                        print("尚未添加面板访问域名。")
                        continue
                    for item in entries:
                        print(f"  {item['domain']}" + ("（原 HTTP 入口）" if item.get("legacy") else
                              "" if item["enabled"] else "（待清理证书）"))
                    value = input("请输入要删除的域名（直接回车取消）: ").strip()
                else:
                    print("请将域名全部 A / AAAA 记录指向本机公网 IP，并放行 TCP 80 和 443。")
                    print("回车后将使用 Let's Encrypt 自动申请证书并开启自动续期。")
                    value = input("请输入访问域名（不含协议或路径，直接回车取消）: ").strip()
                if value:
                    print(self.add(value) if choice == "1" else self.remove(value))
            except EOFError:
                return
            except (PanelError, OSError, ValueError) as exc:
                print(f"操作失败：{exc}。请检查 DNS、端口和服务日志后重试。", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("menu", "renew"))
    parser.add_argument("app", type=Path)
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    manager = DomainManager(args.app, args.state)
    getattr(manager, args.operation)()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (PanelError, OSError, ValueError) as error:
        print(f"域名管理失败：{error}", file=sys.stderr)
        sys.exit(1)
