#!/usr/bin/env python3
"""Persistent, thread-safe state shared by the GM web panel and game server."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GM_STATE_PATH = Path(
    os.environ.get("NARUTO_GM_STATE_PATH", str(PROJECT_ROOT / "save" / "gm-state.json"))
)

MANUAL_RANK_TYPES = ("MENGMEI_PH", "REBUG_CHARA", "SHUAIGE_PH")


class GmStateStore:
    """Keep administrative overrides durable without coupling them to a session."""

    def __init__(self, path: Path = GM_STATE_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "version": 1,
            "rankings": {rank_type: [] for rank_type in MANUAL_RANK_TYPES},
            "weather": {"type": -1, "expiresAt": 0},
            "events": {},
        }

    def _load(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    payload = raw
            except (OSError, UnicodeError, json.JSONDecodeError):
                payload = {}
        normalized = self._defaults()
        rankings = payload.get("rankings")
        if isinstance(rankings, dict):
            for rank_type in MANUAL_RANK_TYPES:
                values = rankings.get(rank_type, [])
                if not isinstance(values, list):
                    continue
                normalized["rankings"][rank_type] = list(
                    dict.fromkeys(
                        int(value)
                        for value in values
                        if str(value).isdigit() and int(value) > 0
                    )
                )[:100]
        weather = payload.get("weather")
        if isinstance(weather, dict):
            weather_type = int(weather.get("type", -1))
            normalized["weather"] = {
                "type": weather_type if -1 <= weather_type <= 9 else -1,
                "expiresAt": max(0, int(weather.get("expiresAt", 0))),
            }
        events = payload.get("events")
        if isinstance(events, dict):
            normalized["events"] = {
                str(key): {
                    "startedAt": max(0, int(value.get("startedAt", 0))),
                    "expiresAt": max(0, int(value.get("expiresAt", 0))),
                }
                for key, value in events.items()
                if isinstance(value, dict) and str(key)
            }
        self.data = normalized
        self._save_locked()
        return normalized

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}."
            f"{secrets.token_hex(4)}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def rankings(self, rank_type: str) -> tuple[int, ...]:
        with self.lock:
            return tuple(self.data["rankings"].get(str(rank_type), ()))

    def set_rankings(self, rank_type: str, character_ids: list[int]) -> tuple[int, ...]:
        if rank_type not in MANUAL_RANK_TYPES:
            raise ValueError("不支持的人工榜单")
        normalized = list(
            dict.fromkeys(int(value) for value in character_ids if int(value) > 0)
        )[:100]
        with self.lock:
            self.data["rankings"][rank_type] = normalized
            self._save_locked()
        return tuple(normalized)

    def weather_override(self, now: int | None = None) -> int | None:
        current = int(time.time()) if now is None else int(now)
        with self.lock:
            weather = self.data["weather"]
            weather_type = int(weather.get("type", -1))
            expires_at = int(weather.get("expiresAt", 0))
            if weather_type < 0 or (expires_at > 0 and expires_at <= current):
                if weather_type >= 0:
                    self.data["weather"] = {"type": -1, "expiresAt": 0}
                    self._save_locked()
                return None
            return weather_type

    def set_weather(self, weather_type: int | None, duration_seconds: int = 0) -> None:
        normalized = -1 if weather_type is None else int(weather_type)
        if not -1 <= normalized <= 9:
            raise ValueError("天气类型必须在 0-9 之间")
        expires_at = (
            int(time.time()) + max(1, int(duration_seconds))
            if normalized >= 0 and duration_seconds > 0
            else 0
        )
        with self.lock:
            self.data["weather"] = {"type": normalized, "expiresAt": expires_at}
            self._save_locked()

    def start_event(self, event_key: str, duration_seconds: int) -> dict[str, int]:
        key = str(event_key).strip()
        if not key:
            raise ValueError("事件标识不能为空")
        now = int(time.time())
        value = {
            "startedAt": now,
            "expiresAt": now + max(10, int(duration_seconds)),
        }
        with self.lock:
            self.data["events"][key] = value
            self._save_locked()
        return dict(value)

    def stop_event(self, event_key: str) -> None:
        with self.lock:
            self.data["events"].pop(str(event_key), None)
            self._save_locked()

    def active_event(self, event_key: str, now: int | None = None) -> dict[str, int] | None:
        current = int(time.time()) if now is None else int(now)
        key = str(event_key)
        with self.lock:
            value = self.data["events"].get(key)
            if not isinstance(value, dict):
                return None
            if int(value.get("expiresAt", 0)) <= current:
                self.data["events"].pop(key, None)
                self._save_locked()
                return None
            return {
                "startedAt": int(value.get("startedAt", 0)),
                "expiresAt": int(value.get("expiresAt", 0)),
            }

    def snapshot(self) -> dict[str, Any]:
        now = int(time.time())
        with self.lock:
            expired = [
                key
                for key, value in self.data["events"].items()
                if int(value.get("expiresAt", 0)) <= now
            ]
            for key in expired:
                self.data["events"].pop(key, None)
            if expired:
                self._save_locked()
            return json.loads(json.dumps(self.data, ensure_ascii=False))


GM_STATE = GmStateStore()
