"""Lightweight SQLite memory plus human-readable explicit remembered-events log."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(slots=True)
class MemoryItem:
    id: int
    kind: str
    text: str
    created_at: str
    tags: list[str]


class MemoryStore:
    """Persist useful text/structured context; never stores raw audio or camera frames."""

    def __init__(
        self,
        db_path: Path,
        remembered_events_path: Path,
        recent_turn_limit: int = 20,
        *,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.db_path = db_path
        self.remembered_events_path = remembered_events_path
        self.recent_turn_limit = recent_turn_limit
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if not self.enabled:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.remembered_events_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS visual_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS device_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.commit()

    async def remember(self, text: str, *, kind: str = "explicit", tags: list[str] | None = None) -> MemoryItem:
        if not self.enabled:
            raise RuntimeError("local memory is disabled")
        clean = " ".join(text.split()).strip()
        if not clean:
            raise ValueError("memory text cannot be empty")
        if len(clean) > 2000:
            raise ValueError("memory text is too long")
        tags = [tag.strip().lower() for tag in (tags or []) if tag.strip()][:12]
        async with self._lock:
            item = await asyncio.to_thread(self._remember_sync, clean, kind, tags)
        if kind == "explicit":
            payload = json.dumps(asdict(item), ensure_ascii=False)
            await asyncio.to_thread(self._append_line, self.remembered_events_path, payload)
        return item

    def _remember_sync(self, text: str, kind: str, tags: list[str]) -> MemoryItem:
        created = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as db:
            cursor = db.execute(
                "INSERT INTO memories(kind, text, tags, created_at) VALUES (?, ?, ?, ?)",
                (kind, text, json.dumps(tags), created),
            )
            db.commit()
            memory_id = int(cursor.lastrowid)
        return MemoryItem(memory_id, kind, text, created, tags)

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    async def search(self, query: str, limit: int = 6) -> list[MemoryItem]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._search_sync, query, limit)

    def _search_sync(self, query: str, limit: int) -> list[MemoryItem]:
        terms = {word for word in re.findall(r"[a-z0-9']+", query.lower()) if len(word) > 2}
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                "SELECT id, kind, text, created_at, tags FROM memories ORDER BY id DESC LIMIT 200"
            ).fetchall()
        scored: list[tuple[int, int, MemoryItem]] = []
        for row in rows:
            text_terms = set(re.findall(r"[a-z0-9']+", str(row[2]).lower()))
            score = len(terms & text_terms)
            item = MemoryItem(int(row[0]), str(row[1]), str(row[2]), str(row[3]), json.loads(row[4]))
            if not terms or score:
                scored.append((score, item.id, item))
        scored.sort(key=lambda value: (value[0], value[1]), reverse=True)
        return [item for _, _, item in scored[: max(1, limit)]]

    async def add_turn(self, role: str, text: str) -> None:
        if not self.enabled:
            return
        clean = " ".join(text.split()).strip()
        if not clean:
            return
        await asyncio.to_thread(self._add_turn_sync, role, clean[:4000])

    def _add_turn_sync(self, role: str, text: str) -> None:
        created = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO conversation_turns(role, text, created_at) VALUES (?, ?, ?)",
                (role, text, created),
            )
            # Bound local conversational storage independently from ADK's in-memory session.
            db.execute(
                "DELETE FROM conversation_turns WHERE id NOT IN (SELECT id FROM conversation_turns ORDER BY id DESC LIMIT ?)",
                (self.recent_turn_limit,),
            )
            db.commit()

    async def add_visual_event(self, event_type: str, confidence: float, summary: str) -> None:
        if not self.enabled:
            return
        await asyncio.to_thread(self._add_visual_event_sync, event_type, confidence, summary)

    def _add_visual_event_sync(self, event_type: str, confidence: float, summary: str) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO visual_events(event_type, confidence, summary, created_at) VALUES (?, ?, ?, ?)",
                (event_type, float(confidence), summary[:1000], datetime.now(UTC).isoformat()),
            )
            db.execute("DELETE FROM visual_events WHERE id NOT IN (SELECT id FROM visual_events ORDER BY id DESC LIMIT 100)")
            db.commit()

    async def set_device_state(self, key: str, value: str) -> None:
        if not self.enabled:
            return
        await asyncio.to_thread(self._set_device_state_sync, key, value)

    def _set_device_state_sync(self, key: str, value: str) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO device_state(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, datetime.now(UTC).isoformat()),
            )
            db.commit()

    @staticmethod
    def extract_explicit_memory(transcript: str) -> str | None:
        match = re.search(r"\bremember(?:\s+that|\s+this)?\s*[:,]?\s+(.+)", transcript, flags=re.IGNORECASE)
        if not match:
            return None
        fact = match.group(1).strip(" .")
        return fact if len(fact) >= 2 else None
