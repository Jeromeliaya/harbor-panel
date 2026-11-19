"""Real host telemetry and bounded, structured Docker operations."""

from collections import deque
import getpass
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time

import psutil

from files import PanelError


class Host:
    COMMANDS = ("help", "hostname", "whoami", "uptime", "df -h", "free -h", "ps", "docker ps")

    def __init__(self, disk_path):
        self.disk_path = str(disk_path)
        self.lock = threading.Lock()
        self.history = deque(maxlen=90)
        self.stop = threading.Event()
        self.previous_network = psutil.net_io_counters()
        self.previous_time = time.monotonic()
        self.snapshot = {}
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
        rows = []
        for process in psutil.process_iter(["pid", "name", "username", "status", "memory_info"]):
            try:
                info = process.info
                memory = info["memory_info"]
                rows.append({"pid": info["pid"], "name": info["name"] or "?", "user": info["username"] or "?", "status": info["status"], "memory": memory.rss if memory else 0})
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        rows.sort(key=lambda item: item["memory"], reverse=True)
        return {"processes": rows[:150], "total": len(rows)}

    def docker_command(self, *arguments):
        executable = shutil.which("docker")
        if not executable:
            raise PanelError("Docker CLI is not installed on this host.", 503)
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
        if not isinstance(command, str) or command.strip() not in self.COMMANDS:
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

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
