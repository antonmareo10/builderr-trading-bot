"""8-session forward-window study on the template's public sample bars.

Question it answers: if this agent were admitted on day D and scored over the
next N sessions (Round 2 late entries get ~8), what does the distribution of its
return look like, how deep do drawdowns get, and does it ever touch a limit?

For EVERY possible start date (with >= 70 sessions of history before it) the
agent starts a fresh $100,000 account (fresh module state) and is run through
the engine replica in ``sim.py`` for N sessions. QQQ buy-and-hold over the same
windows is reported as market context. Then one continuous run over the whole
stitched sample checks the limits over ~620 consecutive sessions.

    python -I research/window_study.py                 # agent.py, N=8
    python -I research/window_study.py --sessions 6
    python -I research/window_study.py --set VOL_TARGET_OTHER=0.02 --set TOP_K=5

Caveat printed with the results: the sample is 2019-2021 (bull market + COVID
crash), 21 tickers, and consecutive windows overlap (~N-fold autocorrelation).
"""
from __future__ import annotations

import argparse
import importlib.util
import math
import sys
from pathlib import Path
from statistics import NormalDist

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim  # noqa: E402

THRESHOLDS = (0.03, 0.045)    # ~3rd place / ~1st place on the 13-Nov-2026 board
BENCHMARK_RET = -0.0729       # Arnav's Round 2 forward return on the 13-Nov-2026 board


def load_module(path: Path, overrides: dict[str, float]):
    spec = importlib.util.spec_from_file_location(f"agent_study_{id(overrides)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for k, v in overrides.items():
        if not hasattr(mod, k):
            raise SystemExit(f"unknown parameter {k}")
        setattr(mod, k, type(getattr(mod, k))(v))
    return mod


def pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return float("nan")
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def mean(xs):
    return sum(xs) / len(xs)


def sd(xs):
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _stressed(closes: list[float]) -> bool:
    """Start-of-window regime like 13-Nov-2026: QQQ below its 50-day mean, or its
    20-day realised vol >= 30% (uses only bars before the window)."""
    if len(closes) < 51:
        return False
    w = closes[-21:]
    r = [w[i] / w[i - 1] - 1 for i in range(1, len(w))]
    vol20 = sd(r) * math.sqrt(252)
    return closes[-1] < sum(closes[-50:]) / 50 or vol20 >= 0.30


def _subset(rets: list[float], mdds: list[float], qqq: list[float]) -> dict:
    if len(rets) < 3:
        return {"n": len(rets)}
    return {"n": len(rets), "mean": mean(rets), "sd": sd(rets),
            "p_above": {t: sum(x > t for x in rets) / len(rets) for t in THRESHOLDS},
            "mdd_p95": pct(mdds, 0.95), "mdd_max": max(mdds),
            "qqq_mean": mean(qqq), "qqq_sd": sd(qqq),
            "qqq_p_above": {t: sum(x > t for x in qqq) / len(qqq) for t in THRESHOLDS}}


def study(agent: Path, sessions: int, overrides: dict[str, float], stride: int = 1, quiet: bool = False) -> dict:
    book = sim.Book(sim.stitched_bars())
    dates = book.dates
    starts = [d for i, d in enumerate(dates) if i >= 70 and i + sessions <= len(dates)][::stride]

    rets, mdds, qqq, stressed = [], [], [], []
    qqq_close = [b["close"] for b in book.bars["QQQ"]]
    qqq_pos = {b["ts"]: i for i, b in enumerate(book.bars["QQQ"])}
    peak_gross = peak_conc = 0.0
    max_streak = errors = clipped = 0
    for d in starts:
        mod = load_module(agent, overrides)
        r = sim.run(mod.decide, book, d, sessions)
        rets.append(r["ret"])
        mdds.append(r["mdd"])
        peak_gross = max(peak_gross, r["peak_gross"])
        peak_conc = max(peak_conc, r["peak_conc"])
        max_streak = max(max_streak, r["max_conc_streak"])
        errors += r["errors"]
        clipped += r["clipped_buys"]
        i = dates.index(d)
        stressed.append(_stressed(qqq_close[:qqq_pos[d]]))
        q0 = book.bar("QQQ", dates[i])["open"]
        q1 = book.bar("QQQ", dates[i + sessions - 1])["close"]
        qqq.append(q1 / q0 - 1)

    full_mod = load_module(agent, overrides)
    full = sim.run(full_mod.decide, book, starts[0])

    m, s = mean(rets), sd(rets)
    demeaned = [x - m for x in rets]
    out = {
        "n": len(starts), "first": starts[0], "last": starts[-1],
        "mean": m, "sd": s,
        "p": {q: pct(rets, q) for q in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)},
        "p_above": {t: sum(x > t for x in rets) / len(rets) for t in THRESHOLDS},
        "p_above_demeaned": {t: sum(x > t for x in demeaned) / len(rets) for t in THRESHOLDS},
        "p_above_normal0": {t: 1 - NormalDist(0, s).cdf(t) for t in THRESHOLDS},
        "p_below_bench": sum(x < BENCHMARK_RET for x in rets) / len(rets),
        "mdd_p50": pct(mdds, 0.5), "mdd_p95": pct(mdds, 0.95), "mdd_max": max(mdds),
        "peak_gross": peak_gross, "peak_conc": peak_conc, "max_streak": max_streak,
        "errors": errors, "clipped": clipped,
        "breach_windows": sum(1 for x in mdds if x >= 0.5),
        "qqq_mean": mean(qqq), "qqq_sd": sd(qqq),
        "qqq_p_above": {t: sum(x > t for x in qqq) / len(qqq) for t in THRESHOLDS},
        "full": full,
        "stressed": _subset([r for r, k in zip(rets, stressed) if k], [m_ for m_, k in zip(mdds, stressed) if k],
                            [q for q, k in zip(qqq, stressed) if k]),
        "calm": _subset([r for r, k in zip(rets, stressed) if not k], [m_ for m_, k in zip(mdds, stressed) if not k],
                        [q for q, k in zip(qqq, stressed) if not k]),
    }
    if not quiet:
        report(out, sessions, overrides)
    return out


