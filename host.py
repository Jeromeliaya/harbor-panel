"""Real host telemetry and bounded, structured Docker operations."""

from collections import deque
import getpass
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import threading
import time

import psutil

from files import PanelError


class Host:
    COMMANDS = ("help", "hostname", "whoami", "uptime", "df -h", "free -h", "ps", "docker ps")
    COMMAND_TIMEOUT = 30
    MAX_COMMAND_OUTPUT = 1024 * 1024

    def __init__(self, disk_path, full_host=False):
        self.disk_path = str(disk_path)
        self.full_host = full_host
        self.lock = threading.Lock()
        self.history = deque(maxlen=90)
        self.stop = threading.Event()
        self.previous_network = psutil.net_io_counters()
        self.previous_time = time.monotonic()
        self.snapshot = {}
        pids = psutil.pids()
        self.process_snapshot = {
            "processes": [
                {"pid": pid, "name": "?", "user": "?", "status": "?", "memory": 0}
                for pid in pids[:150]
            ],
            "total": len(pids),
        }
        self.process_snapshot_at = 0.0
        self.process_lock = threading.Lock()
        self.sampler_error = None
        self.sample()
        self.thread = threading.Thread(target=self._sample_loop, name="harbor-metrics", daemon=True)
        self.thread.start()

    def _sample_loop(self):
        while not self.stop.wait(2):
            try:
                self.sample()
            except (OSError, psutil.Error) as exc:
                with self.lock:
                    self.sampler_error = f"Host metrics unavailable: {exc}"

    def sample(self):
        now = time.monotonic()
        network = psutil.net_io_counters()
        elapsed = max(now - self.previous_time, 0.001)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(self.disk_path)
        cpu = psutil.cpu_percent()
        point = {
            "time": time.time(), "cpu": cpu, "memory": memory.percent,
            "received": max(0, network.bytes_recv - self.previous_network.bytes_recv) / elapsed,
            "sent": max(0, network.bytes_sent - self.previous_network.bytes_sent) / elapsed,
        }
        self.previous_network, self.previous_time = network, now
        snapshot = {
            "hostname": socket.gethostname(), "platform": platform.system(),
            "release": platform.release(), "architecture": platform.machine(),
            "cpu_name": platform.processor() or platform.machine(), "cpu_count": psutil.cpu_count(),
            "cpu_percent": cpu, "uptime": max(0, time.time() - psutil.boot_time()),
            "memory": {"total": memory.total, "used": memory.total - memory.available, "percent": memory.percent},
            "disk": {"total": disk.total, "used": disk.used, "free": disk.free, "percent": disk.percent},
            "network": {"received": point["received"], "sent": point["sent"], "received_total": network.bytes_recv, "sent_total": network.bytes_sent},
            "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            "sampled_at": point["time"],
        }
        with self.lock:
            self.history.append(point)
            self.snapshot = snapshot
            self.sampler_error = None

    def metrics(self):
        with self.lock:
            if self.sampler_error:
                raise PanelError(self.sampler_error, 503)
            return {**self.snapshot, "history": list(self.history)}

    def processes(self):
        now = time.monotonic()
        if now - self.process_snapshot_at >= 2 and self.process_lock.acquire(blocking=False):
            threading.Thread(target=self._refresh_processes, name="harbor-processes", daemon=True).start()
        return {"processes": list(self.process_snapshot["processes"]), "total": self.process_snapshot["total"]}

    def _refresh_processes(self):
        try:
            rows = []
            for process in psutil.process_iter(["pid", "name", "status", "memory_info"]):
                try:
                    info = process.info
                    memory = info["memory_info"]
                    rows.append({"pid": info["pid"], "name": info["name"] or "?", "user": "?", "status": info["status"], "memory": memory.rss if memory else 0})
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            rows.sort(key=lambda item: item["memory"], reverse=True)
            self.process_snapshot = {"processes": rows[:150], "total": len(rows)}
            self.process_snapshot_at = time.monotonic()
        finally:
            self.process_lock.release()

    def docker_command(self, *arguments):
        executable = shutil.which("docker")
        if not executable:
            raise PanelError("Docker CLI is not installed on this host.", 503, code="docker_cli_missing")
        # Docker output may contain user-controlled logs; cap captured bytes as well as time.
        process = subprocess.Popen([executable, *arguments], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False)
        chunks = []
        too_large = threading.Event()

        def collect():
            size = 0
            with process.stdout:
                while True:
                    chunk = process.stdout.read(16384)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 1024 * 1024:
                        too_large.set()
                        process.kill()
                        break
                    chunks.append(chunk)

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            reader.join(timeout=2)
            raise PanelError("Docker operation timed out after 15 seconds.", 504) from exc
        reader.join(timeout=2)
        if reader.is_alive():
            raise PanelError("Docker output stream did not close.", 502)
        output = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if too_large.is_set():
            raise PanelError("Docker output exceeded 1 MiB. Narrow the query.", 413)
        if process.returncode:
            diagnostic = output.lower()
            unix_socket = "unix://" in diagnostic or "dial unix " in diagnostic
            if "permission denied" in diagnostic and unix_socket:
                raise PanelError("The panel service cannot access the Docker socket.", 503, code="docker_socket_permission")
            if "cannot connect to the docker daemon" in diagnostic or (
                unix_socket and ("connection refused" in diagnostic or "no such file or directory" in diagnostic)
            ):
                raise PanelError("The Docker daemon is not reachable.", 503, code="docker_daemon_unavailable")
            raise PanelError(output[:2000] or "Docker command failed.", 502)
        return output

    def containers(self):
        output = self.docker_command("ps", "-a", "--no-trunc", "--format", "{{json .}}")
        rows = []
        for line in output.splitlines():
            try:
                item = json.loads(line)
                rows.append({"id": item["ID"], "name": item["Names"], "image": item["Image"], "state": item["State"], "status": item["Status"], "ports": item.get("Ports", "")})
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise PanelError("Docker returned an unsupported container response.", 502) from exc
        return {"containers": rows}

    def container_config(self, container):
        self.validate_container(container)
        try:
            item = json.loads(self.docker_command("inspect", container))[0]
            config, host = item["Config"], item["HostConfig"]
            ports = []
            for key, bindings in (host.get("PortBindings") or {}).items():
                container_port, protocol = key.split("/", 1)
                for binding in bindings or []:
                    ports.append(f"{binding.get('HostIp', '') + ':' if binding.get('HostIp') else ''}{binding['HostPort']}:{container_port}/{protocol}")
            mounts = [f"{mount['Source']}:{mount['Destination']}" + (":ro" if not mount.get("RW", True) else "") for mount in item.get("Mounts", []) if mount.get("Type") in ("bind", "volume")]
            return {"id": item["Id"], "state": item.get("State", {}).get("Status"), "config": {
                "name": item.get("Name", "").lstrip("/"), "image": config.get("Image", ""),
                "command": config.get("Cmd") or [], "ports": ports,
                "environment": config.get("Env") or [], "volumes": mounts,
                "networks": list((host.get("NetworkMode") or "default").split(",")) if host.get("NetworkMode") not in ("default", "bridge") else (["bridge"] if host.get("NetworkMode") == "bridge" else []),
                "restart_policy": (host.get("RestartPolicy") or {}).get("Name") or "no",
                "memory_limit": host.get("Memory", 0), "cpu_limit": round(host.get("NanoCpus", 0) / 1e9, 3),
            }}
        except (json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise PanelError("Docker returned an unsupported inspect response.", 502) from exc

    @staticmethod
    def validate_container_config(config):
        if not isinstance(config, dict):
            raise PanelError("Container configuration must be an object.")
        name, image = config.get("name"), config.get("image")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name):
            raise PanelError("Container name is invalid.")
        if not isinstance(image, str) or len(image) > 255 or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]*", image):
            raise PanelError("Container image is invalid.")
        fields = ("command", "ports", "environment", "volumes", "networks")
        if any(not isinstance(config.get(field, []), list) or len(config.get(field, [])) > 256 for field in fields):
            raise PanelError("Container list fields must be arrays with at most 256 items.")
        for value in config.get("command", []):
            if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
                raise PanelError("Container command arguments must be strings under 4096 characters.")
        for field, pattern in (("ports", r"(?:[0-9.]+:)?[0-9]{1,5}:[0-9]{1,5}(?:/(?:tcp|udp|sctp))?"), ("volumes", r"[^\x00:]+:[^\x00:]+(?::ro|:rw)?"), ("networks", r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")):
            for value in config.get(field, []):
                if not isinstance(value, str) or len(value) > 4096 or not re.fullmatch(pattern, value):
                    raise PanelError(f"Invalid container {field} entry.")
                if field == "ports":
                    port_parts = value.rsplit("/", 1)[0].split(":")[-2:]
                    if any(int(port) > 65535 for port in port_parts):
                        raise PanelError("Container ports must be between 0 and 65535.")
        for value in config.get("environment", []):
            if not isinstance(value, str) or len(value) > 8192 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", value):
                raise PanelError("Environment entries must use KEY=value format.")
        restart = config.get("restart_policy", "no")
        if restart not in ("no", "always", "unless-stopped", "on-failure"):
            raise PanelError("Unsupported restart policy.")
        memory, cpu = config.get("memory_limit", 0), config.get("cpu_limit", 0)
        if isinstance(memory, bool) or not isinstance(memory, int) or memory < 0 or memory > 2**50:
            raise PanelError("Memory limit must be bytes between 0 and 1 PiB.")
        if isinstance(cpu, bool) or not isinstance(cpu, (int, float)) or not 0 <= cpu <= 1024:
            raise PanelError("CPU limit must be between 0 and 1024 cores.")
        return {**config, "command": config.get("command", []), "ports": config.get("ports", []), "environment": config.get("environment", []), "volumes": config.get("volumes", []), "networks": config.get("networks", []), "restart_policy": restart, "memory_limit": memory, "cpu_limit": cpu}

    def update_container(self, container, config):
        self.validate_container(container)
        config = self.validate_container_config(config)
        inspected = json.loads(self.docker_command("inspect", container))[0]
        current_name = inspected.get("Name", "").lstrip("/")
        if config["name"] != current_name:
            existing = self.docker_command("ps", "-a", "--filter", f"name=^{re.escape(config['name'])}$", "--format", "{{.ID}}")
            if existing:
                raise PanelError("A container with this name already exists.", 409)
        backup = f"harbor-backup-{container[:12]}"
        was_running = inspected.get("State", {}).get("Running", False)
        if was_running:
            self.docker_command("stop", "--time", "10", container)
        self.docker_command("rename", container, backup)
        args = ["run", "-d", "--name", config["name"], "--restart", config["restart_policy"]]
        if config["memory_limit"]:
            args.extend(("--memory", str(config["memory_limit"])))
        if config["cpu_limit"]:
            args.extend(("--cpus", str(config["cpu_limit"])))
        for item in config["ports"]: args.extend(("-p", item))
        for item in config["environment"]: args.extend(("-e", item))
        for item in config["volumes"]: args.extend(("-v", item))
        for item in config["networks"]: args.extend(("--network", item))
        args.append(config["image"])
        args.extend(config["command"])
        try:
            output = self.docker_command(*args)
            new_id = output.strip().splitlines()[0] if output.strip() else ""
            if not re.fullmatch(r"[a-f0-9]{12,64}", new_id):
                raise PanelError("Docker returned an invalid replacement container ID.", 502)
        except PanelError as exc:
            try:
                self.docker_command("rename", backup, current_name)
                if was_running: self.docker_command("start", container)
            except PanelError as recovery_error:
                raise PanelError(f"Container replacement failed: {exc}. Automatic recovery also failed: {recovery_error}", 502) from exc
            raise
        try:
            self.docker_command("rm", "-f", backup)
        except PanelError as exc:
            raise PanelError(f"New container {new_id} is running, but the old backup {backup} could not be removed: {exc}", 502) from exc
        return {"id": new_id, "name": config["name"], "recreated": True}

    @staticmethod
    def validate_container(container):
        if not isinstance(container, str) or not re.fullmatch(r"[a-f0-9]{12,64}", container):
            raise PanelError("A valid container ID is required.")

    def container_action(self, container, action):
        self.validate_container(container)
        if action not in ("start", "stop", "restart"):
            raise PanelError("Unsupported container action.")
        arguments = (action, "--time", "5", container) if action in ("stop", "restart") else (action, container)
        return {"output": self.docker_command(*arguments), "action": action}

    def container_logs(self, container):
        self.validate_container(container)
        return {"output": self.docker_command("logs", "--tail", "150", "--timestamps", container)}

    def terminal(self, command):
        if not isinstance(command, str):
            raise PanelError("Enter a command.")
        if self.full_host:
            if not command.strip() or "\x00" in command or len(command) > 4096:
                raise PanelError("Commands must contain 1 to 4096 characters and no null bytes.")
            if command.strip() == "help":
                identity = "the root account" if hasattr(os, "geteuid") and os.geteuid() == 0 else "the service account"
                return {"command": command, "output": f"Full-host shell command runner. Commands execute as {identity}; use standard shell syntax.", "exit_code": 0}
            return self.run_command(command)
        if command.strip() not in self.COMMANDS:
            raise PanelError("Unsupported command. Enter 'help' to see available diagnostics.")
        command = command.strip()
        metrics = self.metrics()
        if command == "help":
            output = "Available diagnostics (no interactive shell):\n" + "\n".join(self.COMMANDS)
        elif command == "hostname":
            output = socket.gethostname()
        elif command == "whoami":
            output = getpass.getuser()
        elif command == "uptime":
            output = f"Uptime: {metrics['uptime'] / 3600:.2f} hours | CPU: {metrics['cpu_percent']}%"
        elif command == "df -h":
            disk = metrics["disk"]
            output = f"File root volume: {disk['used'] / 2**30:.2f} / {disk['total'] / 2**30:.2f} GiB ({disk['percent']}%)"
        elif command == "free -h":
            memory = metrics["memory"]
            output = f"Memory: {memory['used'] / 2**30:.2f} / {memory['total'] / 2**30:.2f} GiB ({memory['percent']}%)"
        elif command == "ps":
            output = "PID      MEMORY MiB   NAME\n" + "\n".join(f"{p['pid']:<8} {p['memory'] / 2**20:>10.1f}   {p['name']}" for p in self.processes()["processes"][:30])
        else:
            output = self.docker_command("ps")
        return {"command": command, "output": output}

    def run_command(self, command):
        if os.name == "nt":
            arguments = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
            process_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            arguments = ["/bin/sh", "-c", command]
            process_options = {"start_new_session": True}
        process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, **process_options)
        chunks = []
        reader_errors = []
        too_large = threading.Event()

        def terminate_tree():
            if os.name == "nt":
                try:
                    result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
                except subprocess.TimeoutExpired:
                    if process.poll() is None:
                        process.kill()
                    return
                if result.returncode and process.poll() is None:
                    process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        def collect():
            size = 0
            try:
                with process.stdout:
                    while True:
                        chunk = process.stdout.read(16384)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > self.MAX_COMMAND_OUTPUT and not too_large.is_set():
                            too_large.set()
                            terminate_tree()
                        elif not too_large.is_set():
                            chunks.append(chunk)
            except OSError as exc:
                reader_errors.append(exc)

        reader = threading.Thread(target=collect, name="harbor-command-output", daemon=True)
        reader.start()
        try:
            process.wait(timeout=self.COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            terminate_tree()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired as cleanup_error:
                    raise PanelError("Command process could not be stopped after timeout.", 502) from cleanup_error
            reader.join(timeout=2)
            if reader.is_alive():
                raise PanelError("Command output stream did not close after timeout cleanup.", 502) from exc
            raise PanelError(f"Command timed out after {self.COMMAND_TIMEOUT} seconds.", 504) from exc
        reader.join(timeout=2)
        if reader.is_alive():
            terminate_tree()
            reader.join(timeout=2)
            if reader.is_alive():
                raise PanelError("Command output stream did not close after child processes were stopped.", 502)
        if too_large.is_set():
            raise PanelError("Command output exceeded 1 MiB.", 413)
        if reader_errors:
            raise PanelError(f"Could not read command output: {reader_errors[0]}", 502)
        return {"command": command, "output": b"".join(chunks).decode("utf-8", errors="replace"), "exit_code": process.returncode}

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
