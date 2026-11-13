"""Unit tests for agent.py -- standard library only.

    python -I -m unittest discover -s tests -v
"""
from __future__ import annotations

import ast
import importlib.util
import math
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def fresh_agent():
    """A new module instance, so per-run state starts clean."""
    spec = importlib.util.spec_from_file_location("agent_under_test", ROOT / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_bars(returns: list[float], start: float = 100.0, first: date = date(2024, 1, 1)) -> list[dict]:
    out, px, d = [], start, first
    for r in returns:
        px *= 1.0 + r
        out.append({"ts": d.isoformat(), "open": px, "high": px * 1.01, "low": px * 0.99,
                    "close": px, "volume": 1_000_000})
        d += timedelta(days=1)
    return out


def wiggle(drift: float, amp: float, n: int = 140) -> list[float]:
    """Deterministic up/down pattern with a drift: daily stdev ~= amp."""
    return [drift + (amp if i % 2 == 0 else -amp) for i in range(n)]


def bull_market(n: int = 140) -> dict[str, list[dict]]:
    """Calm index up-trend; four high-momentum, high-vol names; boring others."""
    m = {
        "QQQ": make_bars(wiggle(0.0015, 0.006, n)),
        "SPY": make_bars(wiggle(0.0012, 0.005, n)),
        "SMH": make_bars(wiggle(0.0030, 0.010, n)),
        "SOXL": make_bars(wiggle(0.0090, 0.030, n)),
        "TQQQ": make_bars(wiggle(0.0045, 0.018, n)),
        "XLP": make_bars(wiggle(0.0002, 0.003, n)),
        "XLU": make_bars(wiggle(0.0001, 0.003, n)),
    }
    for i, t in enumerate(("NVDA", "AMD", "MU", "PLTR", "AAPL", "MSFT")):
        m[t] = make_bars(wiggle(0.004 - 0.0004 * i, 0.012, n))
    return m


def portfolio(cash: float, holdings: dict[str, float] | None = None, market: dict | None = None) -> dict:
    holdings = holdings or {}
    last = {t: b[-1]["close"] for t, b in (market or {}).items() if b}
    return {"cash": cash,
            "positions": [{"ticker": t, "quantity": q, "avg_cost": last.get(t, 1.0)} for t, q in holdings.items()],
            "last_prices": last}


def weights_after(orders, market, cash, holdings=None):
    """Book weights if every order filled at the last close."""
    holdings = dict(holdings or {})
    px = {t: b[-1]["close"] for t, b in market.items()}
    for o in orders:
        q = float(o["quantity"])
        if o["side"] == "sell":
            holdings[o["ticker"]] -= q
            cash += q * px[o["ticker"]]
        else:
            holdings[o["ticker"]] = holdings.get(o["ticker"], 0.0) + q
            cash -= q * px[o["ticker"]]
    eq = cash + sum(q * px[t] for t, q in holdings.items())
    return {t: q * px[t] / eq for t, q in holdings.items() if q > 0}, cash


class ContractTests(unittest.TestCase):
    def test_orders_are_well_formed(self):
        a, m = fresh_agent(), bull_market()
        orders = a.decide(m, portfolio(100_000.0), 100_000.0)
        self.assertTrue(orders, "bull market should produce buys")
        for o in orders:
            self.assertEqual(set(o), {"ticker", "side", "quantity"})
            self.assertIn(o["side"], ("buy", "sell"))
            self.assertIn(o["ticker"], m)
            self.assertIsInstance(o["quantity"], int)
            self.assertGreater(o["quantity"], 0)

    def test_garbage_inputs_never_raise(self):
        a = fresh_agent()
        bad_bar = {"ts": "2024-01-01", "open": 1, "high": 1, "low": 1, "close": float("nan"), "volume": 0}
        cases = [
            ({}, {}, 0.0),
            (None, None, None),
            ({"SPY": None}, {"cash": "x"}, "y"),
            ({"SPY": [bad_bar]}, {"cash": 1000.0, "positions": [None, {"ticker": None}]}, 1000.0),
            ({"SPY": [{"close": -5}], "QQQ": "nope"}, {"cash": 1e5, "positions": "bad"}, 1e5),
            ({"SPY": make_bars([0.0] * 5)}, {"cash": 1e5, "positions": [], "last_prices": None}, 1e5),
        ]
        for ms, ps, c in cases:
            self.assertIsInstance(a.decide(ms, ps, c), list)
        # the internal path must also be exception-free on these (decide() only masks)
        for ms, ps, c in cases:
            self.assertIsInstance(a._decide(ms, ps, c), list)

    def test_deterministic(self):
        m = bull_market()
        o1 = fresh_agent().decide(m, portfolio(100_000.0), 100_000.0)
        o2 = fresh_agent().decide(m, portfolio(100_000.0), 100_000.0)
        self.assertEqual(o1, o2)

    def test_runtime_on_large_universe(self):
        a = fresh_agent()
        base = bull_market(320)
        m = dict(base)
        for i in range(1000):
            m[f"T{i:04d}"] = make_bars(wiggle(0.001 + (i % 7) * 0.0003, 0.01 + (i % 5) * 0.003, 320))
        t0 = time.perf_counter()
        a.decide(m, portfolio(100_000.0), 100_000.0)
        self.assertLess(time.perf_counter() - t0, 2.0)  # engine limit is 5 s

    def test_source_is_self_contained(self):
        tree = ast.parse((ROOT / "agent.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {n.name.split(".")[0] for n in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("open", "eval", "exec", "__import__"):
                self.fail(f"agent.py calls {node.func.id}()")
        self.assertLessEqual(imported, {"__future__", "math", "typing"})


class LimitTests(unittest.TestCase):
    def test_caps_on_entry(self):
        a, m = fresh_agent(), bull_market()
        orders = a.decide(m, portfolio(100_000.0), 100_000.0)
        w, cash = weights_after(orders, m, 100_000.0)
        self.assertGreaterEqual(cash, 0.0)
        self.assertTrue(all(x <= a.NAME_CAP + 1e-9 for x in w.values()), w)
        beta = sum(x * a.BETA_MULTIPLE.get(t, 1.0) for t, x in w.items())
        self.assertLessEqual(beta, 1.30 + 1e-9, w)
        self.assertIn("SOXL", w, "calm up-trend with strong semis should use the SOXL booster")

    def test_drifted_name_is_trimmed(self):
        a, m = fresh_agent(), bull_market()
        px = m["NVDA"][-1]["close"]
        held = {"NVDA": 29_000 / px}                     # 29% of a $100k book
        orders = a.decide(m, portfolio(71_000.0, held, m), 71_000.0)
        w, _ = weights_after(orders, m, 71_000.0, held)
        self.assertLessEqual(w.get("NVDA", 0.0), a.NAME_CAP + 0.005)

    def test_beta_gross_drift_is_trimmed(self):
        a, m = fresh_agent(), bull_market()
        held = {"SOXL": 45_000 / m["SOXL"][-1]["close"]}  # 1.35x beta from one 3x ETF at 45%
        orders = a.decide(m, portfolio(55_000.0, held, m), 55_000.0)
        w, _ = weights_after(orders, m, 55_000.0, held)
        beta = sum(x * a.BETA_MULTIPLE.get(t, 1.0) for t, x in w.items())
        self.assertLessEqual(beta, 1.30 + 1e-6)
        self.assertLessEqual(w.get("SOXL", 0.0), a.BOOSTER_WEIGHT + 0.005)

    def test_stale_ticker_not_bought(self):
        a, m = fresh_agent(), bull_market()
        m["NVDA"] = m["NVDA"][:-1]                       # missing the newest bar
        orders = a.decide(m, portfolio(100_000.0), 100_000.0)
        self.assertNotIn("NVDA", {o["ticker"] for o in orders})
        self.assertTrue(orders)

    def test_held_ticker_missing_from_feed_is_left_alone(self):
        a, m = fresh_agent(), bull_market()
        held = {"GONE": 100.0}
        ps = portfolio(90_000.0, held, m)
        ps["last_prices"]["GONE"] = 100.0
        orders = a.decide(m, ps, 90_000.0)
        self.assertNotIn("GONE", {o["ticker"] for o in orders})


class RiskTests(unittest.TestCase):
    def test_crash_brake_goes_flat(self):
        a, m = fresh_agent(), bull_market()
        for t in m:                                      # -3% a day for the last 5 sessions
            m[t] = make_bars(wiggle(0.0015, 0.006, 135) + [-0.03] * 5)
        held = {"NVDA": 200.0, "AMD": 200.0}
        orders = a.decide(m, portfolio(50_000.0, held, m), 50_000.0)
        sells = {o["ticker"]: o["quantity"] for o in orders if o["side"] == "sell"}
        self.assertEqual(sells, held)
        self.assertFalse([o for o in orders if o["side"] == "buy"])

    def test_no_eligible_names_means_cash(self):
        a = fresh_agent()
        m = {t: make_bars(wiggle(-0.002, 0.008)) for t in ("QQQ", "SPY", "NVDA", "AMD", "MU", "SMH", "SOXL")}
        self.assertEqual(a.decide(m, portfolio(100_000.0), 100_000.0), [])

    def test_drawdown_governor(self):
        a, m = fresh_agent(), bull_market()
        fresh = {t: a._series(b) for t, b in m.items()}
        a._state.update(peak=100_000.0, cooldown=0)
        full = sum(a.target_weights(fresh, {}, 100_000.0).values())
        half = sum(a.target_weights(fresh, {}, 100_000.0 * (1 - a.DD_HALF - 0.005)).values())
        self.assertGreater(full, 0.9)
        self.assertAlmostEqual(half, full / 2, places=6)
        self.assertEqual(a.target_weights(fresh, {}, 100_000.0 * (1 - a.DD_FLAT - 0.005)), {})
        self.assertEqual(a._state["cooldown"], a.FLAT_COOLDOWN)

    def test_cooldown_then_reentry(self):
        a, m = fresh_agent(), bull_market()
        names = ("NVDA", "AMD", "MU", "PLTR")

        def day(k):
            return {t: b[: len(b) - 10 + k] for t, b in m.items()}

        mm = day(0)
        qty = {t: 24_000.0 / mm[t][-1]["close"] for t in names}
        a.decide(mm, portfolio(4_000.0, qty, mm), 4_000.0)          # peak = ~100k
        mm = day(1)
        eq = 100_000.0 * (1 - a.DD_FLAT - 0.01)
        qty = {t: eq * 0.24 / mm[t][-1]["close"] for t in names}
        orders = a.decide(mm, portfolio(eq * 0.04, qty, mm), eq * 0.04)
        self.assertEqual({o["ticker"] for o in orders}, set(names))  # all sells: flat
        self.assertTrue(all(o["side"] == "sell" for o in orders))
        for k in range(2, 2 + a.FLAT_COOLDOWN - 1):                   # cooldown: stays in cash
            self.assertEqual(a.decide(day(k), portfolio(eq), eq), [], k)
        orders = a.decide(day(1 + a.FLAT_COOLDOWN), portfolio(eq), eq)
        self.assertTrue(any(o["side"] == "buy" for o in orders), "re-enters after the cooldown")
        self.assertEqual(a._state["peak"], eq)                          # peak was reset

    def test_same_day_recall_does_not_advance_cooldown(self):
        a, m = fresh_agent(), bull_market()
        a._state.update(cooldown=2, last_date=m["QQQ"][-1]["ts"], peak=100_000.0)
        for _ in range(5):
            a.decide(m, portfolio(100_000.0), 100_000.0)
        self.assertEqual(a._state["cooldown"], 2)


if __name__ == "__main__":
    unittest.main()
