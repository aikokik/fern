"""Local persistence: one SQLite file for state, one JSONL file for trajectories (eval / fine-tuning data)."""

import json
import os
import sqlite3
import time
from typing import Any

from fern.config import Settings

GLOBAL = "_"  # meta "session" for household-wide keys (builds, notices seen)
USAGE_TOTALS = "SUM(requests), SUM(input), SUM(cached), SUM(output)"
# Token counters per usage row. input/cached/output are totals; audio_* are the audio share (priced ~10x text).
USAGE_COUNTERS = ("requests", "input", "cached", "output", "audio_input", "audio_cached", "audio_output")
NEWER_USAGE_COLUMNS = {
    "ts": "REAL",
    "kind": "TEXT",
    "audio_input": "INT DEFAULT 0",
    "audio_cached": "INT DEFAULT 0",
    "audio_output": "INT DEFAULT 0",
    "max_input": "INT DEFAULT 0",  # largest single-call input: how big the context window got
}


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
        existing_columns = {row[1] for row in self.db.execute("PRAGMA table_info(usage)")}
        with self.db:  # upgrade databases created by older versions
            for column, column_type in NEWER_USAGE_COLUMNS.items():
                if column not in existing_columns:
                    self.db.execute(f"ALTER TABLE usage ADD COLUMN {column} {column_type}")
            self.db.execute(
                "UPDATE usage SET kind = CASE WHEN model LIKE '%realtime%' THEN 'voice' ELSE 'brain' END "
                "WHERE kind IS NULL"
            )

    def get(self, session: str, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE session=? AND key=?", (session, key)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, session: str, key: str, value: Any) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?,?)", (session, key, json.dumps(value)))

    def record_usage(self, session: str, kind: str, model: str, counts: dict) -> None:
        """One usage row. `counts` may hold any of USAGE_COUNTERS plus max_input; missing ones count as 0."""
        values = [int(counts.get(name) or 0) for name in USAGE_COUNTERS]
        if not any(values):
            return
        columns = ", ".join(USAGE_COUNTERS)
        with self.db:
            self.db.execute(
                f"INSERT INTO usage(session, kind, model, {columns}, max_input, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (session, kind, model, *values, int(counts.get("max_input") or 0), time.time()),
            )

    def usage_by_session(self, since_epoch: float = 0.0) -> list[dict]:
        """Summed usage per session, kind and model (input to fern.costs.build_report)."""
        sums = ", ".join(f"SUM({name})" for name in USAGE_COUNTERS)
        query = f"""SELECT session, kind, model, {sums}, MAX(max_input), MAX(ts) FROM usage
                    WHERE COALESCE(ts, 0) >= ? GROUP BY session, kind, model"""
        rows = []
        for session, kind, model, *values, max_input, last_used in self.db.execute(query, (since_epoch,)):
            row = dict(zip(USAGE_COUNTERS, values, strict=True))
            rows.append(
                row | {"session": session, "kind": kind, "model": model, "max_input": max_input, "last_used": last_used}
            )
        return rows

    def stats(self, session: str) -> list[dict]:
        """Token totals per model for one session."""
        query = f"SELECT model, {USAGE_TOTALS} FROM usage WHERE session=? GROUP BY model"
        return self._usage_rows(query, (session,))

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
