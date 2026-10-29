from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")
SCHEMA_VERSION = 1


class JsonStore:
    """Small atomic JSON store for plugin-owned campaign/player state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = self._load()

    @staticmethod
    def _empty() -> dict:
        return {"schema_version": SCHEMA_VERSION, "groups": {}, "shared_sessions": {}, "players": {}, "daily": {}, "reply_usage": {}, "notices": {}, "censor": {}, "help_docs": {}, "system": {"enabled": True, "clocks": [], "last_clock_minute": ""}}

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            raw = self.path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("root must be an object")
            version = parsed.get("schema_version", 1)
            if version != SCHEMA_VERSION:
                raise ValueError(f"unsupported schema version: {version}")
            state = self._empty()
            for key in ("groups", "shared_sessions", "players", "daily", "reply_usage", "notices", "censor", "help_docs", "system"):
                value = parsed.get(key, state[key])
                if not isinstance(value, dict):
                    raise ValueError(f"{key} must be an object")
                state[key] = value
            return state
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Dice state file is invalid ({self.path}): {exc}") from exc

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def transact(self, mutation: Callable[[dict], T]) -> T:
        with self._lock:
            working = copy.deepcopy(self._state)
            result = mutation(working)
            self._commit(working)
            self._state = working
            return copy.deepcopy(result)

    def _commit(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def group(self, group_id: str) -> dict:
        return self.snapshot()["groups"].get(str(group_id), {})

    def player(self, user_id: str) -> dict:
        return self.snapshot()["players"].get(str(user_id), {})
