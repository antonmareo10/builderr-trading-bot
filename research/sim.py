"""Offline replica of the builderr live scoring loop, with limit telemetry.

The fill model mirrors ``run_bot`` in the template's ``live_runner.py`` (MIT,
builderr-ai/builderr-trading-template @ 447529f):

* on each scored session D the agent sees bars strictly before D;
* orders fill at D's OPEN, sells before buys, 5 bps slippage (10 bps on 2x/3x ETFs);
* buys are clipped to available cash, to 30% of equity per name and to 1.5x
  beta-adjusted gross (the engine's own boundary);
* the book is marked at D's CLOSE.

On top of that it records what the rules care about at every close mark:
beta-adjusted gross, the largest single-name weight, the longest run of
consecutive sessions any name spent at >= 30%, and the drawdown.

Data: only ``sample_regimes.json.gz`` shipped with the template (21 tickers,
2019-01-15 .. 2021-10-14 once the three windows are stitched together).
Standard library only.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "sample_regimes.json.gz"

BETA_3X = {"TQQQ", "SOXL", "UPRO", "SPXL", "TNA", "FAS", "TECL", "LABU", "CURE", "DRN", "UDOW", "NAIL"}
BETA_2X = {"QLD", "SSO", "DDM", "ROM", "UWM", "AGQ"}
START_CASH = 100_000.0
MAX_NAME_WEIGHT = 0.30
MAX_BETA_GROSS = 1.50
SLIP_EQUITY = 0.0005
SLIP_LEVERAGED = 0.0010


def beta(t: str) -> float:
    return 3.0 if t in BETA_3X else 2.0 if t in BETA_2X else 1.0


def load_regimes() -> dict:
    return json.loads(gzip.open(DATA, "rb").read())


def stitched_bars() -> dict[str, list[dict]]:
    """Union of the three public windows, one bar per (ticker, date).

    Overlapping dates agree to ~1e-4 (rounding of the stored adjusted prices);
    the later window's value is kept. Returns {ticker: [bar dicts, oldest first]}.
    """
    out: dict[str, dict[str, list]] = {}
    for reg in load_regimes().values():
        for t, rows in reg["bars"].items():
            m = out.setdefault(t, {})
            for r in rows:
                m[r[0]] = r
    return {
        t: [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
            for _, r in sorted(m.items())]
        for t, m in out.items()
    }


def load_decide(path: Path):
    """Fresh module per call, so module-level state starts clean (like a new entry)."""
    spec = importlib.util.spec_from_file_location(f"agent_{id(path)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.decide


class Book:
    """Indexes bars by date once so a long replay stays fast."""

    def __init__(self, bars: dict[str, list[dict]]):
        self.bars = bars
        self.dates = sorted({b["ts"] for rows in bars.values() for b in rows})
        self.pos = {t: {b["ts"]: i for i, b in enumerate(rows)} for t, rows in bars.items()}

    def bar(self, t: str, d: str):
        i = self.pos[t].get(d)
        return None if i is None else self.bars[t][i]

    def history_before(self, d: str) -> dict[str, list[dict]]:
        out = {}
        for t, rows in self.bars.items():
            # bars are sorted; find the count of bars with ts < d
            lo, hi = 0, len(rows)
            while lo < hi:
                mid = (lo + hi) // 2
                if rows[mid]["ts"] < d:
                    lo = mid + 1
                else:
                    hi = mid
            out[t] = rows[:lo]
        return out


def run(decide, book: Book, start: str, n_sessions: int | None = None) -> dict:
    eval_dates = [d for d in book.dates if d >= start]
    if n_sessions is not None:
        eval_dates = eval_dates[:n_sessions]
    cash = START_CASH
    positions: dict[str, float] = {}
    avg_cost: dict[str, float] = {}
    curve: list[float] = []
    trades = 0
    peak_gross = peak_conc = 0.0
    streak: dict[str, int] = {}
    max_streak = 0
    clipped = 0           # buys the engine had to shrink (cash / 30% / 1.5x boundary)
    errors = 0
    exposure: list[float] = []

    for date in eval_dates:
        open_px = {t: b["open"] for t in book.bars if (b := book.bar(t, date))}
        close_px = {t: b["close"] for t in book.bars if (b := book.bar(t, date))}
        market_state = book.history_before(date)
        prior_close = {t: ms[-1]["close"] for t, ms in market_state.items() if ms}
        portfolio_state = {
            "cash": cash,
            "positions": [{"ticker": t, "quantity": q, "avg_cost": avg_cost.get(t, 0.0)}
                          for t, q in positions.items() if q > 0],
            "last_prices": prior_close,
        }
        try:
            orders = decide(market_state, portfolio_state, cash) or []
        except Exception:  # noqa: BLE001
            errors += 1
            orders = []

        normalized = []
        for o in orders[:100] if isinstance(orders, list) else []:
            try:
                tk = str(o["ticker"]).strip().upper()
                side, qty = o["side"], float(o["quantity"])
            except (KeyError, TypeError, ValueError):
                errors += 1
                continue
            if side not in ("buy", "sell") or not math.isfinite(qty) or qty <= 0 or tk not in open_px:
                errors += 1
                continue
            normalized.append((tk, side, qty))

        for tk, side, qty in sorted(normalized, key=lambda x: 0 if x[1] == "sell" else 1):
            px = open_px[tk]
            slip = SLIP_LEVERAGED if beta(tk) > 1 else SLIP_EQUITY
            if side == "buy":
                fill = px * (1 + slip)
                eq_open = max(cash + sum(positions.get(t, 0.0) * open_px.get(t, 0.0) for t in positions), 1e-9)
                held = positions.get(tk, 0.0)
                conc_room = max(0.0, MAX_NAME_WEIGHT * eq_open - held * fill)
                beta_used = sum(positions.get(t, 0.0) * open_px.get(t, 0.0) * beta(t) for t in positions)
                beta_room = max(0.0, MAX_BETA_GROSS * eq_open - beta_used)
                max_notional = min(cash, conc_room, beta_room / beta(tk))
                want = qty
                qty = min(qty, max_notional / fill if fill > 0 else 0.0)
                if qty < want - 1e-9:
                    clipped += 1
                if qty <= 0:
                    continue
                avg_cost[tk] = (avg_cost.get(tk, 0.0) * held + fill * qty) / (held + qty)
                positions[tk] = held + qty
                cash -= fill * qty
                trades += 1
            else:
                held = positions.get(tk, 0.0)
                qty = min(qty, held)
                if qty <= 0:
                    continue
                cash += px * (1 - slip) * qty
                positions[tk] = held - qty
                trades += 1

        values = {t: positions.get(t, 0.0) * close_px.get(t, 0.0) for t in positions}
        equity = max(cash + sum(values.values()), 1e-9)
        curve.append(equity)
        gross = sum(abs(v) * beta(t) for t, v in values.items()) / equity
        peak_gross = max(peak_gross, gross)
        exposure.append(gross)
        for t in book.bars:
            w = abs(values.get(t, 0.0)) / equity
            peak_conc = max(peak_conc, w)
            streak[t] = streak.get(t, 0) + 1 if w >= MAX_NAME_WEIGHT else 0
            max_streak = max(max_streak, streak[t])

    return {
        "start": eval_dates[0] if eval_dates else None,
        "end": eval_dates[-1] if eval_dates else None,
        "curve": curve,
        "ret": (curve[-1] / START_CASH - 1) if curve else 0.0,
        "mdd": max_drawdown([START_CASH] + curve),
        "trades": trades,
        "peak_gross": peak_gross,
        "peak_conc": peak_conc,
        "max_conc_streak": max_streak,
        "clipped_buys": clipped,
        "errors": errors,
        "avg_gross": sum(exposure) / len(exposure) if exposure else 0.0,
    }


def max_drawdown(curve: list[float]) -> float:
    peak, mdd = -1e18, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd
