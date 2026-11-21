"""Authenticated WebSocket transport for local and remote interactive SSH shells."""

import base64
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit

import paramiko
from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, BytesMessage, CloseConnection, Ping, Pong, Request, TextMessage
from wsproto.utilities import LocalProtocolError, RemoteProtocolError

from files import PanelError
from local_ssh import LocalSSH


MAX_MESSAGE = 16 * 1024
MAX_OUTPUT_CHUNKS_PER_TICK = 4
LOG = logging.getLogger(__name__)


def _fingerprint(key):
    digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
    return "SHA256:" + digest


def _remote_host(value):
    if not isinstance(value, str) or not value or len(value) > 253 or value != value.strip():
        raise PanelError("Enter a valid remote VPS host name or IP address.")
    if any(char in value for char in " /\\\t\r\n\x00"):
        raise PanelError("Enter a valid remote VPS host name or IP address.")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        if not re.fullmatch(r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?", value):
            raise PanelError("Enter a valid remote VPS host name or IP address.")
        return value


class _Peer:
    def __init__(self, connection, websocket):
        self.connection = connection
        self.websocket = websocket
        self.protocol_lock = threading.RLock()
        self.closed = threading.Event()

    def send(self, event):
        with self.protocol_lock:
            if self.closed.is_set():
                return
            data = self.websocket.send(event)
            if data:
                self.connection.sendall(data)

    def receive(self, data):
        with self.protocol_lock:
            self.websocket.receive_data(data)
            return tuple(self.websocket.events())

    def control(self, message):
        data = json.dumps(message, ensure_ascii=True, separators=(",", ":"))
        if len(data.encode("utf-8")) > MAX_MESSAGE:
            raise ValueError("Terminal control message is too large.")
        self.send(TextMessage(data=data))

    def close(self, code=1000, reason="Session closed"):
        if self.closed.is_set():
            return
        try:
            self.send(CloseConnection(code=code, reason=reason[:120]))
        except (OSError, LocalProtocolError):
            pass
        self.closed.set()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass


class _HostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, session, generation):
        self.session = session
        self.generation = generation

    def missing_host_key(self, client, hostname, key):
        self.session.confirm_host_key(client, hostname, key, self.generation)


