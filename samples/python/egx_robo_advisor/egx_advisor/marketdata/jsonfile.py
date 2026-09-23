"""JSON-file market data, for development, backtests and the smoke run.

Reads a file, which makes the whole pipeline reproducible without a
subscription. Point production at a real feed and keep the staleness checks:
a provider returning yesterday's close during a devaluation is worse than no
provider at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

from ..types import Instrument, MarketSnapshot, Quote, Tradability
from .base import MarketDataError


@dataclass
class JsonFileMarketData:
    """Reads a snapshot from a JSON file.

    Expected shape::

        {
          "as_of": "2026-09-22T11:00:00+00:00",
          "usd_egp": "62.0",
          "usd_egp_lookback": "48.0",
          "quotes": {
            "COMI.CA": {"last": "85.0", "prev_close": "84.0", "tradability": "open"}
          }
        }

    Anything older than `max_age` is rejected rather than used, because a
    rebalancer sizing orders against a stale tape is the failure mode this seam
    exists to prevent.
    """

    path: Path
    max_age: timedelta = timedelta(minutes=30)

    async def snapshot(self, universe: Sequence[Instrument]) -> MarketSnapshot:
        try:
            payload = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise MarketDataError(f"could not read {self.path}: {exc}") from exc

        as_of = _parse_dt(payload.get("as_of"))
        age = datetime.now(timezone.utc) - as_of
        if age > self.max_age:
            raise MarketDataError(
                f"market snapshot is {age.total_seconds() / 60:.0f} min old, "
                f"limit is {self.max_age.total_seconds() / 60:.0f} min"
            )

        raw_quotes: Mapping[str, Mapping[str, str]] = payload.get("quotes", {})
        quotes: dict[str, Quote] = {}
        for instrument in universe:
            entry = raw_quotes.get(instrument.symbol)
            if not entry:
                continue
            quotes[instrument.symbol] = Quote(
                symbol=instrument.symbol,
                last=Decimal(str(entry["last"])),
                prev_close=Decimal(str(entry.get("prev_close", entry["last"]))),
                as_of=as_of,
                tradability=Tradability(entry.get("tradability", "open")),
            )

        if not quotes:
            raise MarketDataError("snapshot contained no quotes for the universe")

        return MarketSnapshot(
            as_of=as_of,
            quotes=quotes,
            usd_egp=Decimal(str(payload.get("usd_egp", "0"))),
            usd_egp_lookback=Decimal(str(payload.get("usd_egp_lookback", "0"))),
        )


def _parse_dt(raw: object) -> datetime:
    if not isinstance(raw, str):
        raise MarketDataError("snapshot is missing 'as_of'")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
