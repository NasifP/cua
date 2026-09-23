# EGX Robo-Advisor — the running bot

Parts 1–4 of a safety-gated rebalancing bot for the Egyptian Exchange that drives
the **Thndr simulator** (paper trading) through [Cua](https://github.com/trycua/cua).

These slices contain the machinery that decides whether the bot may touch the
screen, what it would buy if allowed, when news should stop it, where its prices
come from, and the loop and dashboard that tie them together. The bot runs from
here; the backtester that measures it arrives in part 5.

> **Status: simulator only.** Nothing in this package is investment advice.

---

## What is here

| Module | Role |
|---|---|
| `types.py` | frozen value types shared by every layer |
| `clock.py` | EGX calendar: Sunday–Thursday, Cairo time, T+2 settlement |
| `bus.py` | the agent ↔ dashboard channel, and the kill switch |
| `safety/` | the demo-mode guard and the only sanctioned path to the screen |
| `strategy/` | allocation policy and drift-band rebalancing — pure, no I/O |
| `regime/` | news fetching, classification, and the circuit breaker |
| `marketdata/` | the price seam, with validation that blocks rather than degrades |
| `egx_cua_agent.py` | the loop and its five gates |
| `execution/` | the only module that drives the Thndr UI |
| `dashboard/` | mobile UI, live log, and the kill switch |

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

## The EGX strategy

`plan_rebalance()` is a pure function — state in, plan out, no clock, no network,
no screen. That is what makes it backtestable against replayed history and
testable without a broker.

**Allocation is deliberately dumb:**

- **Equal weight inside each sleeve.** No optimiser, no covariance estimate, no
  expected returns. Equal weighting has exactly one parameter — the membership
  list — and is the hardest allocation rule in existence to overfit.
- **Two macro states, one threshold.** Trailing EGP depreciation has either
  breached the devaluation trigger or it has not.

  | State | Equity | Gold hedge | Cash |
  |---|---|---|---|
  | Baseline | 60% | 30% | 10% |
  | Devaluation stress (≥15% trailing EGP depreciation) | 45% | 45% | 10% |

- **Drift bands, not schedules.** We trade when the portfolio has actually
  drifted, which is when rebalancing is worth its cost.
- **Parameters drawn from a sanctioned discrete set**, enforced at runtime:

  ```python
  >>> PolicyParameters(sleeve_drift_band=Decimal("0.0437"))
  ValueError: sleeve_drift_band=0.0437 is not a sanctioned value.
  Allowed: ['0.03', '0.05', '0.08']. If you need a different value, change the
  charter and ADMISSIBLE deliberately -- do not fit it.
  ```

  This is the one overfitting guardrail that survives contact with a motivated
  operator at 1am, because it is a raised exception rather than a paragraph.

**EGX frictions encoded**, because on this market they decide whether a plan is
real: board lots · ±10% daily price limits (we stand aside rather than chase a
limit-up name) · halts and stale prints · **T+2 settlement**, so today's sale
proceeds are not investable today · a commission floor · a turnover cap and
per-symbol cooldown to stop oscillation around a band edge.

Sells are planned before buys so de-risking never waits on cash, and an order
that busts the turnover budget is **trimmed, never dropped** — the largest drift
is the most important thing to correct, and skipping it in favour of two small
orders that happen to fit leaves the portfolio further from target.

> **Note on the gold sleeve.** `max_name_weight` is a single-*issuer*
> concentration limit and applies to the equity sleeve only. Applying it to a
> one-ETF hedge sleeve would clamp a 45% hedge target to 15% and make the entire
> devaluation response inert — a bug this codebase had, and now has a test for.

See [`docs/NO_OVERFIT_CHARTER.md`](docs/NO_OVERFIT_CHARTER.md) before changing
any strategy parameter.

---

## The news regime filter

Our backtests found news carries essentially no predictive alpha on EGX names
once retail fill delay is accounted for. So this layer is a **circuit breaker,
never a signal**, and that is enforced rather than documented:

```python
allowed, suppressed = regime_filter.apply(plan.orders, regime)
# apply() calls _assert_subtractive(orders, allowed), which raises
# SubtractiveInvariantViolation if the result contains an order the plan never
# had, an enlarged quantity, or a flipped side.
```

Three things follow, and each has a test:

- **`Severity` has no positive band.** "COMI.CA smashes earnings, shares surge
  20%" classifies as `NONE`. There is nowhere for a bullish score to live.
- **The LLM composes with `max()`, never override.** It can raise severity, never
  lower it. A timeout, a refusal, or a hallucinated all-clear cannot unblock
  trading. The deterministic keyword classifier is the primary, so the breaker
  still works with no API key.
- **Asymmetry.** `BUYS_HALTED` stops buying but still permits sells — refusing to
  let the bot de-risk during a crisis is its own kind of risk. Only `ALL_HALTED`
  stops everything, and a `panic()` latches until explicitly cleared.

Silence fails closed too: stale feeds mean `BUYS_HALTED`, because absence of bad
news is far more likely to mean a broken poller than a calm market. Outside
trading hours a quiet feed is normal and does not latch a halt.

Feeds are public RSS/Atom only — no logins, no paywalls, no scraping disallowed
by robots.txt. Confirm each publisher's terms before enabling one, and verify the
URLs in `config/feeds.toml` actually resolve: a feed that 404s quietly is a
coverage hole in the breaker.

---

## The loop, and why the gates are in this order

Each cycle runs the cheap, safe, offline checks first and only then reaches for
the screen:

```
1. kill switch      (a file read)        -> stop
2. trading calendar (arithmetic)         -> idle until the next session
3. news + regime    (network, no screen) -> may forbid buying, or everything
4. demo assertion   (screen, read-only)  -> may forbid clicking
5. plan + execute   (clicks)             -> per-order re-assertion
```

Gate 3 finishing before gate 4 begins is the property the design turns on: **the
news layer decides what is permitted before the bot is allowed to touch the
screen at all.** It is structural, not conventional. `GuardedInterface` is built
lazily inside `_ensure_connected()`, and on `ALL_HALTED` the cycle returns before
that line — so no interface object exists and nothing in scope can click:

```python
async def test_all_halted_regime_never_touches_the_screen(tmp_path):
    agent.regime_filter.panic("simulated data integrity failure")
    phase = await agent._run_cycle()

    assert news.polls == 1                    # news still polled
    assert computer.interface_accesses == 0   # no interface constructed
    assert computer._iface.screenshots == 0   # no screenshot taken
    assert market.calls == 0                  # never even priced the universe
```

The plan comes from the pure strategy layer, which knows nothing of the news; the
regime filter then subtracts from it. Keeping generation and suppression apart is
what makes "news can only remove trades" checkable rather than aspirational.

### The LLM cannot route around the guard

`ComputerAgent` does not click through us — it takes a `Computer` and reaches for
`computer.interface` itself. Hand it the real one and every safety property is
bypassed by a model deciding where to tap. So it never gets the real one:

```python
agent = ComputerAgent(tools=[GuardedComputer(computer, guarded_interface)])
```

---

## The dashboard

Mobile-first, one screen. Bot state, risk regime, account guard, holdings and
drift, the current plan with a rationale per order, a live SSE log of every
primitive the bot sends to the screen, and a **KILL SWITCH** fixed to the bottom
of the viewport.

It runs as its own process, sharing only the bus file, because the kill switch
must work when the agent loop is wedged mid-click — a button inside the stuck
process cannot be pressed.

**Asymmetric friction:** halting is one tap with no confirmation dialog — if
someone is reaching for that button they want the bot stopped *now*. Resuming
requires typing an exact phrase. Stopping should always be easier than starting,
and the bot never resumes itself.

All CSS is inline: no CDN, no external font. This page is the remote stop button
for something that places orders, and a stylesheet fetched from a third party is
a dependency the kill switch does not need.

---

## Running it

```bash
pip install -e '.[agent,ocr,dashboard,marketdata,test]'
cp .env.example .env    # then fill it in
```

**Read the reasoning without a broker or a device:**

```bash
python demo_dry_run.py
```

`run_agent.py --dry-run` withholds orders but still reads the portfolio off a
live screen, so it needs Cua credentials and a device. `demo_dry_run.py`
substitutes a scripted screen and a fixture price file and runs everything else
for real across seven scenarios: calm, devaluation, EGX frictions, catastrophic
news, bullish news, feeds down, and the safety layer.

**Terminal 1 — dashboard:**

```bash
export EGX_DASHBOARD_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python dashboard/app.py
# open http://127.0.0.1:8787/?token=$EGX_DASHBOARD_TOKEN
```

**Terminal 2 — agent (dry run first, always):**

```bash
python run_agent.py --dry-run
```

The bot starts **halted**. Arm it from the dashboard by typing the resume phrase.

### Getting it on your phone

The app binds `127.0.0.1` and refuses a non-loopback bind unless you set
`EGX_ACKNOWLEDGE_PUBLIC_BIND=yes`. It is a remote control for something that
places orders, so put it behind a tunnel that terminates TLS and authenticates —
Cloudflare Tunnel or Tailscale — rather than exposing it directly.

### Before you ever pass `--calibrated`

`ThndrUiMap.calibration_complete` gates order submission and defaults to `False`
for a reason: nobody — including a language model — can know a third-party app's
current geometry and label text from memory. Guessing produces code that looks
authoritative and clicks the wrong button.

1. Screenshot every relevant Thndr screen **in the simulator**.
2. Verify each label in `ThndrUiMap` against them.
3. Re-measure `DEMO_COLOUR_SIGNATURES` from the real simulator badge.
4. Confirm every symbol in `config/policy.egx.toml` against the live EGX listing.
5. Run for several sessions with `--dry-run` and read the plans.
6. Only then pass `--calibrated`.

---

## Market data

```bash
pip install -e '.[marketdata]'
python verify_market_data.py              # check the feed before trusting it
python verify_market_data.py --history 1825
```

`MarketDataProvider` is the seam. Prices are the denominator of every weight and
the multiplier on every order size, so a bad feed does not produce a slightly
worse trade — it produces an order off by a factor. Providers therefore
**validate what they fetched and raise rather than degrade**, the same posture as
the demo guard.

`YahooMarketData` is the live path. Yahoo quotes EGX with the same `.CA` suffix
this codebase already uses, needs no key, and is delayed and unofficial — good
enough to develop and backtest against, to be verified before it sizes a live
order.

Blocking findings (the snapshot is refused):

| Code | Why it can misprice an order |
|---|---|
| `CURRENCY_MISMATCH` | a USD figure read as EGP scales orders ~50× |
| `IMPLAUSIBLE_MOVE` | beyond EGX's ±10% band is usually an unadjusted split; sizing against the pre-split price buys 5× the shares |
| `MISSING_SYMBOL` | a universe member with no data is an unknown weight, not a zero one — it inflates every other weight |
| `STALE_BAR` | yesterday's close during a devaluation is not a price |
| `NON_POSITIVE`, `INVERTED_RANGE`, `CLOSE_OUTSIDE_RANGE` | the row is not what it claims |

`ZERO_VOLUME` and `THIN_VOLUME` are warnings only. **Halts are never inferred
from price data**: Yahoo cannot distinguish a suspension from a holiday or a
gap, so `Tradability.HALTED` is left to EGX disclosures via the regime layer.
Claiming otherwise would be worse than admitting the gap.

A missing USD/EGP series leaves the policy at baseline — it can fail to *detect*
a devaluation, never trigger one.

> **`verify_market_data.py` cannot tell you the prices are correct.** It catches
> faults self-evident from the data. A feed quietly wrong by 2% passes every
> check. Compare the printed closes against egx.com.eg for a few sessions
> yourself; the script ends with the four manual checks that matter.

---

## Running the tests

```bash
cd samples/python/egx_robo_advisor
pip install -e '.[test]'
python -m pytest        # 166 tests, no network, broker or GPU needed
```

`ruff check --select E,F,B,I` is clean.

---

## What comes next

| Part | Adds |
|---|---|
| 5 | the backtester |

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the decision records
behind the structure above.
