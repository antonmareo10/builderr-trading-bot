"""High-Beta Momentum Sprint -- builderr Trading Agents, Round 2 entry.

Why this design (stated plainly)
--------------------------------
Round 2 ranks entries by % return measured from each entry's OWN first scored
session, and a late entry only gets the last ~8 sessions before the round closes
on 25 Nov 2026. Over 8 sessions a calm, low-volatility book almost never moves
far enough to reach the podium, so this agent deliberately runs a HIGH-VARIANCE
book. It is boxed in by hard rules so that it cannot breach a competition limit
or blow up:

1. Select   - rank every plain (1x) ticker we are given by a momentum blend
              (3-month return, 1-month return, gap above the 50-day mean); keep
              names with a positive score that are at least about as volatile
              as the market index. Hold the top 4, equal weight.
2. Size     - scale the basket so its estimated daily volatility is ~3.0% in a
              calm market up-trend and ~2.5% otherwise; never more than 97%
              invested and never more than 24% in one name.
3. Booster  - only in a calm, confirmed up-trend, move 15% of the book into the
              3x ETF (SOXL or TQQQ) whose underlying is strongest. Beta-adjusted
              gross then tops out near 1.27x (cap: 1.5x).
4. Brakes   - market crash brake (5-day index drop or 10-day index volatility)
              and an account drawdown governor: half size at -12% from the peak,
              flat at -20% with a 5-session cooldown.
5. Guards   - drift trims at 27% per name and 1.35x beta-gross, sells before
              buys, whole shares, cash buffer, stale/missing bars skipped.

Contract: decide(market_state, portfolio_state, cash) -> list of orders.
Only the bars passed in market_state are used: no files, no network, no LLM,
no randomness, no clock. Standard library only. Same inputs -> same orders.
"""
from __future__ import annotations

import math
from typing import Any

# --- competition constants (same table the engine uses) ---------------------
BETA_MULTIPLE = {
    "TQQQ": 3.0, "SOXL": 3.0, "UPRO": 3.0, "SPXL": 3.0, "TNA": 3.0, "FAS": 3.0,
    "TECL": 3.0, "LABU": 3.0, "CURE": 3.0, "DRN": 3.0, "UDOW": 3.0, "NAIL": 3.0,
    "QLD": 2.0, "SSO": 2.0, "DDM": 2.0, "ROM": 2.0, "UWM": 2.0, "AGQ": 2.0,
}

# --- strategy parameters ------------------------------------------------------
INDEX_TICKERS = ("QQQ", "SPY")              # market reference, first available wins
BOOSTERS = (("SOXL", ("SMH", "SOXX")),      # 3x ETF -> underlying(s) used to judge it
            ("TQQQ", ("QQQ",)))

MOM_LONG = 63            # ~3 months
MOM_SHORT = 21           # ~1 month
SMA_FAST = 20
SMA_SLOW = 50
VOL_LOOKBACK = 20        # sessions used for basket volatility
VOL_FLOOR_LOOKBACK = 60  # sessions used for the "at least as volatile as the index" filter
MIN_HISTORY = MOM_LONG + 2
MAX_BARS_USED = 130      # only the newest bars matter; keeps decide() fast on long histories

TOP_K = 4
KEEP_RANK = TOP_K + 1    # hysteresis: a holding survives while it ranks in the top 5
NAME_CAP = 0.24          # target weight per name (rule: < 30%)
MAX_INVESTED = 0.97      # always keep >= 3% cash for open gaps and slippage
BOOSTER_WEIGHT = 0.15    # 3x sleeve -> 0.45 of beta-gross
VOL_TARGET_CALM_UP = 0.030   # daily
VOL_TARGET_OTHER = 0.025     # daily
VOL_FLOOR_RATIO = 0.90
CALM_INDEX_VOL = 0.30        # annualised 20-day index vol below which the market is "calm"

CRASH_R5 = -0.07         # index 5-session return at/below this -> flat
CRASH_VOL10 = 0.60       # annualised 10-session index vol at/above this -> flat

DD_HALF = 0.12           # account drawdown from peak -> half size
DD_FLAT = 0.20           # account drawdown from peak -> flat
FLAT_COOLDOWN = 5        # sessions to stay flat after DD_FLAT, then the peak resets

TRIM_AT = 0.27           # force a trim if any single name drifts above this weight
BETA_GROSS_TRIM = 1.35   # force a trim if beta-adjusted gross drifts above this
MIN_TRADE = 0.02         # ignore rebalances smaller than 2% of equity
BUY_PRICE_PAD = 1.003    # size buys for slippage / small open gaps
MAX_ORDERS = 40          # engine allows 50 trades/day

