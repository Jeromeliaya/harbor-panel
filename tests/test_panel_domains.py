"""Domain lifecycle uses temporary files and simulated commands, never the real host."""

from contextlib import nullcontext
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from files import PanelError
import host_settings as hs
import panel_domains as domains
import panel_proxy


class DomainTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.state, self.app = self.root / "state", self.root / "app"
        self.state.mkdir()
        self.app.mkdir()
        self.conf = self.root / "nginx"
        self.conf.mkdir()
        self.upstream = self.root / "upstream"
        self.service, self.timer = self.root / "renew.service", self.root / "renew.timer"
        self.ops = Mock()
        self.ops.unit.return_value = {"ActiveState": "active", "NeedDaemonReload": "no"}
        self.ops.command.side_effect = self.command
        self.manager = domains.DomainManager(self.app, self.state, self.ops)
        self.reject = None
        self.extra = ""
        self.events = []
        replacements = [
            patch.object(domains, "CONF_DIR", self.conf),
            patch.object(domains, "ACME_ROOT", self.root / "acme"),
            patch.object(domains, "SERVICE", self.service),
            patch.object(domains, "TIMER", self.timer),
            patch.object(panel_proxy, "CONFIG", self.conf / "harbor-panel.conf"),
            patch.object(hs, "NGINX_FILE", self.upstream),
            patch.object(hs, "_read_file", side_effect=lambda p, **kw: p.read_text(encoding="utf-8")),
            patch.object(hs, "_atomic_write", side_effect=lambda p, text: p.write_text(text, encoding="utf-8")),
            patch.object(hs, "_directory"),
            patch.object(hs.HostSettings, "_lock", side_effect=lambda *a, **kw: nullcontext()),
            patch.object(domains.DomainManager, "_installed", return_value=8088),
            patch.object(domains.DomainManager, "_dns"),
            patch.object(domains.DomainManager, "_dependencies"),
            patch.object(domains.DomainManager, "_verify"),
            patch.object(domains.SiteManager, "_listening", return_value=[]),
        ]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)

    def command(self, name, *args, **kwargs):
        self.events.append((name, *args))
        if self.reject and self.reject(name, args):
            raise PanelError("simulated failure")
        if name == "certbot":
            cert = args[args.index("--cert-name") + 1]
            renewal = self.state / "tls" / "config" / "renewal" / (cert + ".conf")
            if args[0] == "certonly":
                renewal.parent.mkdir(parents=True, exist_ok=True)
                renewal.write_text("synthetic certificate")
            elif args[0] == "delete":
                renewal.unlink()
        text = ""
        if (name, args) == ("nginx", ("-T",)):
            text = self.extra + "".join(f"# configuration file {p}:\n{p.read_text()}" for p in self.conf.iterdir())
            if self.upstream.exists() and any("include " in p.read_text() for p in self.conf.iterdir()):
                text += f"# configuration file {self.upstream}:\n"
        return SimpleNamespace(stdout=text, returncode=0)

    def test_add_multiple_remove_one_and_last_with_private_certificates_and_timer(self):
        self.manager.add("one.example.com")
        self.manager.add("two.example.com")
        self.assertEqual(domains.origins(self.state), ["https://one.example.com", "https://two.example.com"])
        self.assertIn("--server", next(event for event in self.events if event[:2] == ("certbot", "certonly")))
        self.assertIn(domains.LE_DIRECTORY, next(event for event in self.events if event[:2] == ("certbot", "certonly")))
        config = domains.config_path("one.example.com")
        self.assertIn("listen 443 ssl;", config.read_text())
        self.assertIn("X-Forwarded-Proto https;", config.read_text())
        self.assertIn("return 301 https://one.example.com", config.read_text())
        self.assertTrue(self.timer.exists())
        self.manager.remove("one.example.com")
        self.assertEqual(domains.origins(self.state), ["https://two.example.com"])
        self.assertFalse(config.exists())
        self.assertTrue(self.timer.exists())
        self.manager.renew()
        self.assertTrue(any(event[:2] == ("certbot", "renew") for event in self.events))
        self.manager.remove("two.example.com")
        self.assertEqual(domains.load(self.state), [])
        self.assertFalse(self.timer.exists())
        self.assertFalse(self.service.exists())
        self.assertFalse(self.upstream.exists())

    def test_failed_issuance_restores_routes_and_does_not_approve_domain(self):
        self.reject = lambda name, args: name == "certbot"
        with self.assertRaisesRegex(PanelError, "simulated"):
            self.manager.add("one.example.com")
        self.assertEqual(domains.load(self.state), [])
        self.assertFalse(domains.config_path("one.example.com").exists())
        self.assertFalse(self.upstream.exists())
        self.assertFalse(self.timer.exists())

    def test_failed_tls_probe_keeps_only_a_removable_certificate_record(self):
        with patch.object(self.manager, "_verify", side_effect=PanelError("TLS failed")):
            with self.assertRaisesRegex(PanelError, "TLS failed"):
                self.manager.add("one.example.com")
        self.assertEqual(domains.origins(self.state), [])
        self.assertFalse(domains.config_path("one.example.com").exists())
        self.manager.remove("one.example.com")
        self.assertEqual(domains.load(self.state), [])

    def test_external_config_or_certificate_reference_is_not_deleted(self):
        self.manager.add("one.example.com")
        path = domains.config_path("one.example.com")
        original = path.read_text()
        path.write_text("manual config")
        with self.assertRaisesRegex(PanelError, "已被修改"):
            self.manager.remove("one.example.com")
        path.write_text(original)
        self.extra = f"ssl_certificate /live/{domains.certificate_name('one.example.com')}/fullchain.pem;\n"
        with self.assertRaisesRegex(PanelError, "仍引用"):
            self.manager.remove("one.example.com")
        self.assertEqual(path.read_text(), original)
        self.assertEqual(domains.origins(self.state), ["https://one.example.com"])
        self.assertFalse(any(event[:2] == ("certbot", "delete") for event in self.events))

    def test_legacy_http_can_be_upgraded_or_deleted_from_menu(self):
        domain = "one.example.com"
        legacy = panel_proxy.PanelProxy(self.state, 8088, enabled=True)
        site = {"id": "a" * 16, "domain": domain, "enabled": True,
                "config_sha256": hashlib.sha256(legacy.render(domain).encode()).hexdigest()}
        legacy.record.write_text(json.dumps(site))
        panel_proxy.CONFIG.write_text(legacy.render(domain))
        self.upstream.write_text(hs._nginx_file(8088))
        self.assertTrue(self.manager.list_domains()[0]["legacy"])
        self.manager.add(domain)
        self.assertFalse(legacy.record.exists())
        self.assertFalse(panel_proxy.CONFIG.exists())
        self.assertEqual(domains.origins(self.state), [f"https://{domain}"])

    def test_domain_menu_lists_delete_targets_and_returns_after_error_or_delete(self):
        self.manager.add("one.example.com")
        output = io.StringIO()
        with patch("builtins.input", side_effect=["2", "absent.example.com", "2", "one.example.com", "0"]), \
                patch("sys.stdout", output), patch("sys.stderr", output):
            self.manager.menu()
        text = output.getvalue()
        self.assertIn("one.example.com", text)
        self.assertIn("操作失败", text)
        self.assertGreaterEqual(text.count("0. 返回上级菜单"), 3)
        self.assertEqual(domains.load(self.state), [])

    def test_active_listener_job_and_foreign_ports_block_domain_mutations(self):
        (self.state / "host-settings.jobs.json").write_text('[{"status":"running"}]')
        with self.assertRaisesRegex(PanelError, "端口切换"):
            self.manager.add("one.example.com")
        self.assertEqual(self.events, [])
        (self.state / "host-settings.jobs.json").unlink()
        connection = SimpleNamespace(laddr=SimpleNamespace(port=80), pid=101)
        with patch.object(domains.SiteManager, "_listening", return_value=[connection]), \
                patch.object(domains.SiteManager, "_project", return_value="caddy"):
            with self.assertRaisesRegex(PanelError, "占用"):
                self.manager.add("one.example.com")
        self.assertEqual(self.events, [])

    def test_failed_certificate_delete_is_explicit_and_can_be_retried(self):
        self.manager.add("one.example.com")
        self.reject = lambda name, args: name == "certbot" and args[0] == "delete"
        with self.assertRaises(PanelError):
            self.manager.remove("one.example.com")
        self.assertEqual(domains.origins(self.state), [])
        self.assertEqual(len(domains.load(self.state)), 1)
        self.reject = None
        self.manager.remove("one.example.com")
        self.assertEqual(domains.load(self.state), [])

    def test_timer_cleanup_failure_can_retry_after_certificate_already_deleted(self):
        self.manager.add("one.example.com")
        self.reject = lambda name, args: name == "systemctl" and args[0] == "disable"
        with self.assertRaises(PanelError):
            self.manager.remove("one.example.com")
        self.assertEqual(domains.origins(self.state), [])
        self.reject = None
        self.manager.remove("one.example.com")
        self.assertEqual(domains.load(self.state), [])
        self.assertEqual(sum(event[:2] == ("certbot", "delete") for event in self.events), 1)

    def test_domain_probe_matches_nonce_and_expires(self):
        path = self.state / "panel-domain-probe.json"
        path.write_text(json.dumps({"nonce": "a" * 32, "proof": "b" * 64, "expires": 2000}))
        with patch.object(domains.time, "time", return_value=1000):
            self.assertEqual(domains.health(self.state, "a" * 32), {"proof": "b" * 64})
            self.assertIsNone(domains.health(self.state, "c" * 32))
        with patch.object(domains.time, "time", return_value=2001):
            self.assertIsNone(domains.health(self.state, "a" * 32))


class DNSValidation(unittest.TestCase):
    def test_all_records_must_point_to_local_public_addresses_including_nat(self):
        result = [(socket.AF_INET, 1, 6, "", ("8.8.8.8", 80))]
        local = {"eth0": [SimpleNamespace(family=socket.AF_INET, address="10.0.0.2")]}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"8.8.8.8"
        with patch.object(domains.socket, "getaddrinfo", return_value=result), \
                patch.object(hs.psutil, "net_if_addrs", return_value=local), \
                patch.object(domains, "urlopen", return_value=response):
            domains.DomainManager._dns("one.example.com")
            result.append((socket.AF_INET6, 1, 6, "", ("2001:4860:4860::8888", 80, 0, 0)))
            with self.assertRaisesRegex(PanelError, "全部 A / AAAA"):
                domains.DomainManager._dns("one.example.com")
