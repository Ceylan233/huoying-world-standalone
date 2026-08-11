#!/usr/bin/env python3
"""Small JSON document store used by the local GM/operations panel.

This build deliberately keeps saves as ordinary JSON files.  No SQLite
database or authentication table is created.
"""

from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAVE_ROOT = PROJECT_ROOT / "save"


def _safe_relative_key(key: str) -> Path:
    value = str(key or "").replace("\\", "/").strip("/")
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid document key")
    return path


def _path_for(scope: str, key: str) -> Path:
    relative = _safe_relative_key(key)
    if scope == "root":
        base = SAVE_ROOT
    elif scope == "multiplayer":
        base = SAVE_ROOT / "multiplayer"
    elif scope == "characters":
        base = SAVE_ROOT / "multiplayer" / "characters"
    else:
        raise ValueError("invalid document scope")
    target = (base / relative).resolve()
    resolved_base = base.resolve()
    try:
        target.relative_to(resolved_base)
    except ValueError as exc:
        raise ValueError("document key escapes save directory") from exc
    return target


def scope_key_for_path(path: str | os.PathLike[str]) -> tuple[str, str]:
    """Map a save path to the scope/key pair used by the GM panel."""
    target = Path(path).resolve()
    characters = (SAVE_ROOT / "multiplayer" / "characters").resolve()
    multiplayer = (SAVE_ROOT / "multiplayer").resolve()
    root = SAVE_ROOT.resolve()
    for scope, base in (
        ("characters", characters),
        ("multiplayer", multiplayer),
        ("root", root),
    ):
        try:
            return scope, target.relative_to(base).as_posix()
        except ValueError:
            continue
    raise ValueError("path is outside the save directory")


class JsonDocumentStore:
    """Thread-safe, atomic JSON text storage over the existing save tree."""

    def __init__(self) -> None:
        self.lock = threading.RLock()

    def read(self, scope: str, key: str) -> str | None:
        path = _path_for(scope, key)
        with self.lock:
            try:
                return path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None

    def write(self, scope: str, key: str, content: str) -> None:
        path = _path_for(scope, key)
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}."
                f"{secrets.token_hex(4)}.tmp"
            )
            try:
                temporary.write_text(str(content), encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    def delete(self, scope: str, key: str) -> None:
        path = _path_for(scope, key)
        with self.lock:
            path.unlink(missing_ok=True)

    def list(self, scope: str) -> list[str]:
        base = _path_for(scope, "placeholder").parent
        with self.lock:
            if not base.is_dir():
                return []
            candidates = base.rglob("*.json") if scope == "characters" else base.glob("*.json")
            return sorted(
                path.relative_to(base).as_posix()
                for path in candidates
                if path.is_file()
            )


_DOCUMENT_STORE = JsonDocumentStore()


def get_document_store() -> JsonDocumentStore:
    return _DOCUMENT_STORE
