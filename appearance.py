"""Persistent single-administrator appearance preferences and safe background images."""

import json
import os
from pathlib import Path
import tempfile
import threading
import warnings
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from files import PanelError


THEMES = ("botanical", "topography", "nocturne", "terracotta")


class AppearanceStore:
    MAX_BACKGROUND = 1_500_000
    MAX_PIXELS = 8_000_000
    MAX_STORED_BACKGROUND = 3_000_000
    FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "appearance.json"
        self.image_path = self.directory / "background-image"
        self.lock = threading.RLock()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.settings = {"theme": "botanical", "background_type": None}
        if self.path.is_file():
            try:
                stored = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Appearance settings are unreadable.") from exc
            if not isinstance(stored, dict) or stored.get("theme") not in THEMES or stored.get("background_type") not in (None, *self.FORMATS):
                raise RuntimeError("Appearance settings contain an invalid theme or image type.")
            if stored.get("background_type") is not None and not self.image_path.is_file():
                raise RuntimeError("The configured background image is missing.")
            self.settings = {"theme": stored["theme"], "background_type": stored.get("background_type")}
        if self.path.exists():
            os.chmod(self.path, 0o600)

    def get(self):
        with self.lock:
            return {"theme": self.settings["theme"], "has_background": self.settings["background_type"] is not None}

    def set_theme(self, theme):
        if not isinstance(theme, str) or theme not in THEMES:
            raise PanelError("Choose one of the available panel themes.")
        with self.lock:
            settings = {**self.settings, "theme": theme}
            self._save(settings)
            self.settings = settings
            return self.get()

    def background(self):
        with self.lock:
            content_type = self.settings["background_type"]
            if content_type is None:
                raise PanelError("No background image has been uploaded.", 404)
            try:
                data = self.image_path.read_bytes()
            except FileNotFoundError as exc:
                raise PanelError("No background image has been uploaded.", 404) from exc
            return content_type, data

    def set_background(self, content_type, data):
        if not isinstance(data, bytes) or not 0 < len(data) <= self.MAX_BACKGROUND:
            raise PanelError("Background images must be no larger than 1.5 MB (1,500,000 bytes).", 413)
        image_format = self.FORMATS.get(content_type)
        if image_format is None:
            raise PanelError("Upload a PNG, JPEG, or WebP image.", 415)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as source:
                    if source.format != image_format:
                        raise PanelError("The image contents do not match the declared file type.", 415)
                    if source.width * source.height > self.MAX_PIXELS:
                        raise PanelError("Background images must be at most 8 megapixels.", 413)
                    source.verify()
                with Image.open(BytesIO(data)) as source:
                    source.load()
                    source.thumbnail((2560, 1600), Image.Resampling.LANCZOS)
                    has_alpha = source.mode in ("RGBA", "LA") or (source.mode == "P" and "transparency" in source.info)
                    with source.convert("RGBA" if has_alpha else "RGB") as cleaned:
                        cleaned.info.clear()
                        output = BytesIO()
                        options = {"optimize": True}
                        if image_format == "JPEG":
                            options["quality"] = 88
                        cleaned.save(output, format=image_format, **options)
                        cleaned_bytes = output.getvalue()
        except PanelError:
            raise
        except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError) as exc:
            raise PanelError("Upload a valid, decodable PNG, JPEG, or WebP image.", 415) from exc
        if len(cleaned_bytes) > self.MAX_STORED_BACKGROUND:
            raise PanelError("The optimized background image exceeds 3 MiB.", 413)
        with self.lock:
            previous_image = self.image_path.read_bytes() if self.image_path.is_file() else None
            settings = {**self.settings, "background_type": content_type}
            self._atomic_write(self.image_path, cleaned_bytes)
            try:
                self._save(settings)
            except OSError:
                if previous_image is None:
                    self.image_path.unlink(missing_ok=True)
                else:
                    self._atomic_write(self.image_path, previous_image)
                raise
            self.settings = settings
            return self.get()

    def remove_background(self):
        with self.lock:
            previous_image = self.image_path.read_bytes() if self.image_path.is_file() else None
            settings = {**self.settings, "background_type": None}
            if self.image_path.exists():
                self.image_path.unlink()
            try:
                self._save(settings)
            except OSError:
                if previous_image is not None:
                    self._atomic_write(self.image_path, previous_image)
                raise
            self.settings = settings
            return self.get()

    def _save(self, settings=None):
        data = json.dumps(self.settings if settings is None else settings, separators=(",", ":")).encode("utf-8")
        self._atomic_write(self.path, data)

    @staticmethod
    def _atomic_write(path, data):
        descriptor, temporary = tempfile.mkstemp(prefix=".harbor-appearance-", dir=path.parent)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
