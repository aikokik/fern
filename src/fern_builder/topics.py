"""Topics: one resumable Claude conversation + working tree + branch each. Stored as one small JSON file."""

import json
import time
import uuid
from pathlib import Path


class TopicStore:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict[str, dict]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, slug: str) -> dict | None:
        return self._load().get(slug)

    def create(self, slug: str) -> dict:
        topic = {
            "slug": slug,
            "branch": f"claude/{slug}",
            "session_id": str(uuid.uuid4()),  # fixed up front so the first turn and every resume use the same id
            "started": False,
            "status": "planning",
            "cost_usd_total": 0.0,
            "pr_url": None,
            "created": time.time(),
            "updated": time.time(),
        }
        self.save(topic)
        return topic

    def save(self, topic: dict) -> None:
        topics = self._load()
        topics[topic["slug"]] = topic | {"updated": time.time()}
        partial = self.path.with_suffix(".tmp")
        partial.write_text(json.dumps(topics, indent=1))
        partial.replace(self.path)

    def all(self) -> list[dict]:
        return sorted(self._load().values(), key=lambda topic: topic["updated"], reverse=True)
