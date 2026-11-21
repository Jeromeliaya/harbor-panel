"""Scoped text file management with optimistic concurrency."""

import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import tempfile
import threading


class PanelError(Exception):
    def __init__(self, message, status=400, *, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class FileStore:
    MAX_TEXT = 256 * 1024
    MAX_DOWNLOAD = 8 * 1024 * 1024

    def __init__(self, root, protected_paths=(), allow_symlinks=False):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.protected_paths = tuple(Path(path).resolve() for path in protected_paths)
        self.allow_symlinks = allow_symlinks
        self.lock = threading.RLock()

    def _protected(self, target):
        resolved = Path(target).resolve()
        return any(resolved == path or resolved.is_relative_to(path) for path in self.protected_paths)

    def resolve(self, relative=""):
        if not isinstance(relative, str) or any(c in relative for c in ("\\", ":", "\x00")):
            raise PanelError("Invalid file path.", 400)
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts:
            raise PanelError("Path must remain inside the configured file root.", 403)
        target = self.root
        for part in path.parts:
            if PureWindowsPath(part).is_reserved() or part.endswith((" ", ".")) or any(ord(c) < 32 or c in '<>"|?*' for c in part):
                raise PanelError("Use a portable file name without reserved characters.")
            target = target / part
            if not self.allow_symlinks and (target.is_symlink() or (hasattr(target, "is_junction") and target.is_junction())):
                raise PanelError("Symbolic links and junctions cannot be followed.", 403)
            if self.allow_symlinks:
                target = target.resolve()
                if not target.is_relative_to(self.root):
                    raise PanelError("Path is outside the configured file root.", 403)
        if not target.resolve().is_relative_to(self.root):
            raise PanelError("Path is outside the configured file root.", 403)
        if self._protected(target):
            raise PanelError("Panel authentication and state files are not accessible through file management.", 403)
        return target

    def listing(self, relative=""):
        folder = self.resolve(relative)
        if not folder.is_dir():
            raise PanelError("Directory not found.", 404)
        entries = []
        truncated = False
        with os.scandir(folder) as iterator:
            for index, entry in enumerate(iterator):
                if index >= 2000:
                    truncated = True
                    break
                if self._protected(entry.path):
                    continue
                linked = entry.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(entry.path))
                try:
                    info = entry.stat(follow_symlinks=self.allow_symlinks)
                except FileNotFoundError:
                    info = entry.stat(follow_symlinks=False)
                entries.append({
                    "name": entry.name,
                    "directory": entry.is_dir(follow_symlinks=self.allow_symlinks),
                    "linked": linked,
                    "size": info.st_size,
                    "modified": info.st_mtime,
                })
        entries.sort(key=lambda e: (not e["directory"], e["name"].casefold()))
        return {"path": "" if folder == self.root else folder.relative_to(self.root).as_posix(), "entries": entries, "truncated": truncated}

    def _bytes(self, relative, limit):
        target = self.resolve(relative)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(target, flags)
        except FileNotFoundError as exc:
            raise PanelError("File not found.", 404) from exc
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise PanelError("Only regular files are supported.")
            if info.st_size > limit:
                raise PanelError("File exceeds the size limit.", 413)
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise PanelError("File exceeds the size limit.", 413)
        return data

    def read(self, relative):
        data = self._bytes(relative, self.MAX_TEXT)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PanelError("The editor supports UTF-8 text files only.", 415) from exc
        if "\x00" in text:
            raise PanelError("Binary files cannot be edited.", 415)
        return {"path": relative, "content": text, "etag": hashlib.sha256(data).hexdigest()}

    def download(self, relative):
        return self._bytes(relative, self.MAX_DOWNLOAD)

    def write(self, relative, content, etag):
        if not isinstance(content, str) or "\x00" in content:
            raise PanelError("Content must be UTF-8 text without null bytes.")
        data = content.encode("utf-8")
        if len(data) > self.MAX_TEXT:
            raise PanelError("Text files are limited to 256 KiB.", 413)
        with self.lock:
            target = self.resolve(relative)
            if target == self.root or not target.parent.is_dir():
                raise PanelError("Choose a file inside an existing directory.")
            if etag is None:
                try:
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                except FileExistsError as exc:
                    raise PanelError("File already exists. Refresh before editing.", 409) from exc
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
            else:
                current = self.read(relative)
                if not isinstance(etag, str) or current["etag"] != etag:
                    raise PanelError("File changed on disk. Reload before saving.", 409)
                original = target.stat()
                mode = stat.S_IMODE(original.st_mode)
                descriptor, temporary = tempfile.mkstemp(prefix=".harbor-", dir=target.parent)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                        if hasattr(os, "fchown") and hasattr(os, "geteuid") and os.geteuid() == 0:
                            os.fchown(stream.fileno(), original.st_uid, original.st_gid)
                    os.chmod(temporary, mode)
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        return {"path": relative, "etag": hashlib.sha256(data).hexdigest()}

    def mkdir(self, relative):
        target = self.resolve(relative)
        try:
            target.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise PanelError("An entry with this name already exists.", 409) from exc
        return {"path": relative}
