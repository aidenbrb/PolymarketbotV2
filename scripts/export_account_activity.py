"""Export a read-only slice of Polymarket US account activity.

This script uses the same signed retail API client as the live bot, but it
only calls GET /v1/portfolio/activities.  It never places, modifies, or
cancels an order.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot import config
from polymarket_bot.live.credentials import load_api_credentials
from polymarket_bot.live.us_client import LiveUsClient, UsApiError


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _activity_time(activity: dict[str, Any]) -> datetime | None:
    for payload_name in ("trade", "positionResolution", "accountBalanceChange"):
        payload = activity.get(payload_name)
        if not isinstance(payload, dict):
            continue
        for field_name in ("createTime", "updateTime"):
            value = payload.get(field_name)
            if isinstance(value, str) and value:
                return _parse_timestamp(value)
    return None


def fetch_activities(
    client: LiveUsClient,
    start: datetime,
    end: datetime,
    activity_types: list[str],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    for _page in range(10_000):
        params: dict[str, Any] = {
            "limit": 100,
            "sortOrder": "SORT_ORDER_ASCENDING",
            "types": activity_types,
        }
        if cursor:
            params["cursor"] = cursor

        data = client._request(  # Read-only endpoint; no public wrapper exists yet.
            "GET",
            "/v1/portfolio/activities",
            params=params,
            retryable=True,
        )
        if not isinstance(data, dict):
            raise UsApiError("Activities endpoint returned a non-object response")
        activities = data.get("activities")
        if not isinstance(activities, list):
            raise UsApiError("Activities endpoint omitted its activities list")

        latest_time: datetime | None = None
        for activity in activities:
            if not isinstance(activity, dict):
                continue
            timestamp = _activity_time(activity)
            if timestamp is None:
                continue
            latest_time = max(latest_time, timestamp) if latest_time else timestamp
            if start <= timestamp < end:
                selected.append(activity)

        if latest_time is not None and latest_time >= end:
            return selected
        if data.get("eof") is not False:
            return selected

        next_cursor = data.get("nextCursor") or data.get("next_cursor")
        if not next_cursor or str(next_cursor) in seen_cursors:
            raise UsApiError(
                "Activities pagination did not provide a new cursor; refusing a partial export"
            )
        cursor = str(next_cursor)
        seen_cursors.add(cursor)
        time.sleep(0.12)

    raise UsApiError("Activities pagination exceeded 10,000 pages")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only export of Polymarket US account activity."
    )
    parser.add_argument("--start", required=True, type=_parse_timestamp)
    parser.add_argument("--end", required=True, type=_parse_timestamp)
    parser.add_argument(
        "--types",
        nargs="+",
        default=[
            "ACTIVITY_TYPE_TRADE",
            "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "ACTIVITY_TYPE_ACCOUNT_DEPOSIT",
            "ACTIVITY_TYPE_ACCOUNT_ADVANCED_DEPOSIT",
            "ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL",
            "ACTIVITY_TYPE_TRANSFER",
            "ACTIVITY_TYPE_TAKER_FEE_REBATE",
            "ACTIVITY_TYPE_LIQUIDITY_PROGRAM",
        ],
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.end <= args.start:
        parser.error("--end must be later than --start")

    settings = config.load_settings().live
    client = LiveUsClient(load_api_credentials(), settings)
    activities = fetch_activities(client, args.start, args.end, args.types)

    payload = {
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "activityTypes": args.types,
        "activities": activities,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    timestamps = [
        timestamp
        for activity in activities
        if (timestamp := _activity_time(activity)) is not None
    ]
    print(f"Exported {len(activities)} activities to {args.output}")
    if timestamps:
        print(f"Activity range: {min(timestamps).isoformat()} to {max(timestamps).isoformat()}")


if __name__ == "__main__":
    main()
