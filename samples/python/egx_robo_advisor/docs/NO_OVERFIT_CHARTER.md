# No-Overfit Charter

Rules governing changes to the strategy layer. This exists because the failure
mode of a personal trading bot is almost never "the code crashed" — it is "the
parameters were quietly tuned until the backtest looked good".

## Why the EGX makes this worse than usual

The usable history contains only a handful of genuinely independent macro
episodes. The 2016 float, the 2022–2024 devaluations, and the COVID drawdown are
not twelve years of independent observations; they are a few events with a lot of
daily prints attached. Any model with more than a couple of free parameters will
fit *those specific episodes* rather than learn anything transferable. The market
is also concentrated, so cross-sectional breadth does not rescue you either.

## The rules

1. **Equal weight inside a sleeve.** No optimiser, no covariance matrix, no
   expected-return estimates. The only allocation decision is membership.

2. **Parameters come from `ADMISSIBLE`.** Every tunable in `PolicyParameters` must
   take one of two or three sanctioned values. `PolicyParameters.validate()`
   raises otherwise. A value like `0.0437` is a fitted parameter by construction.

3. **Widening `ADMISSIBLE` is a charter change.** It requires a reason that is not
   "it backtested better", written down in `docs/ARCHITECTURE.md` with a date.

4. **Two macro states, maximum.** Baseline and devaluation-stress. No third
   regime, no interpolation, no continuous risk score. If a third state seems
   necessary, that is evidence the first two are mis-specified.

5. **Universe changes are rare and recorded.** Adding or removing a name is a
   deliberate decision with a rationale, not the output of a screen. Index
   membership changes and delistings are the normal legitimate triggers.

6. **News never enters the strategy layer.** `plan_rebalance()` accepts a
   `RegimeState` for reporting only and must never consult it to decide what to
   buy. The regime filter subtracts afterwards. This is enforced by
   `_assert_subtractive()`.

7. **No parameter may be conditioned on a specific symbol.** The moment the
   policy contains "except for COMI.CA", it has memorised the sample.

8. **Backtests inform membership and frictions, never parameter values.** It is
   legitimate to use history to check that a lot size is right or that the
   commission floor is realistic. It is not legitimate to use it to pick the drift
   band.

## What a legitimate change looks like

> **2026-03-14 — raised `min_order_notional_egp` from 5,000 to 8,000.**
> Reason: broker commission schedule changed; the round-trip cost on a 5,000 EGP
> order now exceeds the drift it corrects. This is a cost-structure fact, not a
> performance tuning. No backtest was consulted.

## What an illegitimate change looks like

> ~~Lowered `name_drift_band` to 0.022 — improved Sharpe from 0.71 to 0.94 in
> backtest.~~

Rejected by rule 2, and rule 8, and the constructor.
