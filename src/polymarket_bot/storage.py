"""JSON/CSV persistence helpers.

Never used to save secrets. Only market data, scores, paper trades, and
portfolio snapshots pass through here.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def save_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    rows = list(rows)
    ensure_dir(path.parent)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_json_list(path: Path, item: Any) -> None:
    data = load_json(path, default=[])
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list at {path}, got {type(data)}")
    data.append(item)
    save_json(path, data)
