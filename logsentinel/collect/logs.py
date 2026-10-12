"""Slice structured service logs (JSON lines, incl. rotated files) to a time window."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds")


def slice_logs(live_dir: Path, out_dir: Path, start: float, end: float) -> dict[str, int]:
    """Copy log lines with start <= ts < end into out_dir/<service>.jsonl. Returns line counts."""
    lo, hi = _iso(start), _iso(end)  # ISO-8601 UTC strings of equal format sort chronologically
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for current in sorted(live_dir.glob("*.jsonl")):
        service = current.stem
        files = sorted(live_dir.glob(f"{service}.jsonl.*"), reverse=True) + [current]
        lines = []
        for path in files:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        ts = json.loads(line)["ts"]
                    except (ValueError, KeyError):
                        continue
                    if lo <= ts < hi:
                        lines.append(line)
        (out_dir / f"{service}.jsonl").write_text("".join(lines), encoding="utf-8")
        counts[service] = len(lines)
    return counts
