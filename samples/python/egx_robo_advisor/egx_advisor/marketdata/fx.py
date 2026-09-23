"""USD/EGP, recorded over time.

Why this is a journal and not a number
--------------------------------------
Thndr X prints the EGX reference rate in its top bar (``USD/EGP 51.57``), and
that is the rate this system should key off: it is the exchange's own figure,
not a third party's mid-market quote. But the policy's devaluation switch does
not compare against *a* rate -- it compares today against roughly six months
ago. One number off a screen cannot answer that.

So observations are journalled. Each session records what was seen, and the
trailing depreciation is computed from the series this file has accumulated.
That has an honest consequence worth stating plainly: **on a fresh journal there
is no history, so no devaluation can be detected.** It is not that the switch
reads zero and is therefore safe -- it is that the switch is blind until the
journal spans the lookback window. Seed it from a known history before relying
on the macro state, or accept that the policy sits at baseline until enough
sessions have passed.

Reading FX off a screen is defensible in a way that reading *prices* off a
screen is not. A price multiplies an order size, so an OCR slip on a decimal
point changes what you buy by a factor. This rate feeds one coarse two-state
threshold; being a few piastres out cannot flip a 15% decision. That asymmetry
is the whole argument, and it does not extend to anything else on the screen.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

#: A live observation outside this band is a misread, not a devaluation.
#:
#: The bound that matters is the lower one. The likeliest OCR failure on a rate
#: like 51.57 is a lost decimal point giving 5.157 -- an order of magnitude out,
#: and exactly the kind of number a band anchored near zero waves through. So
#: the floor sits well above any rate this system will legitimately observe
#: live, rather than at a token non-zero value.
#:
#: This applies to `record()` only. `seed()` bypasses it, because historical
#: series legitimately reach back to rates far below today's.
PLAUSIBLE_RANGE = (Decimal("10"), Decimal("300"))

#: A single session move beyond this is treated as suspect and recorded but
#: flagged. Egypt has repriced ~35% in a day before, so this is above that.
IMPLAUSIBLE_DAILY_MOVE = Decimal("0.60")


class FxError(ValueError):
    """The observation is unusable. Never silently clamped."""


@dataclass(frozen=True, slots=True)
class FxObservation:
    day: date
    usd_egp: Decimal
    source: str = "screen"


@dataclass
class FxJournal:
    """Append-only USD/EGP history, persisted as plain CSV.

    CSV because this is a series a human should be able to open, sanity-check
    against the exchange, and correct by hand. It is small -- one row per
    session -- and its correctness matters more than its speed.
    """

    path: Path
    _rows: dict[date, FxObservation] | None = None

    # ------------------------------------------------------------------ loading

    def load(self) -> dict[date, FxObservation]:
        if self._rows is not None:
            return self._rows
        rows: dict[date, FxObservation] = {}
        path = Path(self.path)
        if path.exists():
            with path.open(newline="", encoding="utf-8") as handle:
                for line, record in enumerate(csv.DictReader(handle), start=2):
                    try:
                        day = date.fromisoformat(record["date"].strip())
                        rate = Decimal(record["usd_egp"].strip())
                    except (KeyError, ValueError, InvalidOperation, AttributeError) as exc:
                        raise FxError(f"{path}:{line}: {exc}") from exc
                    rows[day] = FxObservation(
                        day, rate, (record.get("source") or "seed").strip()
                    )
        self._rows = rows
        return rows

    # ------------------------------------------------------------------ writing

    def record(self, day: date, rate: Decimal, *, source: str = "screen") -> FxObservation:
        """Journal one observation, rejecting an implausible one rather than storing it."""
        rate = Decimal(rate)
        low, high = PLAUSIBLE_RANGE
        if not (low <= rate <= high):
            raise FxError(
                f"USD/EGP {rate} is outside the plausible band {low}-{high}; "
                f"this is a misread, not a devaluation"
            )

        rows = self.load()
        previous = self._latest_before(day)
        if previous is not None and previous.usd_egp > 0:
            move = abs(rate - previous.usd_egp) / previous.usd_egp
            if move >= IMPLAUSIBLE_DAILY_MOVE:
                raise FxError(
                    f"USD/EGP {previous.usd_egp} -> {rate} is a {move:.0%} move since "
                    f"{previous.day}; refusing to record without a human look"
                )

        observation = FxObservation(day, rate, source)
        rows[day] = observation
        self._flush(rows)
        return observation

    def seed(self, observations: Iterable[FxObservation]) -> int:
        """Bulk-load history, e.g. from a provider, so the switch is not blind."""
        rows = self.load()
        added = 0
        for observation in observations:
            if observation.day not in rows:
                rows[observation.day] = observation
                added += 1
        if added:
            self._flush(rows)
        return added

    def _flush(self, rows: dict[date, FxObservation]) -> None:
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "usd_egp", "source"])
            for day in sorted(rows):
                row = rows[day]
                writer.writerow([day.isoformat(), row.usd_egp, row.source])
        self._rows = rows

    # ------------------------------------------------------------------ reading

    def latest(self) -> Optional[FxObservation]:
        rows = self.load()
        return rows[max(rows)] if rows else None

    def _latest_before(self, day: date) -> Optional[FxObservation]:
        rows = self.load()
        earlier = [d for d in rows if d < day]
        return rows[max(earlier)] if earlier else None

    def rate_on_or_before(self, day: date) -> Optional[FxObservation]:
        rows = self.load()
        candidates = [d for d in rows if d <= day]
        return rows[max(candidates)] if candidates else None

    def depreciation(self, *, today: date, lookback_days: int) -> Optional[Decimal]:
        """Trailing EGP depreciation, or None when the journal cannot answer.

        None is the important return: it means "no opinion", and the caller must
        treat it as no devaluation signal rather than as zero depreciation. The
        two look identical numerically and are completely different claims.
        """
        current = self.rate_on_or_before(today)
        if current is None:
            return None
        reference = self.rate_on_or_before(today - timedelta(days=lookback_days))
        if reference is None or reference.usd_egp <= 0:
            return None
        if reference.day == current.day:
            return None  # only one observation; nothing to compare against
        return (current.usd_egp - reference.usd_egp) / reference.usd_egp

    def spans(self, *, today: date, lookback_days: int) -> bool:
        """Whether the journal actually covers the window the policy asks about."""
        return self.depreciation(today=today, lookback_days=lookback_days) is not None
