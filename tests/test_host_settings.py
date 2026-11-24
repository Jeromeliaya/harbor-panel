"""Exercise listener jobs against an in-memory host; no commands, sockets, or host-file writes."""

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host_settings as hs
from files import PanelError


class FakeHost:
    old = 8088
    origin = "https://panel.example.test"
    ssh = "ssh.service"

    def __init__(self):
        self.files = {hs.PORT_FILE: hs._panel_file(self.old), hs.NGINX_FILE: hs._nginx_file(self.old),
                      hs.SSH_FILE: hs._ssh_file(22)}
        self.events = []
        self.local = self.proxy = self.old
        self.ssh_ports = [22]
        self.occupied = set()
        self.linked = True
        self.failed_local = set()
        self.failed_public = set()
        self.failed_ssh = set()
        self.proxy_reload_fail = set()
        self.enabled = True
        self.reply = None

    def read(self, path, limit=0):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write(self, path, text):
        self.events.append(("write", path, text))
        self.files[path] = text

    def available(self):
        return self.enabled

    def launch(self, _state, job):
        self.events.append(("launch", job))

    def unit(self, name, *_keys):
        assert name == hs.PANEL_UNIT
        return {"ActiveState": "active", "NeedDaemonReload": "no", "MainPID": str(os.getpid())}

    def free_port(self, port):
        self.events.append(("free", port))
        if port in self.occupied:
            raise PanelError("occupied", 409, code="port_in_use")

    def https_proxy(self, port, text):
        self.events.append(("nginx -T", port))
        if not self.linked or text != hs._nginx_file(port):
            raise PanelError("managed Nginx include is not loaded", 409)

    def reload_nginx(self):
        port = int(self.files[hs.NGINX_FILE].rsplit(":", 1)[1].strip(";\n"))
        self.events.append(("nginx -t", port))
        if port in self.proxy_reload_fail:
            raise PanelError("nginx validation failed", 409)
        self.events.append(("reload", "nginx.service", port))
        self.proxy = port

    def probe_panel(self, base, nonce, proof):
        public = base == self.origin
        port = self.proxy if public else int(base.rsplit(":", 1)[1])
        self.events.append(("probe", base, port))
        if port != self.local or port in (self.failed_public if public else self.failed_local):
            return False
        return self.reply(port, base, nonce) == {"proof": proof}

    def command(self, command, action, unit):
        assert command == "systemctl"
        if unit == hs.PANEL_UNIT and action == "restart":
            self.local = int(self.files[hs.PORT_FILE].strip().split("=")[1])
            self.events.append((action, unit, self.local))
        elif unit == self.ssh and action == "reload":
            self.ssh_ports = self.configured_ports()
            self.events.append((action, unit, self.ssh_ports))
        else:
            raise AssertionError((command, action, unit))

    def configured_ports(self):
        return [int(line.split()[1]) for line in self.files[hs.SSH_FILE].splitlines()]

    def ssh_unit(self):
        return self.ssh

    def ssh_config(self, ports):
        if self.configured_ports() != ports:
            raise PanelError("incompatible SSH configuration", 409)
        return ["0.0.0.0"], "unchanged-config-digest"

    def ssh_listening(self, unit, ports, hosts):
        return unit == self.ssh and ports == self.ssh_ports and tuple(ports) not in self.failed_ssh


class ListenerJobs(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.state = Path(__file__).parent / "fake-listener-state"
        self._patch(hs, "_read_file", self.host.read)
        self._patch(hs, "_atomic_write", self.host.write)
        self._patch(hs.HostSettings, "_lock", self.fake_lock)
        self._patch(hs.HostSettings, "_wait", staticmethod(self.immediate_wait))
        self.host.reply = lambda port, _base, nonce: self.settings(
            port, self.settings()._jobs()[-1]["public_origin"]
        ).health(nonce)

    def _patch(self, owner, name, value):
        patch = mock.patch.object(owner, name, value)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    @contextmanager
    def fake_lock(*_args, **_kwargs):
        yield

    @staticmethod
    def immediate_wait(test, message, seconds=20):
        if not test():
            raise PanelError(message, 409)

    def settings(self, port=8088, origin=None):
        return hs.HostSettings(self.state, full_host=True, panel_port=port, public_origin=origin, operations=self.host)

    def worker(self, job_id, **kwargs):
        hs.HostSettings(self.state, full_host=True, panel_port=None, public_origin=None,
                        operations=self.host).run_worker(job_id, **kwargs)

    def mutations(self):
        return [event for event in self.host.events if event[0] == "write" and event[1] in
                (hs.PORT_FILE, hs.NGINX_FILE, hs.SSH_FILE)]

    def test_http_managed_domain_updates_upstream_without_exposing_new_port(self):
        self.host.origin = "http://panel.example.test"
        with mock.patch.object(hs.HostSettings, "_proxy_origin", return_value=self.host.origin):
            manager = self.settings()
            self.assertEqual(manager.status()["panel"]["mode"], "http_proxy")
            job = manager.start("panel", 9090)
            self.worker(job["id"])
            self.assertEqual(manager.job(job["id"])["status"], "succeeded")
        self.assertEqual(self.host.files[hs.NGINX_FILE], hs._nginx_file(9090))
        self.assertEqual(self.host.proxy, 9090)

    def test_direct_noop_and_occupied_port_do_not_launch_or_write(self):
        manager = self.settings()
        self.host.occupied.update((8088, 9090))
        self.assertEqual(manager.start("panel", 8088)["status"], "succeeded")
        for value in (True, -1, 65536, "8088"):
            with self.subTest(value=value), self.assertRaises(PanelError):
                manager.start("panel", value)
        with self.assertRaisesRegex(PanelError, "firewall"):
            manager.start("panel", 9090)
        with self.assertRaisesRegex(PanelError, "occupied"):
            manager.start("panel", 9090, firewall_ready=True)
        self.assertFalse(any(event[0] == "launch" for event in self.host.events))
        self.assertEqual(self.mutations(), [])

    def test_direct_change_reports_new_port_after_service_restart(self):
        job_id = self.settings().start("panel", 9090, firewall_ready=True)["id"]
        self.worker(job_id)
        result = self.settings(9090).job(job_id)
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("firewall access was not verified", result["message"])
        self.assertEqual(self.host.local, 9090)
        self.assertEqual(self.host.proxy, 8088)
        self.assertNotIn("probe", result)

    def test_https_requires_loaded_include_and_real_baseline_route(self):
        manager = self.settings(origin=self.host.origin)
        self.host.linked = False
        with self.assertRaisesRegex(PanelError, "include"):
            manager.start("panel", 9090)
        self.host.linked = True
        self.host.failed_public.add(8088)
        job_id = manager.start("panel", 9090)["id"]
        self.worker(job_id)
        self.assertEqual(manager.job(job_id)["status"], "failed")
        self.assertEqual(self.mutations(), [])

    def test_https_checks_new_local_and_public_proof_and_preserves_origin(self):
        manager = self.settings(origin=self.host.origin)
        job_id = manager.start("panel", 9090)["id"]
        self.worker(job_id)
        result = self.settings(9090, self.host.origin).job(job_id)
        self.assertEqual((result["status"], result["public_origin"]), ("succeeded", self.host.origin))
        self.assertIn(("nginx -t", 9090), self.host.events)
        self.assertIn(("probe", self.host.origin, 9090), self.host.events)
        self.assertEqual(self.host.local, self.host.proxy)
        self.assertIsNone(manager.health("0" * 32))

    def test_health_requires_nonce_and_exact_listener_port(self):
        manager = self.settings(origin=self.host.origin)
        job = manager.start("panel", 9090)
        self.assertIsNone(manager.health("!" * 32))
        manager._update(job["id"], status="running")
        probe = manager._probe(job, 9090)
        self.assertIsNone(manager.health(probe["nonce"]))
        self.assertIsNone(self.settings(9090, self.host.origin).health(probe["proof"][:32]))
        self.assertIsNone(self.settings(9090).health(probe["nonce"]))
        self.assertEqual(self.settings(9090, self.host.origin).health(probe["nonce"]), {"proof": probe["proof"]})

    def test_public_mismatch_restores_and_verifies_both_files_and_services(self):
        manager = self.settings(origin=self.host.origin)
        self.host.failed_public.add(9090)
        job_id = manager.start("panel", 9090)["id"]
        self.worker(job_id)
        self.assertEqual(manager.job(job_id)["status"], "rolled_back")
        self.assertEqual((self.host.local, self.host.proxy), (8088, 8088))
        self.assertEqual(self.host.files[hs.PORT_FILE], hs._panel_file(8088))
        self.assertEqual(self.host.files[hs.NGINX_FILE], hs._nginx_file(8088))

    def test_unhealthy_local_port_never_reconfigures_nginx(self):
        manager = self.settings(origin=self.host.origin)
        self.host.failed_local.add(9090)
        job_id = manager.start("panel", 9090)["id"]
        self.worker(job_id)
        self.assertEqual(manager.job(job_id)["status"], "rolled_back")
        self.assertFalse(any(event[1] == hs.NGINX_FILE for event in self.mutations()))

    def test_resume_worker_after_interruption_rolls_back_instead_of_retrying(self):
        manager = self.settings()
        job_id = manager.start("panel", 9090, firewall_ready=True)["id"]
        manager._update(job_id, status="running", rollback={"files": {"panel": self.host.files[hs.PORT_FILE]}, "panel": True})
        self.host.files[hs.PORT_FILE] = hs._panel_file(9090)
        self.host.local = 9090
        self.worker(job_id)
        self.assertEqual(manager.job(job_id)["status"], "rolled_back")
        self.assertEqual(self.host.local, 8088)

    def test_interrupted_https_worker_restores_both_files_from_durable_journal(self):
        manager = self.settings(origin=self.host.origin)
        job_id = manager.start("panel", 9090)["id"]
        reload_nginx = self.host.reload_nginx

        def reload_then_interrupt():
            reload_nginx()
            raise KeyboardInterrupt

        with mock.patch.object(self.host, "reload_nginx", reload_then_interrupt), self.assertRaises(KeyboardInterrupt):
            self.worker(job_id)
        self.assertEqual(manager.job(job_id)["status"], "running")
        self.assertEqual((self.host.local, self.host.proxy), (9090, 9090))
        self.worker(job_id)
        self.assertEqual(manager.job(job_id)["status"], "rolled_back")
        self.assertEqual((self.host.local, self.host.proxy), (8088, 8088))
        self.assertEqual(self.host.files[hs.PORT_FILE], hs._panel_file(8088))
        self.assertEqual(self.host.files[hs.NGINX_FILE], hs._nginx_file(8088))

    def test_ssh_keeps_old_ingress_until_confirmation_then_retires_it(self):
        manager = self.settings()
        job_id = manager.start("ssh", 2222, firewall_ready=True)["id"]
        with self.assertRaisesRegex(PanelError, "not awaiting"):
            manager.confirm_ssh(job_id)

        def wait_for_operator(test, message, seconds=20):
            if seconds == hs.CONFIRM_SECONDS:
                self.assertEqual(self.host.ssh_ports, [22, 2222])
                self.assertEqual(self.settings().status()["ssh"]["ports"], [22, 2222])
                with self.assertRaisesRegex(PanelError, "already in progress"):
                    manager.start("panel", 9090, firewall_ready=True)
                manager.confirm_ssh(job_id)
            self.immediate_wait(test, message, seconds)

        with mock.patch.object(hs.HostSettings, "_wait", staticmethod(wait_for_operator)):
            self.worker(job_id)
        self.assertEqual(manager.job(job_id)["status"], "succeeded")
        self.assertEqual(self.host.ssh_ports, [2222])

    def test_unconfirmed_or_unhealthy_ssh_rolls_back_and_retains_old_ingress(self):
        for failed in (set(), {(22, 2222)}):
            self.host.failed_ssh = failed
            manager = self.settings()
            job_id = manager.start("ssh", 2222, firewall_ready=True)["id"]
            self.worker(job_id)
            self.assertEqual(manager.job(job_id)["status"], "rolled_back")
            self.assertEqual(self.host.ssh_ports, [22])
            self.assertEqual(self.host.files[hs.SSH_FILE], "Port 22\n")

    def test_rollback_failure_blocks_changes_until_console_recovery(self):
        manager = self.settings(origin=self.host.origin)
        self.host.proxy_reload_fail.update((8088, 9090))
        job_id = manager.start("panel", 9090)["id"]
        self.worker(job_id)
        self.assertEqual(manager.job(job_id)["status"], "rollback_failed")
        self.assertEqual(self.host.local, 8088)
        with self.assertRaisesRegex(PanelError, "console"):
            manager.start("panel", 9000)
        self.host.proxy_reload_fail.clear()
        self.worker(job_id, recover=True)
        self.assertEqual(manager.job(job_id)["status"], "rolled_back")

    def test_non_systemd_does_not_touch_host_or_journal(self):
        self.host.enabled = False
        manager = self.settings()
        self.assertFalse(manager.status()["panel"]["available"])
        with self.assertRaises(PanelError) as error:
            manager.start("panel", 9090, firewall_ready=True)
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.host.events, [])