# --- per-run state (the engine replays sessions in order inside one process) -
_state: dict[str, Any] = {"last_date": None, "peak": None, "cooldown": 0}


# ----------------------------------------------------------------------------
# data helpers
# ----------------------------------------------------------------------------
def _beta(ticker: str) -> float:
    return BETA_MULTIPLE.get(ticker, 1.0)


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _series(bars: Any) -> tuple[list[str], list[float]]:
    """Dates and closes of the usable tail of a bar list.

    Walks backwards from the newest bar and stops at the first malformed bar
    (missing/non-finite/non-positive close), so a bad old bar only shortens the
    history instead of poisoning it. Out-of-order or duplicate dates also stop
    the walk.
    """
    if not isinstance(bars, list) or not bars:
        return [], []
    dates: list[str] = []
    closes: list[float] = []
    prev_date = None
    for bar in reversed(bars[-MAX_BARS_USED:]):
        if not isinstance(bar, dict):
            break
        c = _num(bar.get("close"))
        d = str(bar.get("ts", ""))[:10]
        if c is None or c <= 0 or not d:
            break
        if prev_date is not None and d >= prev_date:
            break
        dates.append(d)
        closes.append(c)
        prev_date = d
    dates.reverse()
    closes.reverse()
    return dates, closes


def _sma(v: list[float], n: int) -> float | None:
    return sum(v[-n:]) / n if len(v) >= n else None


def _ret(v: list[float], n: int) -> float | None:
    return v[-1] / v[-1 - n] - 1.0 if len(v) > n else None


def _daily_returns(v: list[float], n: int) -> list[float]:
    w = v[-(n + 1):]
    return [w[i] / w[i - 1] - 1.0 for i in range(1, len(w))]


def _stdev(x: list[float]) -> float | None:
    if len(x) < 5:
        return None
    m = sum(x) / len(x)
    return math.sqrt(sum((r - m) ** 2 for r in x) / (len(x) - 1))


def _ann_vol(v: list[float], n: int) -> float | None:
    sd = _stdev(_daily_returns(v, n)) if len(v) > n else None
    return sd * math.sqrt(252.0) if sd is not None else None


def _momentum_score(c: list[float]) -> float | None:
    """0.5 x 3-month return + 0.3 x 1-month return + 0.2 x gap above the 50-day mean."""
    r_long, r_short, sma_slow = _ret(c, MOM_LONG), _ret(c, MOM_SHORT), _sma(c, SMA_SLOW)
    if r_long is None or r_short is None or not sma_slow:
        return None
    return 0.5 * r_long + 0.3 * r_short + 0.2 * (c[-1] / sma_slow - 1.0)


def _in_uptrend(c: list[float]) -> bool:
    fast, slow = _sma(c, SMA_FAST), _sma(c, SMA_SLOW)
    return fast is not None and slow is not None and c[-1] > fast and c[-1] > slow



