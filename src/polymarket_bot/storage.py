"""JSON/CSV persistence helpers.

Never used to save secrets. Only market data, scores, paper trades, and
portfolio snapshots pass through here.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping


_JSON_WRITE_LOCK = threading.RLock()
_WINDOWS_REPLACE_RETRY_DELAYS = (0.05, 0.10, 0.20)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def save_json(path: Path, data: Any) -> None:
    """Atomically replace a JSON file after flushing it to disk.

    Live ledgers are safety state: a process crash during a direct truncate-
    and-write must not leave an empty or half-written file. The temporary
    file lives beside the destination so ``os.replace`` stays atomic.
    """
    ensure_dir(path.parent)
    with _JSON_WRITE_LOCK:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(data, handle, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_transient_retry(temp_path, path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()


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


def _replace_with_transient_retry(source: Path, destination: Path) -> None:
    """Retry the narrow Windows sharing-denial race around ``os.replace``.

    Antivirus/indexing or another reader can briefly hold the destination
    without sharing delete access, producing WinError 5 even though our own
    JSON writers are serialized.  Retry only PermissionError/access-denied;
    unrelated disk errors still fail immediately and preserve the prior
    destination file.
    """
    for delay in (*_WINDOWS_REPLACE_RETRY_DELAYS, None):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            transient = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5
            if not transient or delay is None:
                raise
            time.sleep(delay)


def append_json_list(path: Path, item: Any) -> None:
    # Keep the read-modify-write sequence under the same re-entrant lock so
    # concurrent fill/ledger writers cannot silently lose one another's row.
    with _JSON_WRITE_LOCK:
        data = load_json(path, default=[])
        if not isinstance(data, list):
            raise ValueError(f"Expected a JSON list at {path}, got {type(data)}")
        data.append(item)
        save_json(path, data)
