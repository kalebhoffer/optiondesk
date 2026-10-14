"""An append-only SQLite audit trail of everything the engine does."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .bus import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    time    TEXT NOT NULL,
    topic   TEXT NOT NULL,
    data    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_topic_time ON events (topic, time);
"""


class Journal:
    """Subscribe it to the bus with ``bus.subscribe("*", journal.record)``.

    Every event is stored as one row with its data as JSON, which keeps the
    schema stable while event contents evolve. Query it with
    :meth:`recent`, or open the file in any SQLite tool.
    """

    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(_SCHEMA)

    def record(self, event: Event) -> None:
        self.db.execute(
            "INSERT INTO events (time, topic, data) VALUES (?, ?, ?)",
            (event.time.isoformat(), event.topic, json.dumps(event.data, default=str)),
        )
        self.db.commit()

    def recent(self, limit: int = 100, topic: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT id, time, topic, data FROM events"
        params: tuple[Any, ...] = ()
        if topic:
            query += " WHERE topic = ?"
            params = (topic,)
        query += " ORDER BY id DESC LIMIT ?"
        rows = self.db.execute(query, (*params, limit)).fetchall()
        return [{"id": r[0], "time": r[1], "topic": r[2], "data": json.loads(r[3])} for r in rows]

    def close(self) -> None:
        self.db.close()
