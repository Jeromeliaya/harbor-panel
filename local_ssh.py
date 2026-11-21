"""Private loopback OpenSSH instance for native root deployments."""

import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time

import paramiko

from files import PanelError


class LocalSSH:
    def __init__(self, state_dir, enabled):
        self.directory = Path(state_dir) / "local-ssh"
        self.enabled = enabled
        self.lock = threading.RLock()
        self.process = None
        self.port = None
        self.key = None
        self.host_key = None
        self.log = None
        self.stopped = False

    def connection(self):
        with self.lock:
            if self.stopped:
                raise PanelError("The local SSH service is shutting down.", 503)
            if not self.enabled or os.name != "posix":
                raise PanelError("Automatic local SSH requires the native Linux root deployment. Remote SSH is available through HTTPS.")
            if self.process is None or self.process.poll() is not None:
                self._start()
            return self.port, self.key, self.host_key

    def _start(self):
        self.close()
        executable = shutil.which("sshd") or ("/usr/sbin/sshd" if Path("/usr/sbin/sshd").is_file() else None)
        if not executable:
            raise PanelError("OpenSSH server is missing. Update using the native installer or install openssh-server, then reconnect.")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.key = paramiko.RSAKey.generate(3072)
        self.host_key = paramiko.RSAKey.generate(3072)
        host_path = self.directory / "host_key"
        authorized = self.directory / "authorized_keys"
        config = self.directory / "sshd_config"
        # These keys belong only to this process; system SSH files remain untouched.
        self.host_key.write_private_key_file(str(host_path))
        authorized.write_text(self.key.get_name() + " " + self.key.get_base64() + "\n", encoding="ascii")
        for path in (host_path, authorized):
            os.chmod(path, 0o600)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        def quoted(path):
            return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'
        config.write_text(
            f"ListenAddress 127.0.0.1\nPort {self.port}\n"
            f"HostKey {quoted(host_path)}\nAuthorizedKeysFile {quoted(authorized)}\n"
            f"PidFile {quoted(self.directory / 'sshd.pid')}\n"
            "PermitRootLogin prohibit-password\nPubkeyAuthentication yes\n"
            "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
            f"UsePAM {'yes' if Path('/etc/pam.d/sshd').is_file() else 'no'}\nStrictModes yes\nAllowUsers root\n"
            "AllowAgentForwarding no\nAllowTcpForwarding no\nX11Forwarding no\n"
            "PermitTunnel no\nPermitUserEnvironment no\nPermitUserRC no\n"
            "LoginGraceTime 15\nMaxAuthTries 2\nLogLevel ERROR\n",
            encoding="utf-8",
        )
        os.chmod(config, 0o600)
        # OpenSSH's privilege-separation directory is required even for a private instance.
        Path("/run/sshd").mkdir(mode=0o755, parents=True, exist_ok=True)
        self.log = open(self.directory / "sshd.log", "wb")
        os.chmod(self.directory / "sshd.log", 0o600)
        try:
            self.process = subprocess.Popen([executable, "-D", "-e", "-f", str(config)], stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise PanelError("Private local SSH could not start. Check local-ssh/sshd.log in the panel state directory.")
                try:
                    with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                        return
                except OSError:
                    time.sleep(0.05)
            raise PanelError("Private local SSH startup timed out. Check local-ssh/sshd.log in the panel state directory.")
        except (OSError, PanelError):
            self.close()
            raise

    def close(self, shutdown=False):
        with self.lock:
            if shutdown:
                self.stopped = True
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
                self.process = None
            if self.log is not None:
                self.log.close()
                self.log = None
            self.key = self.host_key = None
