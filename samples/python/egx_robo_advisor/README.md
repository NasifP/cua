# EGX Robo-Advisor — foundation and safety layer

Part 1 of a safety-gated rebalancing bot for the Egyptian Exchange that drives
the **Thndr simulator** (paper trading) through [Cua](https://github.com/trycua/cua).

This slice contains the machinery that decides whether the bot is allowed to
touch the screen at all. The strategy, the agent loop, the dashboard, the market
data and the backtester arrive in later parts; nothing here trades on its own.

> **Status: simulator only.** Nothing in this package is investment advice.

---

## What is here

| Module | Role |
|---|---|
| `types.py` | frozen value types shared by every layer |
| `clock.py` | EGX calendar: Sunday–Thursday, Cairo time, T+2 settlement |
| `bus.py` | the agent ↔ dashboard channel, and the kill switch |
| `safety/` | the demo-mode guard and the only sanctioned path to the screen |

---

## The chokepoint

Safety checks that live in the *caller* are advisory: the next person to add an
order flow forgets one, and nothing complains. So `GuardedInterface` is not a
function the execution layer is asked to call politely — it is a proxy wrapping
Cua's `Computer.interface`, and it is **the only handle the execution layer is
ever given**. There is no code path from strategy to screen that skips it,
because there is no other object to call.

Every mutating call passes, in order:

1. **Kill switch** — `bus.is_halted()`, fail-closed.
2. **Demo assertion** — fresh enough for this action's risk tier.
3. **Journal** — published to the bus *before* the action, so a crash still
   leaves a record of what the bot was reaching for.
4. **The actual call.**
5. **Post-verification** (order blocks) — did the screen change identity mid-flow?

### Risk tiers

| Tier | Examples | Requires |
|---|---|---|
| `READ_ONLY` | `screenshot`, `get_screen_size` | kill switch only |
| `NAVIGATION` | `left_click`, `scroll` | 1 corroborating source |
| `ORDER_CRITICAL` | `type_text`, anything inside `order_critical()` | 2 sources, ≤1.5s old |

`left_click` is ordinary navigation when walking a watchlist, and order-critical
when it is the Buy button — same primitive, different stakes, decided by the
caller's context rather than by guessing from coordinates.

**Unrecognised methods get the strictest tier.** When the Cua SDK grows a new
input primitive, the failure mode is "the bot demands a fresh two-source
assertion", not "a new way to click slipped past unnoticed".

### Re-assertion, not caching

A verdict is taken fresh before *every* mutating call. Caching one across calls
opens a window in which the account can switch to real money unnoticed — you tap
the account switcher, a deep link opens the live tab — and the bot keeps clicking
on the strength of a stale yes. A rebalancer issues tens of clicks per session,
not thousands, so one screenshot per click is the right trade:

```python
async def test_account_flipping_to_live_is_caught_on_the_very_next_call(armed):
    await guarded.left_click(1, 1)
    raw.labels = ["Real Account"]          # account flips mid-session
    with pytest.raises(DemoModeViolation):
        await guarded.left_click(2, 2)
    assert len(raw.calls) == before         # not one click landed
    assert bus.is_halted()                  # and the bot latched itself off
```

### Evidence rules

- **Any real-money marker wins outright**, even alongside a simulator marker. A
  screen showing both is a screen we do not understand, and ambiguity must never
  resolve towards clicking.
- **Bare "real" and "live" are NOT markers.** An EGX screen is full of "Real
  Estate" — that is TMGH's own sector — and "live price" chrome. Negative markers
  are specific multi-word phrases only; a guard that cries wolf gets switched off.
- **Arabic and English both**, with normalisation for diacritics, alef variants
  and ta-marbuta, because the app's language follows the phone's.
- **Degradation never loosens the gate.** No working probe means no evidence,
  which means `INDETERMINATE`, which means no click.

Probes are independent families — published accessibility labels, OCR
(`pytesseract`, eng+ara), and a dependency-free accent-colour check built on a
minimal PNG decoder so the guard can run on a bare interpreter.

---

## The bus and the kill switch

`StateBus` is one SQLite file in WAL mode: many concurrent readers alongside one
writer, atomic commits, durability across a crash, and no broker to keep alive on
a laptop.

The halt is deliberately redundant — a row in `control` **and** a sentinel file.
The sentinel is written first, so a crash between the two writes leaves the bot
halted rather than armed. `is_halted()` fails closed: a corrupt database, an
exception, or a missing row all read as halted. Deleting the database does not
un-halt the bot:

```python
def test_sentinel_file_survives_a_destroyed_database(bus_path):
    bus.halt(actor="mobile", reason="kill switch from phone")
    os.remove(bus_path)
    assert StateBus(bus_path).is_halted()
```

A fresh bus starts **halted**. Arming is always an explicit act.

---

## EGX calendar

Sunday–Thursday; Friday and Saturday are the weekend. The continuous session is
10:00–14:30 Cairo time, resolved through `Africa/Cairo` rather than a hard-coded
UTC+2 because Cairo observes DST again as of 2023. A rebalancer has no business
in the opening or closing auction, so the tradeable window is narrowed at both
ends. Equities settle T+2 in session days.

Holidays follow the Hijri calendar and are published annually by EGX, so they are
data, not a rule to compute. The empty default set is honest about knowing
nothing.

---

## Running the tests

```bash
cd samples/python/egx_robo_advisor
pip install -e '.[test]'
python -m pytest        # 71 tests, no network, broker or GPU needed
```

`ruff check --select E,F,B,I` is clean.

---

## What comes next

| Part | Adds |
|---|---|
| 2 | EGX strategy and the news regime filter |
| 3 | market data providers with validation |
| 4 | the agent loop, Thndr execution, and the mobile dashboard |
| 5 | the backtester |

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the decision records
behind the structure above.
