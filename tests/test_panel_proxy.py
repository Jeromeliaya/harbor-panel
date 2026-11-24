"""Nginx domain lifecycle in private temporary files; never run host commands."""

from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from files import PanelError
import host_settings as hs
import panel_proxy as proxy
from sites import SiteManager


class PanelProxyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "conf.d" / "harbor-panel.conf"
        self.upstream = self.root / "harbor-panel.nginx-upstream"
        self.change = Mock()
        self.panel = proxy.PanelProxy(self.root, 8088, True, self.change)
        self.ops = Mock()
        self.loaded = True

        def command(args, *_args, **_kwargs):
            if args == ["nginx", "-T"] and self.config.exists() and self.loaded:
                return f"# configuration file {self.config}:\n" + self.config.read_text()
            return ""

        replacements = [
            patch.object(proxy, "CONFIG", self.config),
            patch.object(hs, "NGINX_FILE", self.upstream),
            patch.object(hs, "_read_file", lambda p, **_kw: p.read_text()),
            patch.object(hs, "_atomic_write", lambda p, text: p.write_text(text)),
            patch.object(hs, "_sync_dir"),
            patch.object(hs.HostSettings, "_require"),
            patch.object(hs.HostSettings, "_preflight_panel"),
            patch.object(hs.HostSettings, "_lock", side_effect=lambda *_a, **_kw: nullcontext()),
            patch.object(hs, "HostOperations", return_value=self.ops),
            patch.object(proxy.shutil, "which", return_value="/fixture/nginx"),
            patch.object(SiteManager, "_command", side_effect=command),
            patch.object(SiteManager, "_listening", return_value=[]),
            patch.object(proxy.PanelProxy, "_probe"),
        ]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_create_restart_disable_enable_remove_preserves_other_sites(self):
        self.config.parent.mkdir()
        other = self.config.parent / "other.conf"
        other.write_text("unrelated")
        self.panel.create("panel.example.com")
        self.assertIn("server_name panel.example.com;", self.config.read_text())
        self.assertEqual(self.upstream.read_text(), hs._nginx_file(8088))
        self.assertEqual(self.panel.public()["url"], "http://panel.example.com")
        restored = proxy.PanelProxy(self.root, 8088, True)
        self.assertEqual(restored.site, self.panel.site)
        self.panel.action("disable")
        self.assertFalse(self.config.exists())
        self.assertFalse(self.panel.public()["enabled"])
        self.assertFalse(self.upstream.exists())
        self.panel.port = 9090
        self.panel.action("enable")
        self.assertTrue(self.config.exists())
        self.assertEqual(self.upstream.read_text(), hs._nginx_file(9090))
        self.panel.action("remove")
        self.assertIsNone(self.panel.site)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.upstream.exists())
        self.assertFalse(self.panel.record.exists())
        self.assertEqual(other.read_text(), "unrelated")

    def test_invalid_loaded_config_or_failed_probe_rolls_back_and_revokes_alias(self):
        self.loaded = False
        with self.assertRaisesRegex(PanelError, "conf.d"):
            self.panel.create("panel.example.com")
        self.assertIsNone(self.panel.site)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.panel.record.exists())
        self.loaded = True
        with patch.object(self.panel, "_probe", side_effect=PanelError("probe failed")):
            with self.assertRaisesRegex(PanelError, "probe failed"):
                self.panel.create("panel.example.com")
        self.assertIsNone(self.panel.site)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.upstream.exists())
        self.change.assert_called()

    def test_manual_config_is_never_adopted_or_overwritten(self):
        self.upstream.write_text("proxy_pass http://127.0.0.1:8088;\n")
        with self.assertRaisesRegex(PanelError, "手动配置"):
            self.panel.create("panel.example.com")
        self.assertTrue(self.upstream.exists())
        self.assertFalse(self.panel.record.exists())

    def test_modified_config_blocks_remove_and_keeps_alias(self):
        self.panel.create("panel.example.com")
        self.config.write_text("edited outside the panel")
        with self.assertRaisesRegex(PanelError, "不会覆盖"):
            self.panel.action("remove")
        self.assertIsNotNone(self.panel.site)
        self.assertEqual(self.config.read_text(), "edited outside the panel")
