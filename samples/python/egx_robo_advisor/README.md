# EGX Robo-Advisor

A safety-gated rebalancing bot for the Egyptian Exchange. It drives the **Thndr
simulator** (paper trading) through [Cua](https://github.com/trycua/cua), and is
built so that touching a real-money account requires a chain of failures rather
than a single one.

> **Status: simulator only.** Order submission is refused until the UI map is
> calibrated against real screenshots, and the bot starts halted every time.
> Nothing here is investment advice.

---

## The four pillars

| # | Pillar | Where it lives | The guarantee |
|---|---|---|---|
| 1 | **Strict demo mode** | `egx_advisor/safety/` | No input reaches the screen without a fresh, corroborated "this is the simulator" verdict |
| 2 | **EGX strategy** | `egx_advisor/strategy/` | Pure, deterministic, and structurally hard to overfit |
| 3 | **News regime filter** | `egx_advisor/regime/` | News can only ever *remove* trades — enforced at runtime |
| 4 | **Mobile dashboard** | `dashboard/` | A kill switch that works even when the agent is wedged |

---

## Architecture

Two processes. One file between them. Nothing else.

```
┌─────────────────────────── AGENT PROCESS ───────────────────────────┐
│                                                                     │
│  ┌── GATE 1 ────┐  kill switch (a file read)                        │
│  │  bus.is_halted()  ── halted? ──► stop. no network, no screen.    │
│  └──────┬───────┘                                                   │
│         ▼                                                           │
│  ┌── GATE 2 ────┐  EGX calendar (arithmetic)                        │
│  │  Sun–Thu, 10:15–14:10 Cairo ── closed? ──► idle until next open  │
│  └──────┬───────┘                                                   │
│         ▼                                                           │
│  ┌── GATE 3 ────┐  NEWS + REGIME        ◄── no screen access yet    │
│  │                                                                  │
│  │   NewsFetcher ──► KeywordClassifier ──┐                          │
│  │   (RSS/Atom)      (offline, primary)  ├─ combine(max severity)   │
│  │                   LlmClassifier ──────┘   (can only escalate)    │
│  │                          │                                       │
│  │                          ▼                                       │
│  │                   RegimeFilter ──► RISK_ON | BUYS_HALTED |       │
│  │                                             ALL_HALTED           │
│  │                                                                  │
│  │   ALL_HALTED ──► return BEFORE _ensure_connected().              │
│  │                  No interface is constructed. Nothing in scope    │
│  │                  is capable of clicking.                          │
│  └──────┬───────┘                                                   │
│         ▼                                                           │
│  ┌── GATE 4 ────┐  DEMO ASSERTION                                   │
│  │   GuardedInterface wraps Computer.interface                      │
│  │   ├─ accessibility labels ─┐                                     │
│  │   ├─ OCR (eng+ara) ────────┼─► DemoGuard ─► CONFIRMED_DEMO       │
│  │   └─ accent colour ────────┘               CONFIRMED_LIVE ─► HALT│
│  │                                            INDETERMINATE  ─► HALT│
│  └──────┬───────┘                                                   │
│         ▼                                                           │
│  ┌── GATE 5 ────┐  PLAN ──► FILTER ──► EXECUTE                      │
│  │                                                                  │
│  │   plan_rebalance(portfolio, market, policy)   ← knows no news    │
│  │           │                                                      │
│  │           ▼                                                      │
│  │   RegimeFilter.apply(orders, regime)          ← only subtracts   │
│  │           │        └─ _assert_subtractive() raises on violation  │
│  │           ▼                                                      │
│  │   ThndrExecutor.submit_order()  ← re-asserts before EVERY click  │
│  └─────────────────────────────────────────────────────────────────┘
│                              │         ▲                            │
└──────────────────────────────┼─────────┼────────────────────────────┘
                     writes    │         │  reads
                     events    ▼         │  control
                  ┌────────────────────────────────┐
                  │   state/egx_bus.db  (SQLite)   │
                  │   events │ snapshot │ control  │
                  │   + HALTED sentinel file       │
                  └────────────────────────────────┘
                     reads     │         ▲  writes
                     events    ▼         │  halt/resume
┌──────────────────────── DASHBOARD PROCESS ──────────────────────────┐
│   FastAPI + SSE + Tailwind                                          │
│   GET  /                    mobile UI                               │
│   GET  /api/state           holdings, drift, regime, guard verdict   │
│   GET  /api/events/stream   live click log (SSE)                    │
│   POST /api/control/halt    ◄── THE KILL SWITCH (one tap)           │
│   POST /api/control/resume  ◄── requires a typed phrase             │
└─────────────────────────────────────────────────────────────────────┘
```

### Why two processes

The kill switch has to work **when the agent loop is wedged mid-click**. A button
living inside the stuck process cannot be pressed. Splitting them also means the
dashboard stays readable after the agent crashes — the last thing it did is still
on screen when you open your phone — and keeps the internet-exposed component
separate from the one driving a broker UI.

SQLite in WAL mode is the transport: many concurrent readers alongside one
writer, atomic commits, durability across a crash, and no broker to keep alive on
a laptop. The agent is the sole writer of `events` and `snapshot`; the dashboard
is the sole writer of `control`.

---

## How `egx_cua_agent.py` talks to the dashboard

**Agent → dashboard** (`egx_advisor/bus.py`):

```python
bus.publish(EventKind.UI_ACTION, "left_click(412, 880)", phase="executing")
bus.put("regime", {"risk_state": "buys_halted", "drivers": [...]})
```

Events are an append-only journal with a monotonic `seq`. The dashboard streams
them over SSE using that `seq` as a cursor, so a phone that drops off wifi
reconnects and resumes exactly where it left off. Snapshots are last-write-wins
key/value rows for things with no history worth keeping (current holdings, the
live plan, the latest demo verdict).

**Dashboard → agent**:

```python
bus.halt(actor="mobile:pola", reason="KILL SWITCH pressed from the dashboard")
```

The agent checks `bus.is_halted()` before **every single mutating call**, not once
per cycle, and again between orders. `is_halted()` fails closed: a corrupt
database, an exception, or a missing row all read as halted.

The halt is deliberately redundant — a row in `control` **and** a sentinel file.
The sentinel is written first, so a crash between the two writes leaves the bot
halted rather than armed. Deleting the database does not un-halt the bot:

```python
def test_sentinel_file_survives_a_destroyed_database(bus_path):
    bus.halt(actor="mobile", reason="kill switch from phone")
    os.remove(bus_path)
    assert StateBus(bus_path).is_halted()
```

---

## How news reaches the Regime Filter before the bot touches the screen

The ordering is structural, not conventional. In `EgxCuaAgent._run_cycle`:

```python
# --- Gate 3: news and regime, BEFORE any screen access -----------------
regime = await self._refresh_regime(now, in_session=True)

if regime.risk_state is RiskState.ALL_HALTED:
    # Return before `_ensure_connected()`: in this state no interface is
    # constructed, so there is nothing in scope that could click.
    return AgentPhase.HALTED

# --- Gate 4: demo assertion --------------------------------------------
executor = await self._ensure_connected()   # ← the interface is born here
```

The `GuardedInterface` is created lazily *inside* `_ensure_connected()`. On
`ALL_HALTED` that line is never reached, so no interface object exists and no
screenshot is taken. This is asserted end to end:

```python
async def test_all_halted_regime_never_touches_the_screen(tmp_path):
    agent.regime_filter.panic("simulated data integrity failure")
    phase = await agent._run_cycle()

    assert news.polls == 1                    # news still polled
    assert computer.interface_accesses == 0   # no interface constructed
    assert computer._iface.screenshots == 0   # no screenshot taken
    assert market.calls == 0                  # never even priced the universe
```

### The filter can only subtract

Our backtests said news carries essentially no predictive alpha on EGX names, so
the filter is a **circuit breaker, never a signal**. That is enforced rather than
documented:

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
  trading.
- **Asymmetry.** `BUYS_HALTED` stops buying but still permits sells — refusing to
  let the bot de-risk during a crisis is its own kind of risk. Only `ALL_HALTED`
  stops everything.

Silence fails closed too: stale feeds mean `BUYS_HALTED`, because absence of bad
news is far more likely to mean a broken poller than a calm market.

---

## Pillar 1: how the demo-mode gate actually holds

Safety checks that live in the *caller* are advisory — the next person to add an
order flow forgets one. So `GuardedInterface` is not a function the execution
layer is asked to call politely. It is a proxy wrapping `Computer.interface`, and
it is **the only handle the execution layer is ever given**. There is no code path
from strategy to screen that skips it, because there is no other object to call.

Every mutating call passes, in order:

1. **Kill switch** — `bus.is_halted()`, fail-closed.
2. **Demo assertion** — fresh enough for this action's risk tier.
3. **Journal** — published to the bus *before* the action, so a crash still leaves
   a record of what the bot was reaching for.
4. **The actual call.**
5. **Post-verification** (order blocks) — did the screen change identity mid-flow?

### Risk tiers

| Tier | Examples | Requires |
|---|---|---|
| `READ_ONLY` | `screenshot`, `get_screen_size` | kill switch only |
| `NAVIGATION` | `left_click`, `scroll` | 1 corroborating source |
| `ORDER_CRITICAL` | `type_text`, anything inside `order_critical()` | 2 sources, ≤1.5s old |

`left_click` is ordinary navigation when walking the watchlist, and
order-critical when it is the Buy button — same primitive, different stakes,
decided by the caller's context rather than by guessing from coordinates.

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
- **Arabic and English both**, with normalisation for diacritics, alef variants,
  and ta-marbuta, because the app's language follows the phone's.
- **Degradation never loosens the gate.** No working probe means no evidence,
  which means `INDETERMINATE`, which means no click.

### The LLM cannot route around it

`ComputerAgent` does not click through us — it takes a `Computer` and reaches for
`computer.interface` itself. Hand it the real one and every safety property here
is bypassed by a model deciding where to tap. So it never gets the real one:

```python
agent = ComputerAgent(tools=[GuardedComputer(computer, guarded_interface)])
```

`GuardedComputer` delegates everything except `.interface`, which returns the
guarded proxy. Model-chosen clicks pass through the same gates as scripted ones.

---

## Pillar 2: the EGX strategy layer

`plan_rebalance()` is a pure function — state in, plan out, no clock, no network,
no screen. That is what makes it backtestable against a replayed history and
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

---

## Pillar 4: the dashboard

Mobile-first, one screen, no scrolling required for the things that matter.

- **Bot state** — ARMED / HALTED, with who did it and why.
- **Risk regime** — RISK ON / BUYS HALTED / ALL HALTED, the driving headline, and
  feed freshness.
- **Account guard** — SIMULATOR / REAL MONEY / UNCONFIRMED, with source count and
  verdict age.
- **Holdings & drift** — current vs target weight per name, with the drift bar
  turning amber outside the band.
- **Current plan** — every order with its one-line rationale, plus a collapsible
  list of what was withheld and why.
- **Live agent log** — SSE stream of every primitive the bot sends to the screen.
- **KILL SWITCH** — fixed to the bottom of the viewport, always reachable.

All CSS is inline: no CDN, no external font. This page is the remote stop button
for something that places orders, and a stylesheet fetched from a third party is
a dependency the kill switch does not need. On a blocked network, an offline
phone, or during a CDN outage, a utility-class CDN would leave the page as
unstyled HTML with the most important control reduced to a plain link below the
fold. Self-contained means the button is always the big red one.

**Asymmetric friction:** halting is one tap with no confirmation dialog — if
someone is reaching for that button they want the bot stopped *now*, and a modal
between them and that is a liability. Resuming requires typing an exact phrase.
Stopping should always be easier than starting, and the bot never resumes itself.

---

## Running it

```bash
cd samples/python/egx_robo_advisor
pip install -e '.[agent,ocr,dashboard,test]'
cp .env.example .env    # then fill it in

python -m pytest              # 194 tests, no network or broker needed
```

**Terminal 1 — dashboard:**

```bash
export EGX_DASHBOARD_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python dashboard/app.py
# open http://127.0.0.1:8787/?token=$EGX_DASHBOARD_TOKEN
```

The token is swapped for an HttpOnly cookie on first load, so it stops appearing
in history and the phone's address bar.

**Read the reasoning without a broker or a device:**

```bash
python demo_dry_run.py
```

`run_agent.py --dry-run` withholds orders but still reads the portfolio off a
live screen, so it needs Cua credentials and a device. `demo_dry_run.py`
substitutes a scripted screen and a fixture price file and runs everything else
for real — the same fetcher, classifier, filter, planner, guard and bus — across
seven scenarios: calm, devaluation, EGX frictions, catastrophic news, bullish
news, feeds down, and the safety layer. Every number in it is an illustrative
fixture, not market data.

**Terminal 2 — agent (dry run first, always):**

```bash
python run_agent.py --dry-run
```

The bot starts **halted**. Arm it from the dashboard by typing the resume phrase.

### Getting it on your phone

The app binds `127.0.0.1` and refuses a non-loopback bind unless you set
`EGX_ACKNOWLEDGE_PUBLIC_BIND=yes`. It is a remote control for something that
places orders, so put it behind a tunnel that terminates TLS and authenticates —
Cloudflare Tunnel or Tailscale — rather than exposing it directly:

```bash
cloudflared tunnel --url http://127.0.0.1:8787
```

### Before you ever pass `--calibrated`

`ThndrUiMap.calibration_complete` gates order submission, and it defaults to
`False` for a reason: nobody — including a language model — can know a third-party
app's current geometry and label text from memory. Guessing here produces code
that looks authoritative and clicks the wrong button.

1. Screenshot every relevant Thndr screen **in the simulator**.
2. Verify each label in `ThndrUiMap` against them.
3. Re-measure `DEMO_COLOUR_SIGNATURES` from the real simulator badge.
4. Confirm every symbol in `config/policy.egx.toml` against the live EGX listing —
   especially the gold ETF's code.
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

## Backtesting

```bash
python run_backtest.py --synthetic                   # exercise the engine
python run_backtest.py --csv prices.csv --macro fx.csv
```

The strategy layer is pure, so replaying it is cheap. What the backtester is
*for* is the part that matters: **measuring frictions, not discovering
parameters.**

It runs three configurations and compares them:

| Run | Answers |
|---|---|
| `policy` | what actually happens, costs paid |
| `frictionless` | what commission, duty and slippage cost you |
| `buy & hold` | whether rebalancing beats leaving it alone |

Three things keep it honest:

- **A limit only fills if the day traded through it.** Assuming every order
  fills at the close is the single assumption behind most backtests that cannot
  be reproduced live. Fills are also capped by volume participation, so you
  cannot take half a thin name's daily turnover for free. On synthetic data the
  fill rate lands near 57% — the rejected orders are recorded, because a plan is
  not a fill and dropping them silently overstates how well the policy tracked.
- **No lookahead, asserted by a test.** Orders are priced off the *previous*
  close and filled against the *current* day's range. Truncating the history must
  not change the days that remain, and `test_no_lookahead...` proves it.
- **Every comparison carries a block-bootstrap confidence interval.** Any
  interval straddling zero prints `INDISTINGUISHABLE FROM NOISE`. Daily returns
  are autocorrelated, so the blocks are circular rather than independent draws —
  plain resampling would produce intervals far too narrow.

The runner also prints a sample-size warning: under eight years it says outright
that the history is long enough to compare frictions but not to choose
parameters. That is the numerical counterpart to `PolicyParameters.validate()` —
the charter forbids fitting, and this makes visible when a fitted difference
would have been meaningless anyway.

> `--synthetic` generates random prices. It exercises the engine and the cost
> model; it says nothing about the EGX. The output labels itself as such.

**Before trusting any figure, replace `CostModel` with your broker's real
schedule.** The defaults are deliberately pessimistic placeholders — the stamp
duty rate in particular has changed repeatedly and must be confirmed.

## What is deliberately not here

- **No live-account support.** Not a missing feature, a design boundary.
- **No holiday calendar.** EGX holidays follow the Hijri calendar and are
  published annually. An empty set is honest about knowing nothing.
- **No backtester.** The strategy layer is pure and deterministic, so one is
  straightforward to add — but a backtest of this policy would mostly measure the
  handful of EGP devaluations in the sample, which is exactly the overfitting the
  charter exists to prevent.

---

## Layout

```
egx_advisor/
  types.py            frozen value types shared by every layer
  clock.py            EGX calendar: Sun–Thu, Cairo time, T+2
  bus.py              the agent ↔ dashboard channel
  marketdata/
    base.py           provider protocol, findings, blocking rule
    quality.py        the checks and why each one blocks
    yahoo.py          live provider (.CA tickers), halts never inferred
    jsonfile.py       development and backtest path
  egx_cua_agent.py    the loop and the five gates
  safety/
    demo_guard.py     evidence rules and the three-way verdict
    vision.py         OCR / accessibility / colour probes
    guarded_interface.py   the chokepoint proxy
    pngutil.py        dependency-free PNG decode
  strategy/
    policy.py         universe, sleeves, sanctioned parameters
    rebalance.py      drift bands and EGX frictions
  regime/
    sources.py        RSS/Atom fetching
    sentiment.py      keyword + LLM classifiers
    filter.py         circuit breaker + subtractive invariant
  execution/
    thndr.py          UI flows, GuardedComputer
  backtest/
    engine.py         replay loop, T+2 settlement, no lookahead
    fills.py          limit/volume/halt fill model
    metrics.py        cost reporting + block-bootstrap intervals
    data.py           CSV loader and a synthetic generator
demo_dry_run.py       seven scenarios against a scripted screen
run_backtest.py       three-way friction comparison
verify_market_data.py check a feed before trusting it
dashboard/
  app.py              FastAPI + SSE + control endpoints
  templates/index.html   self-contained: inline CSS, no CDN
docs/
  ARCHITECTURE.md     decision records
  NO_OVERFIT_CHARTER.md
tests/                one file per pillar; the dashboard tests skip without FastAPI
```

See [`docs/NO_OVERFIT_CHARTER.md`](docs/NO_OVERFIT_CHARTER.md) before changing any
strategy parameter, and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the
decision records behind the structure above.