class HostValidation(unittest.TestCase):
    def test_ssh_bootstrap_preserves_non_port_settings_and_restores_failed_adoption(self):
        with tempfile.TemporaryDirectory() as folder:
            config, include = Path(folder) / "sshd_config", Path(folder) / "panel-ports.conf"
            original = "Port 22\nPasswordAuthentication no\nMatch User backup\n    X11Forwarding no\n"
            config.write_text(original)
            ops = hs.HostOperations()
            output = "port 22\nlistenaddress 0.0.0.0:22\npasswordauthentication no\n"
            with mock.patch.object(hs, "SSH_CONFIG", config), mock.patch.object(hs, "SSH_FILE", include), \
                    mock.patch.object(hs, "_read_file", side_effect=lambda p: p.read_text()), \
                    mock.patch.object(hs, "_atomic_write", side_effect=lambda p, text: p.write_text(text)), \
                    mock.patch.object(hs, "_open_file", side_effect=lambda p, flags: os.open(p, flags)), \
                    mock.patch.object(hs, "_sync_dir"), \
                    mock.patch.object(ops, "command", return_value=SimpleNamespace(stdout=output)):
                self.assertEqual(ops.ssh_bootstrap(apply=False)[0], 22)
                self.assertFalse(include.exists())
                self.assertEqual(config.read_text(), original)
                ops.ssh_bootstrap(apply=True)
                self.assertEqual(include.read_text(), "Port 22\n")
                self.assertTrue(config.read_text().startswith(f"Include {include}\n# Harbor Panel adopted: Port 22\n"))
                self.assertIn("Match User backup\n    X11Forwarding no", config.read_text())
                config.write_text(original)
                include.unlink()
                with mock.patch.object(ops, "ssh_config", return_value=(["0.0.0.0"], "changed")):
                    with self.assertRaisesRegex(PanelError, "beyond"):
                        ops.ssh_bootstrap(apply=True)
                self.assertEqual(config.read_text(), original)
                self.assertFalse(include.exists())
                include.write_text("Port 22\n")
                ops.ssh_bootstrap(apply=True)
                self.assertTrue(config.read_text().startswith(f"Include {include}\n"))
                self.assertEqual(include.read_text(), "Port 22\n")

    def test_https_rejects_any_nonlocal_dns_answer_before_opening_sockets(self):
        local = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 443))
        remote = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.10", 443))
        interfaces = {"eth0": [SimpleNamespace(family=socket.AF_INET, address="192.0.2.10")]}
        with mock.patch.object(hs.psutil, "net_if_addrs", return_value=interfaces), mock.patch.object(
            hs.socket, "getaddrinfo", return_value=[local, remote]
        ), mock.patch.object(hs, "_LocalHTTPSConnection") as conn:
            self.assertFalse(hs.HostOperations().probe_panel("https://panel.example.test", "n", "p"))
            conn.assert_not_called()

    def test_https_uses_numeric_pins_and_original_hostname_for_sni(self):
        hostname = "panel.example.test"
        endpoint = (socket.AF_INET, ("192.0.2.10", 8443))
        context = mock.Mock()
        connection = hs._LocalHTTPSConnection.__new__(hs._LocalHTTPSConnection)
        connection.host, connection.timeout, connection.endpoint, connection.context = hostname, 2, endpoint, context
        with mock.patch.object(hs.socket, "socket") as sock, mock.patch.object(hs.socket, "getaddrinfo") as dns:
            connection.connect()
        dns.assert_not_called()
        sock.return_value.connect.assert_called_once_with(endpoint[1])
        context.wrap_socket.assert_called_once_with(sock.return_value, server_hostname=hostname)

    def test_https_probe_requires_correct_proof_and_verified_context(self):
        endpoint = (socket.AF_INET, ("192.0.2.10", 443))
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"proof":"expected"}'
        with mock.patch.object(hs, "_local_https_endpoints", return_value=[endpoint]), mock.patch.object(
            hs, "_LocalHTTPSConnection"
        ) as conn:
            conn.return_value.getresponse.return_value = response
            ops = hs.HostOperations()
            self.assertTrue(ops.probe_panel("https://panel.example.test", "nonce", "expected"))
            self.assertFalse(ops.probe_panel("https://panel.example.test", "nonce", "wrong"))
            host, port, pin, context = conn.call_args.args
            self.assertEqual((host, port, pin), ("panel.example.test", 443, endpoint))
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, hs.ssl.CERT_REQUIRED)
            conn.return_value.request.assert_called_with("GET", f"{hs.HEALTH_PATH}?nonce=nonce",
                                                         headers={"Cache-Control": "no-cache"})

    def test_unsafe_managed_file_metadata_is_rejected(self):
        for mode, uid, links in ((stat.S_IFLNK | 0o777, 0, 1), (stat.S_IFREG | 0o600, 23, 1),
                                 (stat.S_IFREG | 0o620, 0, 1), (stat.S_IFREG | 0o600, 0, 2)):
            with self.subTest(mode=mode, uid=uid, links=links), self.assertRaises(PanelError):
                hs._regular(SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=links))

    def test_nginx_success_without_managed_file_in_dump_is_rejected(self):
        ops = hs.HostOperations()
        with mock.patch.object(ops, "command", return_value=SimpleNamespace(stdout="# configuration file /etc/nginx/nginx.conf:\n")):
            with self.assertRaisesRegex(PanelError, "must include"):
                ops.https_proxy(8088, hs._nginx_file(8088))

    def test_socket_active_or_enabled_is_rejected_before_sshd_changes(self):
        ops = hs.HostOperations()
        for active, enabled in (("active", "disabled"), ("inactive", "enabled")):
            with self.subTest(active=active, enabled=enabled), mock.patch.object(
                ops, "unit", return_value={"LoadState": "loaded", "ActiveState": active, "UnitFileState": enabled}
            ), self.assertRaisesRegex(PanelError, "Socket-activated"):
                ops.ssh_unit()

    def test_ssh_effective_configuration_must_match_managed_ports(self):
        ops = hs.HostOperations()
        with mock.patch.object(hs, "_read_file", return_value=f"Include {hs.SSH_FILE}\n"), mock.patch.object(
                ops, "command", return_value=SimpleNamespace(stdout="port 22\nport 2222\nlistenaddress 0.0.0.0:22\n")) as run:
            with self.assertRaisesRegex(PanelError, "wildcard listeners"):
                ops.ssh_config([22, 2222])
            self.assertEqual([call.args[:2] for call in run.call_args_list], [("sshd", "-t"), ("sshd", "-T")])


if __name__ == "__main__":
    unittest.main()
