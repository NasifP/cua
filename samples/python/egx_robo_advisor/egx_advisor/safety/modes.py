"""What the bot is allowed to do, and on whose account.

The rest of this package assumes the bot only ever touches a simulator. That
assumption is what lets the demo guard be a hard stop rather than a warning.
Observing a real account means relaxing it deliberately and saying exactly what
replaces it, which is what this module is for.

Two modes, both fully enforced
------------------------------
``SIMULATOR_ONLY``
    A real-money screen halts the bot and latches the kill switch. The account
    assertion is a hard stop.

``LIVE_READ_ONLY``
    A real account may be *observed*. The bot may look and navigate, so it can
    read genuine holdings, price them, and publish a genuine plan to the
    dashboard. It may not open an order ticket, and every order-critical
    primitive is refused at the proxy. Nothing on this rung can place an order,
    which is the point: it answers "is the bot reasoning correctly about my real
    portfolio?" without putting anything at stake.

Why there is no third rung yet
------------------------------
A "fill the ticket but do not submit it" mode is a reasonable thing to want, and
it is deliberately absent rather than half-built. Shipping an enum member that
nothing enforces is worse than not shipping it: someone selects it, reads its
name, and believes a protection exists that does not. If that rung is ever
added, it needs its own enforcement -- blocking Enter inside order blocks, since
the bot types into quantity fields and Enter submits a form in most web UIs --
and its own decision, taken with eyes open.

So the invariant this module upholds is simple and checkable: **no mode defined
here permits an order to reach an exchange on a real account.**
"""

from __future__ import annotations

import enum


class ExecutionMode(enum.Enum):
    SIMULATOR_ONLY = "simulator_only"
    LIVE_READ_ONLY = "live_read_only"

    @property
    def permits_live_account(self) -> bool:
        """Whether a real-money screen is observed rather than halted on."""
        return self is ExecutionMode.LIVE_READ_ONLY

    @property
    def permits_order_tickets(self) -> bool:
        """Whether an order ticket may be opened at all.

        False on the live rung. This is the property that makes the mode a
        restriction rather than a permission.
        """
        return self is ExecutionMode.SIMULATOR_ONLY

    @property
    def permits_orders(self) -> bool:
        """Whether this mode can ever submit an order. Never true on a live account."""
        return self is ExecutionMode.SIMULATOR_ONLY

    @property
    def banner(self) -> str:
        return {
            ExecutionMode.SIMULATOR_ONLY: "SIMULATOR ONLY",
            ExecutionMode.LIVE_READ_ONLY: "REAL ACCOUNT - READ ONLY, NO ORDERS",
        }[self]

    @classmethod
    def parse(cls, value: str) -> "ExecutionMode":
        try:
            return cls(value.strip().lower())
        except ValueError:
            raise ValueError(
                f"unknown execution mode {value!r}; expected one of "
                f"{[m.value for m in cls]}"
            ) from None


class OrderTicketsForbidden(RuntimeError):
    """An order ticket was opened in a mode that observes but never trades.

    Raised at the proxy, not checked by the caller, so no instruction to a model
    and no future execution flow can talk its way past it.
    """
