"""SSH transport and local-only appearance persistence regression tests."""

from io import BytesIO
import json
from pathlib import Path
import queue
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

import paramiko
from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from appearance import AppearanceStore
from files import PanelError
from terminal import TerminalManager, _TerminalSession


def image_bytes(color="red"):
    output = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private", "remove this metadata")
    with Image.new("RGB", (30, 20), color) as image:
        image.save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


class AppearanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = AppearanceStore(directory.name)

    def test_background_reencoded_and_preferences_survive_restart(self):
        self.store.set_theme("nocturne")
        self.store.set_background("image/png", image_bytes())
        restored = AppearanceStore(self.store.directory)
        self.assertEqual(restored.get(), {"theme": "nocturne", "has_background": True})
        content_type, data = restored.background()
        self.assertEqual(content_type, "image/png")
        with Image.open(BytesIO(data)) as image:
            self.assertNotIn("private", image.info)
            self.assertEqual(image.size, (30, 20))
        restored.remove_background()
        self.assertFalse(AppearanceStore(self.store.directory).get()["has_background"])

    def test_invalid_upload_and_theme_do_not_replace_preferences(self):
        self.store.set_background("image/png", image_bytes())
        previous = self.store.background()
        for content_type, data in (("image/svg+xml", b"<svg/>"), ("image/jpeg", image_bytes()), ("image/png", b"invalid"), ("image/png", b"x" * 1_500_001)):
            with self.subTest(content_type=content_type, length=len(data)), self.assertRaises(PanelError):
                self.store.set_background(content_type, data)
        with self.assertRaises(PanelError):
            self.store.set_theme("unknown")
        self.assertEqual(self.store.background(), previous)

    def test_failed_settings_write_rolls_back_image_changes(self):
        self.store.set_background("image/png", image_bytes())
        previous = self.store.background()
        with mock.patch.object(self.store, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.set_background("image/png", image_bytes("blue"))
            self.assertEqual(self.store.background(), previous)
            with self.assertRaises(OSError):
                self.store.remove_background()
            self.assertEqual(self.store.background(), previous)

    def test_invalid_persisted_settings_are_reported(self):
        self.store.path.write_text('{"theme":"unknown"}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "invalid theme"):
            AppearanceStore(self.store.directory)


class Peer:
    def __init__(self):
        self.messages = queue.Queue()
        self.closed = threading.Event()

    def control(self, message):
        self.messages.put(message)

    def send(self, event):
        self.messages.put(event)

    def close(self):
        self.closed.set()


class SSHServer(paramiko.ServerInterface):
    def __init__(self):
        self.shell = threading.Event()
        self.dimensions = None

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if username == "root" else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, channel_id):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, *args):
        self.dimensions = (term, width, height)
        return True

    def check_channel_shell_request(self, channel):
        self.shell.set()
        return True


class TerminalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = TerminalManager(directory.name)
        self.addCleanup(self.manager.close_all)
        self.peer = Peer()
        self.session = _TerminalSession("owner", self.peer, self.manager)
        self.addCleanup(self.session.close)

    def test_remote_password_requires_https_and_is_not_audited(self):
        with mock.patch.object(self.session, "_connect_worker") as worker:
            self.session.connect({"target": "remote", "host": "example.com", "password": "secret"}, False)
            worker.assert_not_called()
        self.assertIn("HTTPS", self.peer.messages.get_nowait()["message"])

    def test_host_key_confirmation_and_persistence(self):
        key = paramiko.RSAKey.generate(2048)
        client = paramiko.SSHClient()
        errors = queue.Queue()
        def confirm():
            try:
                self.session.confirm_host_key(client, "example.com", key, 0)
            except Exception as exc:
                errors.put(exc)
        worker = threading.Thread(target=confirm)
        worker.start()
        try:
            message = self.peer.messages.get(timeout=3)
            self.assertTrue(message["fingerprint"].startswith("SHA256:"))
            self.assertFalse(self.manager.host_keys_path.exists())
            self.session.accept_host_key(message["challenge"], True)
        finally:
            self.session.close_ssh() if not self.session.pending_key or not self.session.pending_key["accepted"] else None
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        if not errors.empty():
            raise errors.get()
        self.assertEqual(paramiko.HostKeys(str(self.manager.host_keys_path))["example.com"][key.get_name()], key)

    def test_logout_keeps_shared_local_daemon_but_shutdown_stops_it(self):
        self.manager._reserve(self.session)
        with mock.patch.object(self.manager.local, "close") as close:
            self.manager.close_owner("owner")
            close.assert_not_called()
            self.manager.close_all()
            close.assert_called_once_with(shutdown=True)
        with self.assertRaises(PanelError):
            self.manager._reserve(self.session)

    def test_ssh_eof_during_close_still_closes_client_and_browser(self):
        channel, client = mock.Mock(), mock.Mock()
        channel.close.side_effect = EOFError()
        self.session.channel, self.session.client = channel, client
        with self.assertLogs("terminal", level="WARNING"):
            self.session.close()
        client.close.assert_called_once()
        self.assertTrue(self.peer.closed.is_set())
        self.assertIsNone(self.session.channel)

    def test_cancelled_local_setup_cannot_publish_a_connection(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def connection():
            started.set()
            release.wait(5)
            raise PanelError("cancelled")
        def connect():
            self.manager.connect_slots.acquire()
            self.session._connect_worker(0, "local", "127.0.0.1", None, "root", None, 100, 28)
            finished.set()
        with mock.patch.object(self.manager.local, "connection", side_effect=connection):
            worker = threading.Thread(target=connect)
            worker.start()
            try:
                self.assertTrue(started.wait(3))
                self.session.close()
            finally:
                release.set()
                worker.join(5)
        self.assertTrue(finished.is_set())
        self.assertIsNone(self.session.client)
        self.assertTrue(self.peer.messages.empty())

    def test_real_ssh_pty_input_and_output_with_pinned_local_key(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(8)
        self.addCleanup(listener.close)
        host_key = paramiko.RSAKey.generate(2048)
        client_key = paramiko.RSAKey.generate(2048)
        ssh_server = SSHServer()
        errors = queue.Queue()
        finished = threading.Event()
        def serve():
            transport = None
            try:
                connection, _ = listener.accept()
                transport = paramiko.Transport(connection)
                transport.add_server_key(host_key)
                transport.start_server(server=ssh_server)
                channel = transport.accept(5)
                if channel is None or not ssh_server.shell.wait(5):
                    raise AssertionError("SSH PTY/shell was not requested")
                channel.settimeout(5)
                channel.sendall(b"echo:" + channel.recv(100))
                finished.wait(5)
            except Exception as exc:
                errors.put(exc)
            finally:
                if transport:
                    transport.close()
        worker = threading.Thread(target=serve)
        worker.start()
        try:
            with mock.patch.object(self.manager.local, "connection", return_value=(listener.getsockname()[1], client_key, host_key)):
                self.manager.connect_slots.acquire()
                self.session._connect_worker(0, "local", "127.0.0.1", None, "root", None, 110, 35)
            connected = self.peer.messages.get(timeout=3)
            self.assertEqual(connected["type"], "connected", connected)
            self.assertEqual(ssh_server.dimensions, (b"xterm-256color", 110, 35))
            self.session.write(b"hello\r")
            self.session.channel.status_event.wait(.1)
            for _ in range(30):
                self.session.pump_output()
                try:
                    output = self.peer.messages.get(timeout=.1)
                    break
                except queue.Empty:
                    continue
            else:
                self.fail("SSH output did not arrive")
            self.assertEqual(output.data, b"echo:hello\r")
        finally:
            finished.set()
            self.session.close()
            worker.join(8)
        self.assertFalse(worker.is_alive())
        if not errors.empty():
            raise errors.get()


if __name__ == "__main__":
    unittest.main()
