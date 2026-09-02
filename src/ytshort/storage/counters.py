"""Date-keyed counter backing the PRD's "max 10 emails per day" guardrail.

Kept as a tiny JSON file rather than derived from job records on the fly, so the
cap still holds when old jobs are archived or pruned.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path


class DailyCounter:
    def __init__(self, counters_dir: Path, name: str = "emails") -> None:
        self.path = counters_dir / f"{name}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return {k: int(v) for k, v in data.items() if isinstance(v, int | float | str)}

    def _write(self, data: dict[str, int]) -> None:
        # Keep only the last 30 days so the file cannot grow without bound.
        trimmed = dict(sorted(data.items())[-30:])
        self.path.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    def count(self, on: date | None = None) -> int:
        key = on.isoformat() if on else self._today()
        return self._read().get(key, 0)

    def remaining(self, limit: int) -> int:
        return max(0, limit - self.count())

    def increment(self, amount: int = 1) -> int:
        data = self._read()
        key = self._today()
        data[key] = data.get(key, 0) + amount
        self._write(data)
        return data[key]
