"""Local persistence: one SQLite file for state, one JSONL file for trajectories (eval / fine-tuning data)."""

import json
import os
import sqlite3
import time
from typing import Any

from fern.config import Settings

GLOBAL = "_"  # meta "session" for household-wide keys (builds, notices seen)
USAGE_TOTALS = "SUM(requests), SUM(input), SUM(cached), SUM(output)"


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
        usage_columns = {row[1] for row in self.db.execute("PRAGMA table_info(usage)")}
        if "ts" not in usage_columns:  # databases created before usage had timestamps
            self.db.execute("ALTER TABLE usage ADD COLUMN ts REAL")

    def get(self, session: str, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE session=? AND key=?", (session, key)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, session: str, key: str, value: Any) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?,?)", (session, key, json.dumps(value)))

    def record_usage(
        self, session: str, model: str, requests: int, input_tokens: int, cached_tokens: int, output_tokens: int
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO usage(session, model, requests, input, cached, output, ts) VALUES (?,?,?,?,?,?,?)",
                (session, model, requests, input_tokens, cached_tokens, output_tokens, time.time()),
            )

    def stats(self, session: str) -> list[dict]:
        """Token totals per model for one session."""
        query = f"SELECT model, {USAGE_TOTALS} FROM usage WHERE session=? GROUP BY model"
        return self._usage_rows(query, (session,))

    def usage_since(self, since_epoch: float) -> list[dict]:
        """Token totals per model across all sessions (brain and voice) since a moment in time."""
        query = f"SELECT model, {USAGE_TOTALS} FROM usage WHERE ts >= ? GROUP BY model ORDER BY SUM(output) DESC"
        return self._usage_rows(query, (since_epoch,))

    def _usage_rows(self, query: str, parameters: tuple) -> list[dict]:
        return [
            {
                "model": model,
                "requests": requests,
                "input": input_tokens,
                "cached": cached_tokens,
                "output": output_tokens,
                "cache_hit": round(cached_tokens / input_tokens, 2) if input_tokens else 0,
            }
            for model, requests, input_tokens, cached_tokens, output_tokens in self.db.execute(query, parameters)
        ]

    def log(self, record: dict) -> None:
        file_descriptor = os.open(self.trajectories_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(file_descriptor, "a") as trajectories:
            trajectories.write(json.dumps(record, default=str) + "\n")