class _TerminalSession:
    def __init__(self, owner, peer, manager, address=""):
        self.id = secrets.token_urlsafe(16)
        self.owner = owner
        self.peer = peer
        self.manager = manager
        self.address = address
        self.target = None
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.client = None
        self.pending_client = None
        self.channel = None
        self.pending_key = None
        self.connect_generation = 0

    def close_ssh(self):
        with self.lock:
            self.connect_generation += 1
            pending = self.pending_key
            self.pending_key = None
            if pending:
                pending["accepted"] = False
                pending["event"].set()
            channel, self.channel = self.channel, None
            client, self.client = self.client, None
            pending_client, self.pending_client = self.pending_client, None
            target, self.target = self.target, None
        resources = (channel, client, pending_client if pending_client is not client else None)
        for resource in resources:
            if resource is not None:
                try:
                    resource.close()
                except (OSError, EOFError, paramiko.SSHException) as exc:
                    LOG.warning("SSH connection ended during cleanup: %s", type(exc).__name__)
        if target:
            self.manager.record("ssh.disconnect", target, self.address)

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        self.close_ssh()
        self.peer.close()

    def confirm_host_key(self, client, hostname, key, generation):
        challenge = secrets.token_urlsafe(18)
        pending = {"challenge": challenge, "event": threading.Event(), "accepted": False}
        with self.lock:
            if self.closed.is_set() or generation != self.connect_generation:
                raise paramiko.SSHException("Terminal session ended.")
            self.pending_key = pending
        self.peer.control({"type": "host_key", "challenge": challenge, "host": hostname, "key_type": key.get_name(), "fingerprint": _fingerprint(key)})
        if not pending["event"].wait(120):
            raise paramiko.SSHException("SSH host-key confirmation timed out.")
        with self.lock:
            if self.pending_key is pending:
                self.pending_key = None
            if not pending["accepted"] or self.closed.is_set() or generation != self.connect_generation:
                raise paramiko.SSHException("SSH host key was not accepted.")
        self.manager.remember_host_key(client, hostname, key)

    def accept_host_key(self, challenge, accepted):
        with self.lock:
            pending = self.pending_key
            if pending and secrets.compare_digest(pending["challenge"], challenge):
                pending["accepted"] = accepted
                pending["event"].set()

    def connect(self, message, secure_transport):
        target = message.get("target")
        if target == "local":
            hostname, port, username, password = "127.0.0.1", None, "root", None
        elif target == "remote":
            if not secure_transport:
                self.peer.control({"type": "error", "message": "Remote password SSH is available only through the configured HTTPS panel URL."})
                return
            hostname = _remote_host(message.get("host"))
            port = message.get("port", 22)
            if type(port) is not int or not 1 <= port <= 65535:
                raise PanelError("SSH port must be an integer from 1 to 65535.")
            username = message.get("username", "root")
            password = message.get("password")
            if username != "root" or not isinstance(password, str) or not password or len(password) > 1024:
                raise PanelError("Remote SSH requires the root user and a password of 1 to 1024 characters.")
        else:
            raise PanelError("Choose the local VPS or a remote VPS SSH connection.")

        cols = self._dimension(message.get("cols"), 100, 20, 300)
        rows = self._dimension(message.get("rows"), 28, 5, 100)
        self.close_ssh()
        with self.lock:
            if self.closed.is_set():
                return
            generation = self.connect_generation
        if not self.manager.connect_slots.acquire(blocking=False):
            self.peer.control({"type": "error", "message": "SSH connection capacity is busy. Wait for another connection attempt to finish."})
            return
        worker = threading.Thread(
            target=self._connect_worker,
            args=(generation, target, hostname, port, username, password, cols, rows),
            name="harbor-ssh-connect",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError as exc:
            self.manager.connect_slots.release()
            raise PanelError("Could not start the SSH connection worker.", 503) from exc

    @staticmethod
    def _dimension(value, default, minimum, maximum):
        if type(value) is not int:
            return default
        return max(minimum, min(maximum, value))

    def _connect_worker(self, generation, target, hostname, port, username, password, cols, rows):
        client = paramiko.SSHClient()
        audit_target = "local:root" if target == "local" else f"root@{hostname}:{port}"
        connected = False
        try:
            with self.lock:
                if self.closed.is_set() or generation != self.connect_generation:
                    return
                self.pending_client = client
            key = None
            if target == "local":
                port, key, host_key = self.manager.local.connection()
                client.get_host_keys().add(f"[{hostname}]:{port}", host_key.get_name(), host_key)
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                client.load_system_host_keys()
                with self.manager.host_keys_lock:
                    if self.manager.host_keys_path.is_file():
                        client.load_host_keys(str(self.manager.host_keys_path))
                client.set_missing_host_key_policy(_HostKeyPolicy(self, generation))
            with self.lock:
                if self.closed.is_set() or generation != self.connect_generation:
                    return
            client.connect(
                hostname=hostname,
                port=port,
                username=username,
                password=password,
                pkey=key,
                allow_agent=False,
                look_for_keys=False,
                timeout=12,
                banner_timeout=12,
                auth_timeout=12,
            )
            client.get_transport().set_keepalive(30)
            channel = client.invoke_shell(term="xterm-256color", width=cols, height=rows)
            channel.settimeout(1)
            with self.lock:
                if self.closed.is_set() or generation != self.connect_generation:
                    channel.close()
                    client.close()
                    return
                self.pending_client = None
                self.client, self.channel = client, channel
                self.target = audit_target
                connected = True
                self.manager.record("ssh.connect", audit_target, self.address)
            self.peer.control({"type": "connected", "target": target, "host": hostname, "username": username})
        except PanelError as exc:
            self._connection_error(generation, str(exc))
        except paramiko.BadHostKeyException:
            self._connection_error(generation, "SSH host key changed. The connection was blocked; verify the server key out of band.")
        except paramiko.AuthenticationException:
            self._connection_error(generation, "SSH authentication failed. Check the remote root password or the private local SSH log.")
        except paramiko.NoValidConnectionsError:
            self._connection_error(generation, "SSH service is unreachable. Check the host, port, firewall, and sshd service.")
        except (paramiko.SSHException, OSError, EOFError, ValueError):
            self._connection_error(generation, "SSH connection failed. Verify the host, SSH service, credentials, and host-key confirmation.")
        finally:
            if not connected:
                self.manager.record("ssh.connect", audit_target, self.address, False)
            with self.lock:
                if self.pending_client is client:
                    self.pending_client = None
                current = self.client is client
            if not current:
                try:
                    client.close()
                except (OSError, EOFError, paramiko.SSHException) as exc:
                    LOG.warning("SSH connection ended during cleanup: %s", type(exc).__name__)
            password = None
            self.manager.connect_slots.release()

    def _connection_error(self, generation, message):
        with self.lock:
            if self.closed.is_set() or generation != self.connect_generation:
                return
        try:
            self.peer.control({"type": "error", "message": message})
        except (OSError, LocalProtocolError):
            pass

    def resize(self, message):
        cols = self._dimension(message.get("cols"), 100, 20, 300)
        rows = self._dimension(message.get("rows"), 28, 5, 100)
        with self.lock:
            channel = self.channel
        if channel and channel.active:
            channel.resize_pty(width=cols, height=rows)

    def write(self, data):
        if len(data) > MAX_MESSAGE:
            raise PanelError("Terminal input exceeded the size limit.", 413)
        with self.lock:
            channel = self.channel
        if not channel or not channel.active:
            self.peer.control({"type": "error", "message": "Connect to an SSH server before typing."})
            return
        channel.sendall(data)

    def pump_output(self):
        with self.lock:
            channel = self.channel
        if not channel:
            return
        for _ in range(MAX_OUTPUT_CHUNKS_PER_TICK):
            if not channel.active or not channel.recv_ready():
                break
            data = channel.recv(MAX_MESSAGE)
            if not data:
                break
            self.peer.send(BytesMessage(data=data))
        if channel.closed:
            self.close_ssh()
            self.peer.control({"type": "disconnected", "message": "SSH session ended."})


class TerminalManager:
    MAX_SESSIONS = 8
    MAX_OWNER_SESSIONS = 3

    def __init__(self, state_dir, native_root=False, audit=None):
        self.directory = Path(state_dir)
        self.host_keys_path = self.directory / "ssh_known_hosts"
        self.host_keys_lock = threading.RLock()
        self.lock = threading.RLock()
        self.sessions = {}
        self.connect_slots = threading.BoundedSemaphore(4)
        self.local = LocalSSH(state_dir, native_root)
        self.audit = audit
        self.stopped = False

    def record(self, action, target, address, success=True):
        if self.audit is not None:
            try:
                self.audit.add(action, target, address, success)
            except Exception:
                LOG.exception("Could not record SSH lifecycle event")

    def remember_host_key(self, client, hostname, key):
        with self.host_keys_lock:
            host_keys = paramiko.HostKeys()
            if self.host_keys_path.is_file():
                host_keys.load(str(self.host_keys_path))
            host_keys.add(hostname, key.get_name(), key)
            descriptor, temporary = tempfile.mkstemp(prefix=".harbor-ssh-", dir=self.directory)
            os.close(descriptor)
            try:
                host_keys.save(temporary)
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.host_keys_path)
                client.get_host_keys().add(hostname, key.get_name(), key)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def _reserve(self, session):
        with self.lock:
            if self.stopped:
                raise PanelError("The terminal service is shutting down.", 503)
            owned = sum(item.owner == session.owner for item in self.sessions.values())
            if len(self.sessions) >= self.MAX_SESSIONS or owned >= self.MAX_OWNER_SESSIONS:
                raise PanelError("The interactive terminal session limit has been reached.", 429)
            self.sessions[session.id] = session

    def _release(self, session):
        with self.lock:
            self.sessions.pop(session.id, None)

    def close_owner(self, owner):
        with self.lock:
            sessions = [session for session in self.sessions.values() if session.owner == owner]
        for session in sessions:
            session.close()

    def close_all(self):
        with self.lock:
            self.stopped = True
            sessions = list(self.sessions.values())
        for session in sessions:
            session.close()
        self.local.close(shutdown=True)

    def serve(self, handler, owner, auth_session, secure_transport):
        if handler.command != "GET" or urlsplit(handler.path).path != "/api/terminal/ws":
            raise PanelError("Invalid WebSocket upgrade request.", 400)
        origin = handler.headers.get("Origin")
        host = handler.headers.get("Host", "").lower()
        expected_public_origin = "http://" + host
        if origin not in handler.server.origins and not (handler.server.public_bind and not secure_transport and origin == expected_public_origin):
            raise PanelError("WebSocket Origin is not allowed.", 403)
        csrf = auth_session["csrf"]
        websocket = WSConnection(ConnectionType.SERVER)
        headers = [(name.lower().encode("ascii"), value.encode("latin-1")) for name, value in handler.headers.raw_items()]
        try:
            websocket.initiate_upgrade_connection(headers, handler.path)
            events = tuple(websocket.events())
        except (LocalProtocolError, RemoteProtocolError, ValueError) as exc:
            raise PanelError("Invalid WebSocket handshake.", 400) from exc
        request = next((event for event in events if isinstance(event, Request)), None)
        if request is None or csrf not in request.subprotocols:
            raise PanelError("WebSocket CSRF validation failed.", 403)
        peer = _Peer(handler.connection, websocket)
        session = _TerminalSession(owner, peer, self, handler.client_address[0])
        self._reserve(session)
        try:
            response = websocket.send(AcceptConnection(subprotocol=csrf))
            handler.close_connection = True
            handler.connection.sendall(response)
            handler.connection.settimeout(0.5)
            last_ping = time.monotonic()
            fragment_kind = None
            fragment_data = bytearray()
            while not session.closed.is_set() and not peer.closed.is_set() and handler.server.auth.has_session(owner):
                try:
                    chunk = handler.connection.recv(65536)
                except socket.timeout:
                    chunk = None
                if chunk == b"":
                    break
                if chunk:
                    for event in peer.receive(chunk):
                        if isinstance(event, CloseConnection):
                            try:
                                peer.send(CloseConnection(code=event.code, reason=event.reason or ""))
                            except (OSError, LocalProtocolError):
                                pass
                            return
                        if isinstance(event, Ping):
                            peer.send(Pong(payload=event.payload))
                        elif isinstance(event, (TextMessage, BytesMessage)):
                            kind = "text" if isinstance(event, TextMessage) else "binary"
                            piece = event.data.encode("utf-8") if kind == "text" else event.data
                            if fragment_kind is not None and fragment_kind != kind:
                                raise PanelError("Invalid fragmented terminal message.", 400)
                            fragment_kind = kind
                            if len(fragment_data) + len(piece) > MAX_MESSAGE:
                                raise PanelError("Terminal message exceeded the size limit.", 413)
                            fragment_data.extend(piece)
                            if event.message_finished:
                                if kind == "text":
                                    try:
                                        control = bytes(fragment_data).decode("utf-8")
                                    except UnicodeDecodeError as exc:
                                        raise PanelError("Terminal control message is not valid UTF-8.") from exc
                                    self._control(session, control, secure_transport)
                                else:
                                    session.write(bytes(fragment_data))
                                fragment_kind = None
                                fragment_data.clear()
                session.pump_output()
                if time.monotonic() - last_ping >= 25:
                    peer.send(Ping(payload=b"harbor"))
                    last_ping = time.monotonic()
        except (ConnectionError, OSError, EOFError, paramiko.SSHException, LocalProtocolError, RemoteProtocolError, PanelError, ValueError) as exc:
            if not peer.closed.is_set() and not session.closed.is_set():
                try:
                    peer.control({"type": "error", "message": str(exc)[:240]})
                except (ConnectionError, OSError, LocalProtocolError):
                    pass
        finally:
            session.close()
            self._release(session)

    def _control(self, session, data, secure_transport):
        try:
            message = json.loads(data)
        except json.JSONDecodeError as exc:
            raise PanelError("Invalid terminal control message.") from exc
        if not isinstance(message, dict):
            raise PanelError("Invalid terminal control message.")
        action = message.get("type")
        if action == "connect":
            session.connect(message, secure_transport)
        elif action == "host_key":
            challenge = message.get("challenge")
            if not isinstance(challenge, str):
                raise PanelError("Invalid SSH host-key confirmation.")
            session.accept_host_key(challenge, message.get("accepted") is True)
        elif action == "resize":
            session.resize(message)
        elif action == "disconnect":
            session.close_ssh()
            session.peer.control({"type": "disconnected", "message": "SSH session disconnected."})
        else:
            raise PanelError("Unknown terminal control message.")