# ----------------------------------------------------------------------------
# portfolio helpers
# ----------------------------------------------------------------------------
def _holdings(portfolio_state: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in portfolio_state.get("positions") or []:
        if not isinstance(p, dict):
            continue
        t = str(p.get("ticker", "")).strip().upper()
        q = _num(p.get("quantity"))
        if t and q is not None and q > 0:
            out[t] = out.get(t, 0.0) + q
    return out


def _price_map(series: dict[str, tuple[list[str], list[float]]], portfolio_state: dict,
               holdings: dict[str, float]) -> dict[str, float]:
    prices = {t: c[-1] for t, (_, c) in series.items() if c}
    last = portfolio_state.get("last_prices") or {}
    for t in holdings:
        if t not in prices:
            p = _num(last.get(t)) if isinstance(last, dict) else None
            if p is None or p <= 0:
                p = next((_num(x.get("avg_cost")) for x in portfolio_state.get("positions") or []
                          if isinstance(x, dict) and str(x.get("ticker", "")).upper() == t), None)
            if p is not None and p > 0:
                prices[t] = p
    return prices


# ----------------------------------------------------------------------------
# target construction
# ----------------------------------------------------------------------------
def _index(series: dict[str, tuple[list[str], list[float]]]) -> list[float] | None:
    for t in INDEX_TICKERS:
        c = series.get(t, ([], []))[1]
        if len(c) >= SMA_SLOW + 1:
            return c
    return None


def _basket_vol(names: list[str], series: dict[str, tuple[list[str], list[float]]]) -> float | None:
    """Daily stdev of the equal-weight basket over the last VOL_LOOKBACK sessions,
    using only dates every name has (missing bars never get forward-filled)."""
    per_name = []
    for t in names:
        d, c = series[t]
        d, c = d[-(VOL_LOOKBACK + 1):], c[-(VOL_LOOKBACK + 1):]
        per_name.append({d[i]: c[i] / c[i - 1] - 1.0 for i in range(1, len(d))})
    common = set(per_name[0])
    for m in per_name[1:]:
        common &= set(m)
    if len(common) >= 10:
        basket = [sum(m[d] for m in per_name) / len(per_name) for d in sorted(common)]
        return _stdev(basket)
    # Fallback (not enough aligned dates): assume perfect correlation, i.e. the
    # average single-name volatility -- the conservative (largest) estimate.
    vols = [_stdev(list(m.values())) for m in per_name]
    vols = [v for v in vols if v is not None]
    return sum(vols) / len(vols) if vols else None


def target_weights(fresh: dict[str, tuple[list[str], list[float]]], held: dict[str, float],
                   equity: float) -> dict[str, float]:
    """{ticker: target weight of equity} from the up-to-date series only.

    Depends on the bars, on current holdings (rank hysteresis) and on the
    account drawdown state -- nothing else."""
    if not fresh:
        return {}
    idx = _index(fresh)
    idx_vol20 = _ann_vol(idx, 20) if idx else None
    idx_vol10 = _ann_vol(idx, 10) if idx else None
    idx_vol60 = _ann_vol(idx, VOL_FLOOR_LOOKBACK) if idx else None
    idx_r5 = _ret(idx, 5) if idx else None

    # Market crash brake -> flat.
    if (idx_r5 is not None and idx_r5 <= CRASH_R5) or (idx_vol10 is not None and idx_vol10 >= CRASH_VOL10):
        return {}

    calm_up = bool(
        idx and idx_vol20 is not None and idx_vol20 < CALM_INDEX_VOL
        and _in_uptrend(idx) and (_sma(idx, SMA_FAST) or 0.0) > (_sma(idx, SMA_SLOW) or 0.0)
    )

    # Rank plain (1x) names.
    scored: list[tuple[float, str]] = []
    for t, (_, c) in fresh.items():
        if t in BETA_MULTIPLE or len(c) < MIN_HISTORY:
            continue
        s = _momentum_score(c)
        if s is None or s <= 0:
            continue
        if idx_vol60:
            v60 = _ann_vol(c, VOL_FLOOR_LOOKBACK)
            if v60 is None or v60 < VOL_FLOOR_RATIO * idx_vol60:
                continue
        scored.append((s, t))
    scored.sort(key=lambda x: (-x[0], x[1]))
    ranked = [t for _, t in scored]
    keep = [t for t in ranked[:KEEP_RANK] if t in held][:TOP_K]
    selected = keep + [t for t in ranked if t not in keep][:TOP_K - len(keep)]
    if not selected:
        return {}

    target_vol = VOL_TARGET_CALM_UP if calm_up else VOL_TARGET_OTHER
    sigma = _basket_vol(selected, fresh)
    invested = min(MAX_INVESTED, NAME_CAP * len(selected))
    if sigma and sigma > 0:
        invested = min(invested, target_vol / sigma)

    booster, booster_w = None, 0.0
    if calm_up and sigma and target_vol / sigma > MAX_INVESTED:
        best = None
        for etf, underlyings in BOOSTERS:
            if etf not in fresh:
                continue
            for u in underlyings:
                c = fresh.get(u, ([], []))[1]
                if len(c) < MIN_HISTORY or not _in_uptrend(c):
                    continue
                s = _momentum_score(c)
                if s is not None and s > 0 and (best is None or s > best[0]):
                    best = (s, etf)
        if best:
            booster, booster_w = best[1], BOOSTER_WEIGHT
            invested = min(invested, MAX_INVESTED - booster_w)

    # Account drawdown governor.
    scale = _governor(equity)
    weights = {t: scale * invested / len(selected) for t in selected}
    if booster:
        weights[booster] = weights.get(booster, 0.0) + scale * booster_w
    return {t: w for t, w in weights.items() if w > 1e-4}


def _governor(equity: float) -> float:
    st = _state
    if st["cooldown"] > 0:
        return 0.0
    peak = st["peak"] or equity
    dd = 1.0 - equity / peak if peak > 0 else 0.0
    if dd >= DD_FLAT:
        st["cooldown"] = FLAT_COOLDOWN
        return 0.0
    return 0.5 if dd >= DD_HALF else 1.0


def _advance_session(date: str, equity: float) -> None:
    """Session bookkeeping: once per new bar date (robust to intraday re-calls)."""
    st = _state
    if date != st["last_date"]:
        st["last_date"] = date
        if st["cooldown"] > 0:
            st["cooldown"] -= 1
            if st["cooldown"] == 0:
                st["peak"] = equity  # fresh start after the cooldown
    st["peak"] = equity if st["peak"] is None else max(st["peak"], equity)


# ----------------------------------------------------------------------------
# orders
# ----------------------------------------------------------------------------
def _orders(targets: dict[str, float], held: dict[str, float], prices: dict[str, float],
            tradable: set[str], equity: float, cash: float) -> list[dict]:
    if equity <= 0:
        return []
    value = {t: q * prices.get(t, 0.0) for t, q in held.items()}
    beta_gross = sum(v * _beta(t) for t, v in value.items()) / equity
    force_beta = beta_gross > BETA_GROSS_TRIM
    min_trade = MIN_TRADE * equity
    band = {t: min_trade / 2 if _beta(t) > 1.0 else min_trade for t in set(held) | set(targets)}

    sells: list[dict] = []
    proceeds = 0.0
    for t in sorted(held):
        px = prices.get(t)
        if t not in tradable or not px:
            continue
        cur = value[t]
        want = targets.get(t, 0.0) * equity
        if want <= 0:
            qty = held[t]                      # full exit, whatever the size
        else:
            over = cur - want
            forced = cur / equity > TRIM_AT or (force_beta and _beta(t) > 1.0)
            if over <= 0 or (over < band[t] and not forced):
                continue
            qty = min(float(math.floor(over / px)), held[t])
        if qty > 0:
            sells.append({"ticker": t, "side": "sell", "quantity": int(qty) if qty == int(qty) else qty})
            proceeds += qty * px

    budget = max(cash, 0.0) + proceeds * 0.995
    buys: list[dict] = []
    for t in sorted(targets, key=lambda x: (-targets[x], x)):
        px = prices.get(t)
        if t not in tradable or not px:
            continue
        add = targets[t] * equity - value.get(t, 0.0)
        if add < band[t]:
            continue
        qty = math.floor(min(add, budget) / (px * BUY_PRICE_PAD))
        if qty > 0:
            buys.append({"ticker": t, "side": "buy", "quantity": int(qty)})
            budget -= qty * px * BUY_PRICE_PAD
    return (sells + buys)[:MAX_ORDERS]


def _decide(market_state: dict, portfolio_state: dict, cash: float) -> list[dict]:
    if not isinstance(market_state, dict) or not market_state:
        return []
    portfolio_state = portfolio_state if isinstance(portfolio_state, dict) else {}
    c = _num(portfolio_state.get("cash", cash))
    cash_now = c if c is not None else (_num(cash) or 0.0)

    series = {str(t).strip().upper(): _series(b) for t, b in market_state.items()}
    series = {t: s for t, s in series.items() if s[1]}
    if not series:
        return []
    # The session date is the last bar date most tickers agree on (a lagging or
    # glitched ticker cannot move it); tickers not on that date are not traded.
    counts: dict[str, int] = {}
    for d, _ in series.values():
        counts[d[-1]] = counts.get(d[-1], 0) + 1
    latest = max(counts, key=lambda d: (counts[d], d))
    tradable = {t for t, s in series.items() if s[0][-1] == latest}

    held = _holdings(portfolio_state)
    prices = _price_map(series, portfolio_state, held)
    equity = cash_now + sum(q * prices.get(t, 0.0) for t, q in held.items())
    if equity <= 0:
        return []

    _advance_session(latest, equity)
    targets = target_weights({t: series[t] for t in tradable}, held, equity)
    orders = _orders(targets, held, prices, tradable, equity, cash_now)

    # Map back to the exact ticker spelling the engine sent us.
    spelling = {str(t).strip().upper(): t for t in market_state}
    for o in orders:
        o["ticker"] = spelling.get(o["ticker"], o["ticker"])
    return orders


def decide(market_state: dict, portfolio_state: dict, cash: float) -> list[dict]:
    """Return long-only orders. Never raises: on any unexpected input it does nothing."""
    try:
        return _decide(market_state, portfolio_state, cash)
    except Exception:  # noqa: BLE001 - an exception would only cost us the session
        return []
