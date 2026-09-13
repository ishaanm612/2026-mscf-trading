"""Append-only structured logs for strategy replay and calibration."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping


class StrategyLogger:
    """Write one durable JSON object per snapshot or decision.

    :param path: JSONL destination. The caller normally chooses ``data/``.
    """

    def __init__(self, path: str | Path) -> None:
        """Create a logger without truncating previous heat records.

        :param path: JSONL destination.
        """

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, fields: Mapping[str, Any]) -> None:
        """Append and flush one timestamped structured event.

        :param event: Stable event name used by replay analysis.
        :param fields: JSON-compatible event contents.
        """

        record = {"timestamp": time.time(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, allow_nan=False, sort_keys=True) + "\n")
            output.flush()
