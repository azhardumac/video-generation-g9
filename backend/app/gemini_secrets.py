"""Backend-only Gemini API key storage with environment override and 0600 file fallback."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


class SecretStoreError(RuntimeError):
    pass


class GeminiSecretStore:
    def __init__(self, path: str | Path, env: dict[str, str] | None = None):
        self.path = Path(path).expanduser()
        self.env = os.environ if env is None else env

    def get(self) -> tuple[str | None, str | None]:
        value = self.env.get("GEMINI_API_KEY", "").strip()
        if value:
            return value, "environment"
        if self.path.is_symlink():
            raise SecretStoreError("Gemini secret file cannot be a symbolic link")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            raise SecretStoreError("The protected Gemini secret file could not be read") from exc
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            if metadata.st_size > 4096:
                raise SecretStoreError("Gemini secret file exceeds the expected size")
            if os.name == "posix" and metadata.st_mode & 0o077:
                raise SecretStoreError("Gemini secret file permissions are too broad; set its permissions to 0600")
            raw = handle.read(4097)
        try:
            value = json.loads(raw).get("gemini_api_key")
        except (json.JSONDecodeError, AttributeError) as exc:
            raise SecretStoreError("The protected Gemini secret file is malformed") from exc
        if not isinstance(value, str) or not value.strip():
            return None, None
        return value.strip(), "local_file"

    def save(self, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 512 or any(char in value for char in "\r\n\x00"):
            raise ValueError("Enter a valid Gemini API key (1–512 characters, without line breaks).")
        if self.env.get("GEMINI_API_KEY", "").strip():
            raise SecretStoreError("GEMINI_API_KEY is supplied by the backend environment; remove or change it there instead of replacing it in Settings.")
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink() or self.path.is_symlink():
            raise SecretStoreError("Gemini secret storage cannot use symbolic links")
        if os.name == "posix":
            parent.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix=".gemini-secret-", dir=parent)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"gemini_api_key": value}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if os.name == "posix":
                self.path.chmod(0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return "local_file"

    def delete(self) -> str | None:
        if self.env.get("GEMINI_API_KEY", "").strip():
            raise SecretStoreError("GEMINI_API_KEY is supplied by the backend environment and cannot be removed from Settings.")
        if self.path.is_symlink():
            raise SecretStoreError("Gemini secret storage cannot use symbolic links")
        try:
            self.path.unlink()
            return "local_file"
        except FileNotFoundError:
            return None
