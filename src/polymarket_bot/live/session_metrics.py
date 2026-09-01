"""Crash-tolerant per-live-start performance journal.

This deliberately records the same cumulative strategy-P/L metric used by
the session circuit breaker, so a run's delta can be audited after the
process exits.  It adds no API calls: fill and quote-cycle counts come from
the existing local ledgers, while the runner supplies the P/L value it
already computed for its safety checks.
"""

from __future__ import annotations

from pathlib import Path
from collections import defaultdict
from typing import Optional
from uuid import uuid4

from .. import config, storage
from ..models import utcnow_iso
from .fills import get_all_fills
from .ledger import get_all_cycles


SESSIONS_FILE = config.LIVE_TRADES_DIR / "sessions.json"


class SessionMetricsRecorder:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or SESSIONS_FILE
        self.session_id = uuid4().hex
        self._started = False

    def start(self) -> dict:
        now = utcnow_iso()
        fill_count = len(get_all_fills())
        quote_cycle_count = len(get_all_cycles())
        record = {
            "session_id": self.session_id,
            "status": "running",
            "started_at": now,
            "last_updated_at": now,
            "ended_at": None,
            "starting_total_pnl_usd": None,
            "latest_total_pnl_usd": None,
            "session_pnl_usd": None,
            "session_pnl_source": None,
            "account_position_pnl_delta_usd": None,
            "flat_round_trip_fill_pnl_usd": 0.0,
            "starting_fill_count": fill_count,
            "latest_fill_count": fill_count,
            "new_fill_count": 0,
            "starting_quote_cycle_count": quote_cycle_count,
            "latest_quote_cycle_count": quote_cycle_count,
            "new_quote_cycle_count": 0,
        }
        records = _load_records(self.path)
        records.append(record)
        storage.save_json(self.path, records)
        self._started = True
        return record

    def update(self, total_pnl_usd: float) -> dict:
        if not self._started:
            self.start()
        records = _load_records(self.path)
        record = _find_record(records, self.session_id)
        if record.get("starting_total_pnl_usd") is None:
            record["starting_total_pnl_usd"] = round(total_pnl_usd, 4)
        record["latest_total_pnl_usd"] = round(total_pnl_usd, 4)
        account_delta = round(
            record["latest_total_pnl_usd"] - record["starting_total_pnl_usd"], 4,
        )
        all_fills = get_all_fills()
        record["account_position_pnl_delta_usd"] = account_delta
        flat_fill_pnl = flat_round_trip_fill_pnl(
            all_fills[record["starting_fill_count"]:]
        )
        record["flat_round_trip_fill_pnl_usd"] = flat_fill_pnl
        if flat_fill_pnl is not None:
            record["session_pnl_usd"] = flat_fill_pnl
            record["session_pnl_source"] = "flat_round_trip_fills"
        else:
            record["session_pnl_usd"] = account_delta
            record["session_pnl_source"] = "account_position_pnl_delta"
        record["latest_fill_count"] = len(all_fills)
        record["new_fill_count"] = (
            record["latest_fill_count"] - record["starting_fill_count"]
        )
        record["latest_quote_cycle_count"] = len(get_all_cycles())
        record["new_quote_cycle_count"] = (
            record["latest_quote_cycle_count"] - record["starting_quote_cycle_count"]
        )
        record["last_updated_at"] = utcnow_iso()
        storage.save_json(self.path, records)
        return record

    def finish(self, total_pnl_usd: Optional[float] = None) -> dict:
        if total_pnl_usd is not None:
            self.update(total_pnl_usd)
        records = _load_records(self.path)
        record = _find_record(records, self.session_id)
        now = utcnow_iso()
        record["status"] = "stopped"
        record["ended_at"] = now
        record["last_updated_at"] = now
        storage.save_json(self.path, records)
        return record


def mark_stale_running_sessions_crashed(path: Optional[Path] = None) -> list[str]:
    """Called once at process startup, after the instance lock is
    successfully acquired (see live/instance_lock.py) -- which is itself
    proof no other bot process is alive, so ANY session record still
    "running" at this point unambiguously means its process died without
    reaching .finish() (the only path that ever sets "stopped"): a
    confirmed real incident where an abruptly killed process left its
    session stuck "running" forever with no record of when/why it
    stopped. Returns the session_ids marked, for the caller to log."""
    file_path = path or SESSIONS_FILE
    records = _load_records(file_path)
    now = utcnow_iso()
    marked: list[str] = []
    for record in records:
        if record.get("status") == "running":
            record["status"] = "crashed"
            record["ended_at"] = now
            record["last_updated_at"] = now
            marked.append(record.get("session_id"))
    if marked:
        storage.save_json(file_path, records)
    return marked


def _load_records(path: Path) -> list[dict]:
    records = storage.load_json(path, default=[])
    if not isinstance(records, list):
        raise ValueError(f"Expected a JSON list at {path}")
    return records


def _find_record(records: list[dict], session_id: str) -> dict:
    for record in reversed(records):
        if record.get("session_id") == session_id:
            return record
    raise ValueError(f"Session record {session_id} is missing")


def flat_round_trip_fill_pnl(fills: list) -> Optional[float]:
    """Exact signed cashflow when the given fills' deltas return flat.

    Positive commissionNotionalCollected is a fee and is subtracted;
    negative is a rebate. If any market/outcome has a nonzero share delta,
    cashflow includes inventory acquisition/disposal and is not yet P/L, so
    return None and let the caller fall back to its account-position proxy
    instead. Not underscore-prefixed -- ws_runner.py reuses this directly to
    apply the same accuracy correction to the daily circuit breaker's P/L,
    windowed to today's fills instead of this session's (see
    live/RUNBOOK.md's most recent section).
    """
    if not fills:
        return 0.0
    if any(not isinstance(fill, dict) for fill in fills):
        return None
    net_shares = defaultdict(float)
    cashflow = 0.0
    for fill in fills:
        try:
            shares = float(fill.get("shares") or 0.0)
            price = float(fill.get("price") or 0.0)
            commission = float(fill.get("commission_usd") or 0.0)
        except (TypeError, ValueError):
            return None
        side = str(fill.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            return None
        key = (fill.get("market_slug"), fill.get("outcome"))
        if side == "BUY":
            net_shares[key] += shares
            cashflow -= shares * price
        else:
            net_shares[key] -= shares
            cashflow += shares * price
        cashflow -= commission
    if any(abs(shares) > 1e-6 for shares in net_shares.values()):
        return None
    return round(cashflow, 4)