def report(o: dict, sessions: int, overrides: dict[str, float]) -> None:
    f = lambda x: f"{x * 100:+.2f}%"  # noqa: E731
    print(f"=== {sessions}-session windows, agent.py{' ' + str(overrides) if overrides else ''} ===")
    print(f"start dates: {o['n']} ({o['first']} .. {o['last']}), every session, fresh $100k each")
    print(f"return  mean {f(o['mean'])}  sd {o['sd'] * 100:.2f}%")
    print("        " + "  ".join(f"p{int(q * 100):02d} {f(v)}" for q, v in o["p"].items()))
    for t in THRESHOLDS:
        print(f"P(ret > {t * 100:.1f}%)  empirical {o['p_above'][t] * 100:5.1f}%   "
              f"de-meaned (zero drift) {o['p_above_demeaned'][t] * 100:5.1f}%   "
              f"normal(0, sd) {o['p_above_normal0'][t] * 100:5.1f}%   "
              f"[QQQ buy&hold: {o['qqq_p_above'][t] * 100:5.1f}%]")
    print(f"P(ret < {BENCHMARK_RET * 100:.2f}% benchmark)  {o['p_below_bench'] * 100:.1f}%")
    print(f"window max drawdown  p50 {o['mdd_p50'] * 100:.2f}%  p95 {o['mdd_p95'] * 100:.2f}%  "
          f"worst {o['mdd_max'] * 100:.2f}%")
    print(f"QQQ same windows     mean {f(o['qqq_mean'])}  sd {o['qqq_sd'] * 100:.2f}%")
    print(f"limits over all windows: peak beta-gross {o['peak_gross']:.2f}x (cap 1.50)  "
          f"peak single name {o['peak_conc'] * 100:.1f}% (cap 30%)  "
          f"longest run >=30%: {o['max_streak']}d  errors {o['errors']}  "
          f"engine-clipped buys {o['clipped']}  windows with DD>=50%: {o['breach_windows']}")
    for key, label in (("stressed", "QQQ < 50d mean or vol20 >= 30% at entry (like 13-Nov-2026)"),
                       ("calm", "all other entry days")):
        sub = o[key]
        if sub["n"] < 3:
            continue
        print(f"  {label}: n={sub['n']}  mean {f(sub['mean'])}  sd {sub['sd'] * 100:.2f}%  "
              + "  ".join(f"P(>{t * 100:.1f}%) {sub['p_above'][t] * 100:.1f}%" for t in THRESHOLDS)
              + f"  DD p95 {sub['mdd_p95'] * 100:.2f}% worst {sub['mdd_max'] * 100:.2f}%"
              + f"  | QQQ mean {f(sub['qqq_mean'])} sd {sub['qqq_sd'] * 100:.2f}% "
              + "  ".join(f"P(>{t * 100:.1f}%) {sub['qqq_p_above'][t] * 100:.1f}%" for t in THRESHOLDS))
    fr = o["full"]
    print(f"continuous run {fr['start']} .. {fr['end']} ({len(fr['curve'])} sessions): "
          f"return {f(fr['ret'])}  max DD {fr['mdd'] * 100:.2f}%  peak gross {fr['peak_gross']:.2f}x  "
          f"peak name {fr['peak_conc'] * 100:.1f}%  longest run >=30%: {fr['max_conc_streak']}d  "
          f"avg gross {fr['avg_gross']:.2f}x  trades {fr['trades']}  errors {fr['errors']}")
    print("caveat: 2019-2021 sample (bull market + COVID crash), 21 tickers, overlapping windows "
          f"(~{o['n'] // sessions} independent); the live pool (42 tickers) differs.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default=str(sim.ROOT / "agent.py"))
    ap.add_argument("--sessions", type=int, default=8)
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--set", action="append", default=[], help="PARAM=value override")
    a = ap.parse_args()
    overrides = {}
    for kv in a.set:
        k, v = kv.split("=", 1)
        overrides[k.strip()] = float(v)
    study(Path(a.agent), a.sessions, overrides, a.stride)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
