"""Owned Nginx HTTP entry for the panel's native listener."""

import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil

from files import PanelError


CONFIG = Path("/etc/nginx/conf.d/harbor-panel.conf")


class PanelProxy:
    def __init__(self, state, port, enabled=False, changed=None):
        self.state = Path(state)
        self.record = self.state / "panel-domain.json"
        self.port = port
        self.enabled = enabled
        self.changed = changed
        self.site = None
        self.probe = None
        if enabled and self.record.exists():
            from host_settings import _read_file
            from sites import domain_name, SITE_ID
            self.site = json.loads(_read_file(self.record))
            if (set(self.site) != {"id", "domain", "enabled", "config_sha256"} or not SITE_ID.fullmatch(self.site["id"])
                    or domain_name(self.site["domain"]) != self.site["domain"] or type(self.site["enabled"]) is not bool
                    or self.site["config_sha256"] != hashlib.sha256(self.render(self.site["domain"]).encode()).hexdigest()):
                raise ValueError("Invalid managed panel domain.")

    def public(self):
        if self.site is None:
            return None
        return {"id": self.site["id"], "domain": self.site["domain"], "enabled": self.site["enabled"],
                "kind": "proxy", "engine": "nginx", "panel": True,
                "upstream": f"http://127.0.0.1:{self.port}", "url": f"http://{self.site['domain']}"}

    @staticmethod
    def render(domain):
        return (
            "# Harbor Panel managed HTTP entry\n"
            "server {\n    listen 80;\n    listen [::]:80;\n"
            f"    server_name {domain};\n"
            "    location / {\n"
            "        include /etc/harbor-panel.nginx-upstream;\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_set_header Host $http_host;\n"
            "        proxy_set_header X-Forwarded-Proto http;\n"
            "        proxy_set_header Upgrade $http_upgrade;\n"
            '        proxy_set_header Connection "upgrade";\n'
            "        proxy_buffering off;\n        proxy_read_timeout 3600s;\n"
            "    }\n}\n"
        )

    def create(self, domain):
        if self.site is not None:
            raise PanelError("面板已有 Nginx 域名入口，请先移除原入口。", 409)
        return self._apply({"id": secrets.token_hex(8), "domain": domain, "enabled": True,
                            "config_sha256": hashlib.sha256(self.render(domain).encode()).hexdigest()})

    def action(self, action):
        if self.site is None:
            raise PanelError("未找到面板域名入口。", 404)
        return self._apply(None if action == "remove" else {**self.site, "enabled": action == "enable"})

    def _apply(self, candidate):
        from host_settings import (ACTIVE, HostSettings, NGINX_FILE, _atomic_write, _nginx_file,
                                   _read_file, _sync_dir)
        from sites import SiteManager, SYSTEM_PATH

        if not self.enabled:
            raise PanelError("面板域名入口需要原生 Linux root systemd 部署。", 403)
        settings = HostSettings(self.state, full_host=True, panel_port=self.port, public_origin=None)
        settings._require()
        with settings._lock("host-settings.worker.lock", nonblocking=True), settings._lock():
            jobs = settings._jobs()
            if jobs and jobs[-1]["status"] in ACTIVE | {"rollback_failed"}:
                raise PanelError("请先完成或恢复正在进行的端口设置。", 409)
            settings._preflight_panel(self.port, require_current_process=True)
            old = self.site
            expected = self.render(old["domain"]) if old and old["enabled"] else None
            before = {}
            for path in (CONFIG, NGINX_FILE, self.record):
                try:
                    before[path] = _read_file(path)
                except FileNotFoundError:
                    before[path] = None
            if before[CONFIG] != expected or (before[NGINX_FILE] not in (None, _nginx_file(self.port))):
                raise PanelError("Nginx 面板配置已被其他程序修改，不会覆盖。", 409)
            if old is None and before[NGINX_FILE] is not None:
                raise PanelError("检测到已有的面板 Nginx 片段，不会接管手动配置。", 409)
            if old and before[self.record] != json.dumps(old):
                raise PanelError("面板域名记录已改变，请刷新后重试。", 409)
            command = SiteManager._command
            if not shutil.which("nginx", path=SYSTEM_PATH):
                package = SiteManager._package_manager("systemd")
                if package == "apt-get":
                    command(["apt-get", "update"], "刷新软件源", timeout=300)
                    command(["apt-get", "install", "-y", "--no-install-recommends", "nginx"], "安装 Nginx", timeout=600)
                elif package in ("dnf", "yum"):
                    command([package, "install", "-y", "nginx"], "安装 Nginx", timeout=600)
                else:
                    raise PanelError("请先在本机安装 Nginx。", 503)
            CONFIG.parent.mkdir(mode=0o755, exist_ok=True)
            dump = command(["nginx", "-T"], "检查 Nginx 配置")
            if candidate and candidate["enabled"]:
                remaining = dump.replace(expected or "\0", "")
                for names in re.findall(r"(?m)^\s*server_name\s+([^;]+);", remaining):
                    if candidate["domain"] in names.split():
                        raise PanelError("该域名已存在于其他 Nginx 配置中。", 409)
                for row in SiteManager._listening():
                    if row.laddr.port == 80 and SiteManager._project(row.pid) != "nginx":
                        raise PanelError("TCP 80 被其他服务占用，请先处理入口端口冲突。", 409)
            updated = {
                CONFIG: self.render(candidate["domain"]) if candidate and candidate["enabled"] else None,
                NGINX_FILE: _nginx_file(self.port) if candidate and candidate["enabled"] else None,
                self.record: json.dumps(candidate) if candidate else None,
            }
            changed = []
            try:
                for path, text in updated.items():
                    if text != before[path]:
                        if text is None:
                            path.unlink(missing_ok=True)
                            _sync_dir(path.parent)
                        else:
                            _atomic_write(path, text)
                        changed.append(path)
                loaded = command(["nginx", "-T"], "验证 Nginx 面板入口")
                if candidate and candidate["enabled"] and f"# configuration file {CONFIG}:" not in loaded.splitlines():
                    raise PanelError("Nginx 主配置未加载 conf.d/*.conf；未启用域名入口。", 409)
                settings.ops.command("systemctl", "enable", "nginx.service")
                settings.ops.command("systemctl", "reload-or-restart", "nginx.service")
                self.site = candidate
                if self.changed:
                    self.changed()
                if candidate and candidate["enabled"]:
                    self._probe(candidate["domain"])
            except Exception:
                self.site = old
                if self.changed:
                    self.changed()
                for path in reversed(changed):
                    if before[path] is None:
                        path.unlink(missing_ok=True)
                        _sync_dir(path.parent)
                    else:
                        _atomic_write(path, before[path])
                settings.ops.reload_nginx()
                raise
            message = (f"面板 Nginx 入口{'已启用' if candidate['enabled'] else '已停用'}：http://{candidate['domain']}。请将 DNS 指向本机，并开放 TCP 80。此入口为 HTTP；Passkey 仍需要 HTTPS 或 localhost。"
                       if candidate else "面板 Nginx 域名入口已移除，仍可通过 IP:端口访问。")
            return message, None

    def _probe(self, domain):
        from http.client import HTTPConnection, HTTPException
        from host_settings import HEALTH_PATH, HostSettings

        self.probe = {"nonce": secrets.token_hex(16), "proof": secrets.token_hex(32)}
        expected = {"proof": self.probe["proof"]}

        def request():
            connection = HTTPConnection("127.0.0.1", 80, timeout=2)
            try:
                connection.request("GET", f"{HEALTH_PATH}?nonce={self.probe['nonce']}", headers={"Host": domain})
                response = connection.getresponse()
                return response.status == 200 and json.loads(response.read(256)) == expected
            except (OSError, HTTPException, ValueError):
                return False
            finally:
                connection.close()

        try:
            HostSettings._wait(request, "Nginx 入口未能连接本面板实例，已恢复原配置。")
        finally:
            self.probe = None
