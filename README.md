# High-Beta Momentum Sprint — builderr Trading Agents, Round 2

A deterministic, standard-library-only `decide()` agent for the
[builderr Trading Agent](https://builderr.ai/trading-v0) Round 2 forward test,
built on the official
[starter template](https://github.com/builderr-ai/builderr-trading-template) (MIT).

## One command

```bash
python preview.py            # the official local admission preview (no install, no network)
```

On a Windows console that is not UTF-8, use `python -X utf8 preview.py` (the
template prints ✓/✗ characters). Python 3.10+; nothing to install
(`requirements.txt` is empty on purpose).

Other checks:

```bash
python selfcheck.py                          # template smoke test + secret scan
python -m unittest discover -s tests -v      # 16 unit tests for agent.py
python research/window_study.py              # 8-session forward-window study (~15 s)
```

## Why a high-variance design (the honest objective)

Round 2 ranks entries by **% return from each entry's own first scored session**.
An entry submitted for the 15 Nov 2026 cutoff is scored over at most ~8 sessions
(16–25 Nov). On the 13 Nov board the podium sat at +4.48 / +3.25 / +2.90 %, and the
leading entries move about 0.5–1.0 % a day. Over 8 sessions a calm book (daily
sd ≈ 1 %) reaches +3 % only about 1 time in 7, so this agent deliberately runs a
**high-variance** book: it aims for roughly 2.5–3 % daily volatility in the
strongest, most volatile names it is given. That raises the chance of a podium
finish and, equally, the chance of a loss. It is a deliberate bet on spread, not
a claim of edge over 8 sessions. Hard rules keep it far from every competition
limit (see below), because a breach or disqualification is worse than a poor
return.

## Strategy

Every session, using only the bars in `market_state`:

1. **Select.** Score every plain (1x) ticker:
   `0.5 × 63-day return + 0.3 × 21-day return + 0.2 × gap above the 50-day mean`.
   Keep names with a positive score whose 60-day volatility is at least 0.9× the
   index's (QQQ, else SPY). Hold the **top 4**, equal weight; a holding stays while
   it ranks in the top 5 (less churn).
2. **Size by volatility.** Estimate the basket's daily volatility from the last 20
   sessions (aligned dates only, never forward-filled). Scale the basket to
   **3.0 %/day in a calm market up-trend** (index above its 20/50-day means,
   20-day mean above 50-day mean, 20-day index vol < 30 %) and **2.5 %/day
   otherwise**, capped at 97 % invested and 24 % per name.
3. **Booster (calm up-trends only).** If the basket is still below its volatility
   target at the cap, move 15 % of the book into the 3x ETF (SOXL or TQQQ) whose
   underlying (SMH/SOXX or QQQ) is in an up-trend with the stronger score.
   Beta-adjusted gross then tops out at 0.82 + 3 × 0.15 = 1.27x.
4. **Crash brake.** Go to cash while the index's 5-session return is ≤ −7 % or its
   10-session annualised volatility is ≥ 60 %.
5. **Drawdown governor.** Half size at −12 % from the account peak; flat at −20 %,
   stay flat 5 sessions, then restart from the new peak.

Orders: sells before buys, whole shares, buys sized for 0.3 % slippage/gap with
the cash left after sells, trades smaller than 2 % of equity skipped (1 % for
2x/3x ETFs), at most 40 orders.

## Limits handling

| Rule (organiser) | Engine behaviour | This agent |
|---|---|---|
| Single position < 30 % (breach if ≥ 30 % for > 5 sessions) | buys clipped at 30 % of equity at the open | target ≤ 24 %; forced trim back to target if any name is above 27 % at a decision |
| Beta-adjusted gross ≤ 1.5x (3x/2x ETFs count 3/2) | buys clipped at 1.5x | ≤ 0.96x without booster (4 × 24 %), ≤ 1.27x with it; forced trim of 2x/3x names above 1.35x |
| Long only, cash only | sells capped at holdings, buys capped at cash | never sells more than held; buys sized from cash + sell proceeds |
| ≤ 50 trades/day, `decide()` ≤ 5 s | — | ≤ 40 orders; measured ~2 ms per call on the preview data, ~75 ms with 1,000 tickers |
| No blow-up (> 50 % drawdown) | admission check | crash brake + governor (flat at −20 %); worst measured drawdown 17.1 % |
| No lookahead / external data | code review | only `market_state` bars are read; no files, network, clock, randomness or LLM |

## Data handling

- Works with whatever tickers it is given: the 21-ticker preview set, the
  42-ticker set the published `live_runner.py` fetches, or the full 1,000-name
  `universe.json`. Leveraged ETFs are never ranked, only used as the booster.
- Each ticker's series is read backwards from the newest bar and stops at the
  first malformed bar (missing, non-finite or non-positive close, out-of-order
  date), so a bad old bar shortens history instead of poisoning it.
- The session date is the last-bar date most tickers agree on; a ticker missing
  that bar (or carrying a glitched date) is neither bought nor sold that session.
- Holdings missing from the feed are valued at `last_prices` (else average cost)
  and left alone. Any unexpected input returns no orders instead of raising.

## Measured numbers

All numbers below were produced by the commands shown, on the bars that ship
with the template (`sample_regimes.json.gz`: 21 tickers, 2019-01-15 → 2021-10-14
once the three public windows are stitched). They are **not** forecasts of the
live result.

### `python preview.py`

```
=== builderr local admission preview: agent.py ===
Running your bot across 3 real, public sample windows...

  window                   Ret   MaxDD  Sharpe  Calmar  Trades
  calm_uptrend          18.68%   4.38%    3.96   21.96      64
  moderate_selloff      -6.80%  10.61%   -2.85   -4.11      28
  vol_spike_snapback   -13.47%  16.89%   -3.56   -3.07      31

Safety bar (exactly what admission gates on):
  [PASS] runs clean, no errors
  [PASS] no leverage breach   (peak gross 1.31x  <= 1.5x)
  [PASS] no concentration breach (peak 26%; longest run >= 30% was 0d, limit is 5d)
  [PASS] no blow-up           (worst drawdown 16.9%  < 50%)

VERDICT: ✓ You clear admission's safety bar — ready for organizer source review.
```

### `python research/window_study.py` (8 sessions, the Round 2 late-entry window)

A fresh $100,000 account is started on **every** session with ≥ 70 sessions of
history and run for 8 sessions through `research/sim.py`, a replica of the
template's `live_runner.run_bot` fill model (decide on bars before the session,
fill at the open, sells first, 5/10 bps slippage, buys clipped at cash / 30 % /
1.5x) that also records gross, concentration and drawdown at every close.

```
=== 8-session windows, agent.py ===
start dates: 617 (2019-04-26 .. 2021-10-05), every session, fresh $100k each
return  mean +0.85%  sd 4.14%
        p05 -6.61%  p10 -4.29%  p25 -1.47%  p50 +1.06%  p75 +3.57%  p90 +5.61%  p95 +7.10%
P(ret > 3.0%)  empirical  30.3%   de-meaned (zero drift)  22.0%   normal(0, sd)  23.5%   [QQQ buy&hold:  24.8%]
P(ret > 4.5%)  empirical  16.5%   de-meaned (zero drift)  11.5%   normal(0, sd)  13.9%   [QQQ buy&hold:  11.3%]
P(ret < -7.29% benchmark)  3.9%
window max drawdown  p50 2.51%  p95 9.25%  worst 17.01%
QQQ same windows     mean +0.86%  sd 3.68%
limits over all windows: peak beta-gross 1.33x (cap 1.50)  peak single name 26.2% (cap 30%)  longest run >=30%: 0d  errors 0  engine-clipped buys 4  windows with DD>=50%: 0
  QQQ < 50d mean or vol20 >= 30% at entry (like 13-Nov-2026): n=183  mean +1.13%  sd 3.37%  P(>3.0%) 31.1%  P(>4.5%) 13.7%  DD p95 6.16% worst 10.10%  | QQQ mean +1.74% sd 4.68% P(>3.0%) 41.0%  P(>4.5%) 21.3%
  all other entry days: n=434  mean +0.74%  sd 4.43%  P(>3.0%) 30.0%  P(>4.5%) 17.7%  DD p95 9.83% worst 17.01%  | QQQ mean +0.49% sd 3.10% P(>3.0%) 18.0%  P(>4.5%) 7.1%
continuous run 2019-04-26 .. 2021-10-14 (624 sessions): return +56.54%  max DD 17.09%  peak gross 1.31x  peak name 25.9%  longest run >=30%: 0d  avg gross 0.96x  trades 674  errors 0
caveat: 2019-2021 sample (bull market + COVID crash), 21 tickers, overlapping windows (~77 independent); the live pool (42 tickers) differs.
```

If the entry is loaded late and only gets 6 sessions
(`python research/window_study.py --sessions 6`):

```
P(ret > 3.0%)  empirical  22.6%   de-meaned (zero drift)  17.1%   normal(0, sd)  20.2%   [QQQ buy&hold:  17.1%]
P(ret > 4.5%)  empirical  11.8%   de-meaned (zero drift)   8.9%   normal(0, sd)  10.6%   [QQQ buy&hold:   8.2%]
window max drawdown  p50 2.02%  p95 7.27%  worst 17.01%
```

### ±20 % parameter sensitivity (`stride 2`, every other start date)

Each row reruns the 8-session study with the named parameters changed by about ±20 % (`stressSD`/`stressP3`: entries in the stressed regime; `gross`/`name`: peaks over all windows; last column: continuous run return / max drawdown). Produced by passing the overrides to `research/window_study.py --stride 2 --set NAME=value`.

```
variant (stride 2)          mean     sd   P>3%  P>4.5%   DDp95   DDmax stressSD stressP3  gross   name    full ret/DD
final                     +0.89%  4.21%  31.1%   17.2%   8.63%  17.01%    3.43%    32.6%  1.32x  26.1%  +56.5%/17.1%
vol targets -20%          +0.85%  4.09%  28.8%   15.5%   8.03%  17.01%    3.06%    27.2%  1.32x  26.1%  +59.2%/16.0%
vol targets +20%          +0.90%  4.23%  31.1%   17.5%   8.63%  17.01%    3.54%    32.6%  1.32x  26.1%  +56.8%/17.5%
momentum windows -20%     +0.87%  4.28%  30.4%   18.4%   8.50%  17.01%    3.47%    32.6%  1.32x  26.8%  +86.5%/16.4%
momentum windows +20%     +0.91%  4.18%  29.4%   19.1%   7.72%  17.11%    3.37%    28.3%  1.32x  27.5% +100.1%/17.3%
vol floor 0.72            +0.84%  4.14%  31.1%   15.5%   8.46%  17.01%    3.29%    32.6%  1.33x  26.1%  +70.1%/16.9%
vol floor 1.08            +0.89%  4.22%  31.1%   15.9%   8.65%  16.86%    3.18%    31.5%  1.33x  27.6%  +74.6%/16.8%
crash brake -20%          +0.89%  4.07%  29.8%   17.2%   8.03%  17.01%    3.21%    28.3%  1.32x  26.1%  +81.0%/16.0%
crash brake +20%          +0.75%  4.37%  30.4%   17.2%   8.91%  17.01%    3.80%    30.4%  1.32x  26.1%  +62.3%/20.1%
drawdown gov -20%         +0.92%  4.17%  31.1%   17.2%   8.63%  16.87%    3.43%    32.6%  1.32x  26.1%  +62.8%/19.5%
drawdown gov +20%         +0.89%  4.21%  31.1%   17.2%   8.63%  17.01%    3.43%    32.6%  1.32x  26.1%  +52.6%/19.2%
booster 0.12              +0.90%  4.10%  30.7%   16.8%   8.35%  16.50%    3.41%    32.6%  1.26x  26.1%  +62.2%/17.4%
booster 0.18              +0.88%  4.30%  32.0%   18.1%   8.85%  17.53%    3.45%    32.6%  1.38x  26.1%  +80.2%/17.4%
top 5 names               +0.71%  3.98%  30.4%   15.9%   8.21%  16.37%    3.32%    33.7%  1.32x  20.6%  +62.0%/16.8%
```

P(> 3 %) stays between 28.8 % and 32.0 %, the worst window drawdown between 16.4 % and 17.5 %, peak beta-gross ≤ 1.38x and peak single name ≤ 27.6 %: no parameter sits on a cliff. The continuous-run returns swing widely (+53 % to +100 %) — path dependence over 2½ years, irrelevant to an 8-session score.

### How to read these numbers

- **Limits:** across 617 overlapping 8-session windows and one 624-session
  continuous run, the agent never exceeded 1.33x beta-gross (cap 1.5x) or 26.2 %
  in one name (cap 30 %), never held any name ≥ 30 % at a close, never errored,
  and its worst drawdown was 17.1 % (blow-up line: 50 %). Observed breach
  frequency: 0. That is evidence of margin, not a proof; the margins are built
  into the targets (24 % / 1.27x) and the forced trims (27 % / 1.35x).
- **Return odds:** the empirical P(> 3 %) of 30 % is flattered by the 2019–2021
  bull market; with the drift removed it is about 22 %. The sample's names are
  tame, so the 97 % invested cap — not the volatility target — usually binds
  (realised 8-session sd 4.1 %). The live pool includes much more volatile
  single names (AMD, MU, MRVL, PLTR, TSLA, …), so the 2.5 %/day target is more
  likely to bind live. **Model estimate, not a measurement:** with zero drift and
  8 sessions at 2.5 %/day (sd 7.1 %), P(> 3 %) ≈ 34 %, P(> 4.5 %) ≈ 26 %,
  P(< −7.29 %) ≈ 15 %; at 3.0 %/day, 36 % / 30 % / 20 %.
- **It is a high-variance choice.** It can easily finish below the benchmark or
  in the bottom half; the brakes only stop it from blowing up.
- Overlapping windows are strongly autocorrelated (~77 independent 8-session
  windows); the sample has 21 tickers and one crash. Treat every figure as
  indicative.

## Files

| Path | What |
|---|---|
| `agent.py` | the submitted agent (`decide`) |
| `preview.py`, `selfcheck.py`, `sample_regimes.json.gz`, `universe.json`, `baseline.py`, `AGENT_BRIEF.md`, `LICENSE` | unmodified from the template (builderr-ai/builderr-trading-template @ 447529f) |
| `research/sim.py` | engine replica with limit telemetry (fill model of the template's `live_runner.run_bot`) |
| `research/window_study.py` | forward-window study behind the numbers above |
| `tests/test_agent.py` | unit tests: contract, garbage input, caps, drift trims, stale/glitched bars, crash brake, governor, runtime, imports |

## License

The template files listed above are © 2026 builderr under the MIT License
(`LICENSE`), kept unchanged. `agent.py`, `research/` and `tests/` are © 2026 Antón
González, all rights reserved; Builderr may run, reproduce and audit them for the
challenge and share them under the published prize terms. This is a paper-trading
competition entry, not investment advice.
