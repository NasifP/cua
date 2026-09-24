"""What the bot is allowed to do, and on whose account.

The rest of this package assumes the bot only ever touches a simulator. That
assumption is what lets the demo guard be a hard stop rather than a warning.
Observing a real account means relaxing it deliberately and saying exactly what
replaces it, which is what this module is for.

Three modes, all fully enforced
-------------------------------
``SIMULATOR_ONLY``
    A real-money screen halts the bot and latches the kill switch. The account
    assertion is a hard stop.

``LIVE_READ_ONLY``
    A real account may be *observed*, and only observed. The bot reads the
    screen the operator left in front, prices the holdings, and publishes a
    genuine plan to the dashboard. Every input -- click, key, hotkey, drag,
    scroll, typed text -- is refused at the proxy, because a click can open a
    ticket and keys can fill and submit one. Nothing on this rung can place an
    order, which is the point: it answers "is the bot reasoning correctly about
    my real portfolio?" without putting anything at stake.

``LIVE_PREPARE_ONLY``
    A real account, and the bot may fill an order ticket -- symbol, side,
    quantity, limit -- and then stop. It never submits. The operator looks at
    the filled ticket on their own screen and presses the broker's own submit
    button themselves, or discards it.

What replaces the demo assertion on the prepare rung
----------------------------------------------------
On ``SIMULATOR_ONLY`` the gate is "this screen is provably a simulator". That
gate cannot exist for an operator who has no simulator account, so on
``LIVE_PREPARE_ONLY`` the verdict reports which account is on screen rather than
authorising the click. Three things carry the weight instead, and all three are
enforced at the proxy rather than asked of the caller:

1. **No submit key.** Enter, Return and keypad Enter are refused inside an order
    block, as is any typed text containing a newline. The bot types into
    quantity fields and Enter submits a form in most web UIs, so this is the
    route that would otherwise submit an order without anything resembling a
    click on "Buy".
2. **No click inside the submit fence.** The calibrated rectangle around the
    broker's submit button is refused as a click target. This turns "the bot
    does not press Buy" from a promise about the execution layer's code into a
    geometric invariant the proxy checks on every pointer call.
3. **The operator's own press.** The final action is a human one, on the
    broker's own screen, which is the only authorisation this rung recognises.

The fence is required, not optional: a ``LIVE_PREPARE_ONLY`` guard built
without one refuses to start. An uncalibrated fence would leave (2) as a
comment rather than a check, and the mode's whole claim rests on it.

What this rung does *not* claim
-------------------------------
A proxy over raw input primitives cannot prove that no click ever lands on a
submit button. It can refuse the coordinates it was told about; it cannot know
that a mis-read layout put "Buy" somewhere else. The fence narrows that risk and
the journal makes it visible after the fact -- it does not eliminate it. Run
this rung watching the screen, not from another room.

So the invariant this module upholds is: **no mode defined here submits an order
on a real account.** Preparing one is now possible; committing it is the
operator's act, not the bot's.
"""

from __future__ import annotations

import enum


class ExecutionMode(enum.Enum):
    SIMULATOR_ONLY = "simulator_only"
    LIVE_READ_ONLY = "live_read_only"
    LIVE_PREPARE_ONLY = "live_prepare_only"

    @property
    def permits_live_account(self) -> bool:
        """Whether a real-money screen is observed rather than halted on."""
        return self in (
            ExecutionMode.LIVE_READ_ONLY,
            ExecutionMode.LIVE_PREPARE_ONLY,
        )

    @property
    def permits_order_tickets(self) -> bool:
        """Whether an order ticket may be opened and filled at all.

        False on the read-only rung. Opening a ticket is not submitting one:
        see `permits_orders`, which is the property that decides whether
        anything can actually reach the exchange.
        """
        return self in (
            ExecutionMode.SIMULATOR_ONLY,
            ExecutionMode.LIVE_PREPARE_ONLY,
        )

    @property
    def permits_orders(self) -> bool:
        """Whether this mode can ever submit an order. Never true on a real account."""
        return self is ExecutionMode.SIMULATOR_ONLY

    @property
    def requires_submit_fence(self) -> bool:
        """Whether the guard must be given a no-click rectangle before it runs.

        True exactly where the bot may fill a real ticket it must not submit.
        On the other rungs the fence is redundant -- the simulator has nothing
        at stake, and the read-only rung cannot open a ticket to begin with --
        so demanding one there would be ceremony rather than a check.
        """
        return self is ExecutionMode.LIVE_PREPARE_ONLY

    @property
    def banner(self) -> str:
        return {
            ExecutionMode.SIMULATOR_ONLY: "SIMULATOR ONLY",
            ExecutionMode.LIVE_READ_ONLY: "REAL ACCOUNT - READ ONLY, NO ORDERS",
            ExecutionMode.LIVE_PREPARE_ONLY: "REAL ACCOUNT - YOU PRESS SUBMIT",
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


class SubmitForbidden(RuntimeError):
    """Something inside an order block tried to commit it.

    Raised for the two routes that submit a form without anything that looks
    like pressing "Buy": an Enter-family key, and a click inside the calibrated
    fence around the broker's submit button. Like `OrderTicketsForbidden` this
    is raised at the proxy, so neither a model's chosen action nor a future
    execution flow can arrange to miss it.
    """


class SubmitFenceMissing(RuntimeError):
    """A mode that must not submit was built without the fence that stops it.

    Fails at construction rather than at the first click. A guard that only
    discovers this once it is already in front of a filled ticket has picked the
    worst possible moment to find out.
    """
