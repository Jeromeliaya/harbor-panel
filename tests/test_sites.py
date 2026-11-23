"""Website management tests that do not modify the host or contact Docker or ACME."""

import ipaddress
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from files import PanelError
from sites import SiteManager, domain_name, parse_upstream


class SiteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = SiteManager(Path(directory.name), False, mock.Mock(), 8088, {"panel.example.com"})
        self.manager.supported = True

    def test_domains_and_upstreams_are_validated_before_interpolation(self):
        self.assertEqual(domain_name("例子.example."), "xn--fsqu00a.example")
        self.assertEqual(parse_upstream("[::1]:3000", "proxy").url, "http://[::1]:3000")
        self.assertEqual(parse_upstream("origin.example.com", "https").url, "https://origin.example.com:443")
        for host in ("*.example.com", "foo.example.com:8080", "https://example.com", "example.com..", "127.0.0.1"):
            with self.subTest(host=host), self.assertRaises(PanelError):
                domain_name(host)
        for target in ("app:0", "app", "app:65536", "http://a:b@app:80", "app:80/secret", "app:80#x",
                       "app:80\nreverse_proxy", "[::1]trailing:443", "app:80/{env.TEST}"):
            with self.subTest(target=target), self.assertRaises(PanelError):
                parse_upstream(target, "proxy")
        with self.assertRaises(PanelError):
            parse_upstream("http://example.com", "https")

    def test_hostname_is_pinned_and_unsafe_resolution_is_refused(self):
        upstream = parse_upstream("https://origin.example.com", "https")
        with mock.patch.object(self.manager, "_resolve", return_value=[ipaddress.ip_address("192.0.2.50")]):
            self.assertEqual(self.manager._target(upstream).url, "https://192.0.2.50:443")
        with mock.patch.object(self.manager, "_resolve", return_value=[ipaddress.ip_address("192.0.2.50"),
                                                                      ipaddress.ip_address("::ffff:169.254.1.1")]):
            with self.assertRaises(PanelError):
                self.manager._target(upstream)
        for target in ("127.0.0.1:8088", "127.0.0.1:443", "localhost:2375", "[::1]:80"):
            with self.subTest(target=target), self.assertRaises(PanelError):
                self.manager._target(parse_upstream(target, "proxy"))

    def test_scan_identifies_docker_projects_and_excludes_panel_ports(self):
        def listener(host, port, pid):
            return SimpleNamespace(laddr=SimpleNamespace(ip=host, port=port), pid=pid)
        docker = {"name": "/shop-web-1", "service": "web", "project": "shop", "owner": "",
                  "ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3000"}],
                            "53/udp": [{"HostIp": "0.0.0.0", "HostPort": "53"}]}}
        listeners = [listener("0.0.0.0", 3000, 2), listener("0.0.0.0", 8088, 3), listener("::", 8000, 4)]
        with mock.patch.object(self.manager, "_listening", return_value=listeners), \
                mock.patch.object(self.manager, "_project", return_value="webserver"), \
                mock.patch.object(self.manager, "_containers", return_value=[docker]):
            self.assertEqual(self.manager.scan()["services"], [
                {"port": 3000, "address": "127.0.0.1", "project": "shop/web", "upstream": "127.0.0.1:3000"},
                {"port": 8000, "address": "::1", "project": "webserver", "upstream": "[::1]:8000"},
            ])
            self.assertEqual(self.manager._target(parse_upstream("web:80", "proxy")).address, "127.0.0.1:3000")
            with self.assertRaises(PanelError):
                self.manager._target(parse_upstream("web:81", "proxy"))

    def test_docker_binding_conflicts_even_without_listening_process(self):
        docker = {"name": "/existing-web", "ports": {"8443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}]}}
        with mock.patch.object(self.manager, "_listening", return_value=[]), \
                mock.patch.object(self.manager, "_containers", return_value=[docker]), self.assertRaises(PanelError) as error:
            self.manager._check_entry_ports(0)
        self.assertEqual(error.exception.status, 409)
        self.assertIn("existing-web", str(error.exception))

    def test_reload_failure_restores_prior_disk_and_memory_state(self):
        self.manager.config_dir.mkdir(parents=True)
        prior = self.manager._render([])
        self.manager.config.write_text(prior, encoding="utf-8")
        site = {"id": "1", "domain": "example.org", "kind": "https", "enabled": True,
                "target": "https://192.0.2.20:443", "origin_host": "origin.example.net", "origin_port": 443}
        candidate = self.manager._render([site])
        self.assertIn("header_up Host origin.example.net", candidate)
        self.assertIn("tls_server_name origin.example.net", candidate)
        with mock.patch.object(self.manager, "_docker", side_effect=["", PanelError("reload failed"), ""]) as docker:
            with self.assertRaisesRegex(PanelError, "reload failed"):
                self.manager._apply([site])
        self.assertEqual(docker.call_args_list[0].kwargs["data"], candidate)
        self.assertEqual(self.manager.config.read_text(encoding="utf-8"), prior)
        self.assertEqual(self.manager.overview()["sites"], [])

    def test_wordpress_initializes_before_publishing_and_uses_private_stdin(self):
        self.manager.directory.mkdir()
        calls = []
        def docker(*args, **kwargs):
            calls.append((args, kwargs))
            if kwargs.get("operation") == "初始化 WordPress 管理员":
                return "HARBOR_WP_READY"
            if args[0] == "port":
                return "127.0.0.1:32768\n"
            return ""
        site_id = "e012345678abcdef"
        with mock.patch.object(self.manager, "_docker", side_effect=docker):
            target, credentials = self.manager._wordpress(site_id, "site.example.org")
        initialize = next(i for i, (_, kw) in enumerate(calls) if kw.get("data"))
        publish = next(i for i, (args, _) in enumerate(calls) if "-p" in args)
        self.assertLess(initialize, publish)
        self.assertIn(credentials["password"], calls[initialize][1]["data"])
        self.assertFalse(any(credentials["password"] in " ".join(args) for args, _ in calls))
        self.assertEqual(target.address, "127.0.0.1:32768")
        state = self.manager.directory / "wordpress" / site_id
        self.assertEqual([item.name for item in state.iterdir()], ["admin-login"])

    def test_job_credentials_are_omitted_from_overview_and_expire(self):
        self.manager.job = {"status": "done", "credentials": {"username": "site-admin"}}
        self.manager.credentials_expire = float("inf")
        self.assertNotIn("credentials", self.manager.job_status(reveal=True)["job"])
        self.manager.secure = True
        self.assertNotIn("credentials", self.manager.overview()["job"])
        self.assertEqual(self.manager.job_status()["job"], {"status": "done", "has_credentials": True})
        self.assertIn("credentials", self.manager.job_status(reveal=True)["job"])
        self.manager.credentials_expire = 0
        self.assertEqual(self.manager.job_status(reveal=True)["job"], {"status": "done"})
