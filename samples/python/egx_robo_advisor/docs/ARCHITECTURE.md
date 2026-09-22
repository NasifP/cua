# Architecture decision records

Short records of the structural choices, with the reasoning that would otherwise
be lost. See `README.md` for the diagram and the runbook.

---

## ADR-001: Two processes, one SQLite file

**Decision.** The agent and the dashboard run as separate processes sharing
`state/egx_bus.db` (WAL mode) and a `HALTED` sentinel file. Neither imports the
other.

**Why not one process?** A kill switch inside the agent cannot be pressed when
the agent is wedged mid-click — which is precisely when you want it. Separation
also keeps the dashboard readable after an agent crash, and keeps the
internet-exposed component apart from the one driving a broker UI.

**Why not Redis / a message broker?** Nothing here needs it. The agent emits tens
of events per minute. SQLite WAL gives concurrent readers, atomic commits, and
durability with no daemon to keep alive on a laptop, and the state survives a
reboot without any extra work.

**Cost accepted.** Polling-based SSE with ~1s latency rather than push. For a
rebalancer that acts every few minutes, invisible.

---

## ADR-002: The guard is a proxy, not a convention

**Decision.** `GuardedInterface` wraps `Computer.interface` and is the only
handle the execution layer receives.

**Why.** Safety checks in the caller are advisory. The next order flow someone
adds forgets one, and nothing complains. A proxy makes the check unskippable:
there is no other object to call.

**Consequence.** `ComputerAgent` must be constructed over `GuardedComputer`, not
the raw `Computer`, because the cua handler does
`self.interface = self.cua_computer.interface` and then calls primitives on it.
Handing it the real object would let a model's chosen click bypass every gate.

---

## ADR-003: Re-assert before every mutating call

**Decision.** `reassert_every_action=True` by default: a fresh screenshot and
verdict before each click, rather than caching a verdict within its freshness
window.

**Why.** An earlier version cached the verdict per risk tier. A mid-session
switch to the live account then stayed invisible for the whole window, and clicks
kept landing on a real-money screen on the strength of a stale yes. The freshness
windows remain as a backstop for anyone who turns the flag off.

**Cost accepted.** One screenshot (~50–200ms) per click. A rebalancer issues tens
of clicks per session, so this is not a throughput concern.

---

## ADR-004: Unknown interface methods are order-critical

**Decision.** `classify()` returns `ORDER_CRITICAL` for any method not in the
three tables, and logs a warning.

**Why.** When the Cua SDK grows a new input primitive, the fail-safe direction is
"the bot demands a fresh two-source assertion", not "a new way to click slipped
past the guard". Relaxing it is a one-line, reviewable change.

---

## ADR-005: The regime filter may only subtract

**Decision.** `RegimeFilter.apply()` calls `_assert_subtractive()` on every
invocation, which raises if the output contains an order the input did not, an
enlarged quantity, or a flipped side. `Severity` has no positive band.

**Why.** Our backtests found news carries essentially no predictive alpha on EGX
names once retail fill delay is accounted for. "Use news as a filter, not a
signal" is the kind of discipline that erodes: it starts as a comment, someone
adds "if sentiment is very positive, upsize by 20%", and eighteen months later
the bot is a momentum chaser with a news feed. An executable invariant does not
erode.

**Consequence.** The LLM classifier composes with `max()` over severity, never as
an override. Its worst case is over-sensitivity (we stop buying for a while),
never a false all-clear.

---

## ADR-006: Fail-closed on silence

**Decision.** Stale or unpolled feeds produce `BUYS_HALTED` during a session.
`bus.is_halted()` returns `True` on any exception. An empty screenshot yields
`INDETERMINATE`. A fresh bus starts halted.

**Why.** Absence of bad news is not evidence of calm — it is far more likely that
the poller is broken. Every "we cannot tell" has to resolve towards inaction,
because the asymmetry of outcomes demands it: a missed rebalance costs a few
basis points of tracking error; an unintended order costs real money.

**Exception.** Out of session, quiet feeds are normal and do not latch a halt.
Otherwise the bot would open every Sunday already halted.

---

## ADR-007: Panic is latched

**Decision.** `RegimeFilter.panic()` sets `_panic_until`, and `evaluate()` returns
`ALL_HALTED` while it holds, regardless of what the feeds say. It clears on a
timer or via `clear_panic(actor=...)`.

**Why.** Found by a test. `panic()` originally only set the risk-off cooldown, so
the next `evaluate()` recomputed from the headlines and silently downgraded
`ALL_HALTED` to `BUYS_HALTED` — putting the bot back on the screen after we had
declared we did not trust our own data. A panic means exactly that, and a
subsequent calm poll is not evidence to the contrary.

---

## ADR-008: The strategy layer is pure

**Decision.** `plan_rebalance()` takes state and returns a plan. No clock, no
network, no screen, no regime consultation.

**Why.** It makes the strategy backtestable against replayed history and testable
without a broker, and it keeps generation separate from suppression so each can
be verified alone. It is also what makes the subtractive invariant checkable:
there are two distinct artefacts to compare.

---

## ADR-009: Prices do not come from the screen

**Decision.** `MarketDataProvider` is an explicit seam. `JsonFileMarketData`
serves development and backtests; production points at a real feed.

**Why.** Screen-scraped prices are low precision, arrive at whatever moment the
screenshot happened, and an OCR error on a decimal point silently multiplies an
order by ten. The portfolio read tolerates the screen because there is no
alternative — and even that is validated hard. Prices have an alternative.

---

## ADR-010: The UI map ships uncalibrated

**Decision.** `ThndrUiMap.calibration_complete` defaults to `False`, and
`submit_order()` refuses while it is.

**Why.** Nobody — including a language model — can know a third-party app's
current pixel geometry and label text from memory. Shipping plausible-looking
constants would produce code that reads as authoritative and clicks the wrong
button. Making the uncalibrated state explicit and blocking is more honest than
a comment saying "adjust these".

**Related.** Semantic navigation via `ComputerAgent` is preferred over
coordinates throughout: a model asked to "open the portfolio tab" adapts to a
redesign, whereas a hard-coded point silently clicks whatever moved into that
spot.
