"""Fetches and normalizes public market data. Makes no trading decisions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from . import config, storage
from .logger import get_logger
from .models import Market, utcnow_iso
from .polymarket_client import PolymarketClient

logger = get_logger("market_scanner")


def _parse_json_field(value: Any) -> Any:
    """Polymarket US encodes some list fields (outcomes, prices) as JSON strings."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
    return value or []


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _quote_value(quote: Any) -> Optional[float]:
    """Polymarket US represents prices as {"value": "123.45", "currency": ...}."""
    if not isinstance(quote, dict):
        return None
    try:
        return float(quote.get("value"))
    except (TypeError, ValueError):
        return None


class MarketScanner:
    def __init__(self, client: Optional[PolymarketClient] = None):
        self.client = client or PolymarketClient()
        self.settings = config.load_settings()

    def scan(self, enrich_order_book: bool = True, max_pages: int = 10) -> list[Market]:
        config.ensure_data_dirs()

        raw_markets = self._fetch_raw_markets(max_pages=max_pages)
        self._save_raw(raw_markets)

        markets: list[Market] = []
        skipped = 0
        failed = 0
        for raw in raw_markets:
            try:
                market = self._normalize(raw)
            except Exception as exc:  # defensive: never let one bad record kill a scan
                failed += 1
                logger.error("Failed to normalize market record: %s", exc)
                continue

            if market is None:
                skipped += 1
                continue

            if enrich_order_book:
                self._enrich_with_order_book(market)

            markets.append(market)

        logger.info(
            "Scan complete: fetched=%d normalized=%d skipped=%d failed=%d",
            len(raw_markets), len(markets), skipped, failed,
        )

        self._save_processed(markets)
        return markets

    # ------------------------------------------------------------------
    def _fetch_raw_markets(self, max_pages: int) -> list[dict[str, Any]]:
        try:
            return self.client.get_markets(active=True, closed=False, max_pages=max_pages)
        except Exception as exc:
            logger.error("Failed to fetch markets: %s", exc)
            return []

    def _normalize(self, raw: dict[str, Any]) -> Optional[Market]:
        # Trading (order creation/cancellation) on Polymarket US uses the
        # market SLUG, not the numeric id -- market_id holds the slug.
        market_id = str(raw.get("slug") or raw.get("id") or "").strip()
        question = (raw.get("question") or raw.get("title") or "").strip()
        if not market_id or not question:
            return None

        outcomes = _parse_json_field(raw.get("outcomes"))
        outcome_prices_raw = _parse_json_field(raw.get("outcomePrices"))
        outcome_prices = [_to_float(p) for p in outcome_prices_raw]

        # marketSides[] replaces .com's clobTokenIds -- each side is a
        # tradable instrument (e.g. one team, or "Yes"/"No").
        market_sides = raw.get("marketSides") or []
        token_ids = [str(s["id"]) for s in market_sides if isinstance(s, dict) and s.get("id")]
        any_tradable = any(
            isinstance(s, dict) and s.get("tradable") for s in market_sides
        )

        tags_raw = raw.get("tags") or []
        tags = [t.get("label") if isinstance(t, dict) else str(t) for t in tags_raw]

        category = raw.get("category")

        best_bid = _quote_value(raw.get("bestBidQuote"))
        best_ask = _quote_value(raw.get("bestAskQuote"))
        midpoint = (
            round((best_bid + best_ask) / 2, 6)
            if best_bid is not None and best_ask is not None
            else None
        )
        spread = round(best_ask - best_bid, 6) if midpoint is not None else None

        return Market(
            market_id=market_id,
            event_id=str(raw.get("eventId") or "") or None,
            question=question,
            category=category,
            tags=[t for t in tags if t],
            outcomes=outcomes,
            outcome_prices=outcome_prices,
            token_ids=token_ids,
            volume_24h=_to_float(raw.get("volume24hr")),
            # No direct "liquidity" field is exposed by Polymarket US's
            # markets endpoint -- total volume is used as an approximation
            # pending confirmation of a better depth/liquidity signal (see
            # live/RUNBOOK.md).
            liquidity=_to_float(raw.get("volume")),
            end_date=raw.get("endDate"),
            closed=bool(raw.get("closed", False)),
            active=bool(raw.get("active", True)),
            has_order_book=any_tradable,
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint=midpoint,
            spread=spread,
            raw=raw,
        )

    def _enrich_with_order_book(self, market: Market) -> None:
        """Fall back to a live BBO lookup only when the markets list didn't
        already provide bid/ask/spread data for this market."""
        if market.spread is not None and market.midpoint is not None:
            return
        bbo = self.client.get_market_bbo(market.market_id)
        if bbo is None:
            return
        market.best_bid = bbo.get("best_bid")
        market.best_ask = bbo.get("best_ask")
        if market.best_bid is not None and market.best_ask is not None:
            market.midpoint = round((market.best_bid + market.best_ask) / 2, 6)
            market.spread = round(market.best_ask - market.best_bid, 6)

    # ------------------------------------------------------------------
    def _save_raw(self, raw_markets: list[dict[str, Any]]) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = config.RAW_DIR / f"markets_{timestamp}.json"
        storage.save_json(path, raw_markets)
        logger.info("Saved %d raw market records to %s", len(raw_markets), path)

    def _save_processed(self, markets: list[Market]) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = config.PROCESSED_DIR / f"markets_{timestamp}.json"
        storage.save_json(path, [m.to_dict() for m in markets])
        latest_path = config.PROCESSED_DIR / "latest.json"
        storage.save_json(latest_path, {
            "scanned_at": utcnow_iso(),
            "markets": [m.to_dict() for m in markets],
        })
        logger.info("Saved %d processed markets to %s", len(markets), path)
