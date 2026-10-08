"""Local persistence: one SQLite file for state, one JSONL file for trajectories (eval / fine-tuning data)."""

import json
import os
import sqlite3
from typing import Any

from fern.config import Settings

GLOBAL = "_"  # meta "session" for household-wide keys (alarms, briefing, builds)


class Store:
    def __init__(self, settings: Settings):
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(settings.db, check_same_thread=False)
        self.trajectories_file = settings.data_dir / "trajectories.jsonl"
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(session TEXT, key TEXT, value TEXT, PRIMARY KEY(session, key));
            CREATE TABLE IF NOT EXISTS pending(
                id TEXT PRIMARY KEY, session TEXT, device TEXT, model TEXT, state TEXT, ts REAL
            );
            CREATE TABLE IF NOT EXISTS usage(session TEXT, model TEXT, requests INT, input INT, cached INT, output INT);
            CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY, text TEXT, ts REAL);
            CREATE TABLE IF NOT EXISTS voice(day TEXT, device TEXT, seconds REAL);
        """)

    def get(self, session: str, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE session=? AND key=?", (session, key)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, session: str, key: str, value: Any) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?,?)", (session, key, json.dumps(value)))

    def stats(self, session: str) -> list[dict]:
        query = """SELECT model, SUM(requests), SUM(input), SUM(cached), SUM(output) FROM usage
                   WHERE session=? GROUP BY model"""
        return [
            {
                "model": model,
                "requests": requests,
                "input": input_tokens,
                "cached": cached_tokens,
                "output": output_tokens,
                "cache_hit": round(cached_tokens / input_tokens, 2) if input_tokens else 0,
            }
            for model, requests, input_tokens, cached_tokens, output_tokens in self.db.execute(query, (session,))
        ]

    def log(self, record: dict) -> None:
        file_descriptor = os.open(self.trajectories_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(file_descriptor, "a") as trajectories:
            trajectories.write(json.dumps(record, default=str) + "\n")
