"""Memory is a two-method protocol so you can swap in any paper's design via FERN_MEMORY=module:Class."""

import sqlite3
from pathlib import Path
from typing import Protocol


class Memory(Protocol):
    def remember(self, text: str, kind: str = "fact") -> None: ...
    def recall(self, query: str, k: int = 5) -> list[str]: ...


class SQLiteMemory:
    """Baseline: SQLite FTS5 keyword recall. Local file, never leaves the box."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem USING fts5(text, kind, ts UNINDEXED)")

    def remember(self, text: str, kind: str = "fact") -> None:
        with self.db:
            self.db.execute("INSERT INTO mem VALUES (?, ?, datetime('now'))", (text, kind))

    def recall(self, query: str, k: int = 5) -> list[str]:
        terms = " OR ".join(f'"{w}"' for w in query.replace('"', "").split() if len(w) > 2)
        if not terms:
            return []
        rows = self.db.execute("SELECT text FROM mem WHERE mem MATCH ? ORDER BY rank LIMIT ?", (terms, k))
        return [r[0] for r in rows]


def load_memory(spec: str, data_dir: Path) -> Memory:
    import importlib

    mod, _, cls = spec.partition(":")
    return getattr(importlib.import_module(mod), cls)(data_dir / "memory.db")
