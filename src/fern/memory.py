"""Memory is a two-method protocol so you can swap in any paper's design via FERN_MEMORY=module:Class."""

import importlib
import sqlite3
from pathlib import Path
from typing import Protocol


class Memory(Protocol):
    def remember(self, text: str, kind: str = "fact") -> None: ...
    def recall(self, query: str, limit: int = 5) -> list[str]: ...


class SQLiteMemory:
    """Baseline: SQLite FTS5 keyword recall. Local file, never leaves the box."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem USING fts5(text, kind, ts UNINDEXED)")

    def remember(self, text: str, kind: str = "fact") -> None:
        with self.db:
            self.db.execute("INSERT INTO mem VALUES (?, ?, datetime('now'))", (text, kind))

    def recall(self, query: str, limit: int = 5) -> list[str]:
        words = [word for word in query.replace('"', "").split() if len(word) > 2]
        if not words:
            return []
        match_any_word = " OR ".join(f'"{word}"' for word in words)
        rows = self.db.execute("SELECT text FROM mem WHERE mem MATCH ? ORDER BY rank LIMIT ?", (match_any_word, limit))
        return [text for (text,) in rows]

    def recent(self, limit: int = 10) -> list[str]:
        """Optional extra (not in the protocol): newest facts, used for the voice profile."""
        rows = self.db.execute("SELECT text FROM mem ORDER BY rowid DESC LIMIT ?", (limit,))
        return [text for (text,) in rows]


def load_memory(spec: str, data_dir: Path) -> Memory:
    module_name, _, class_name = spec.partition(":")
    memory_class = getattr(importlib.import_module(module_name), class_name)
    return memory_class(data_dir / "memory.db")
