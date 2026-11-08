"""Backtest the rules on CoinDCX futures history with the same paper broker the live engine uses.

  python -m cdx.backtest --days 90                      BTC + ETH, all rules, 5-min base bars
  python -m cdx.backtest --days 30 --symbols B-BTC_USDT --rules dc

Fills: bar close (entries/exits by rule) or stop/target price, each 0.01% worse; 0.059% fee per side;
funding 0.01% every 8 h (longs pay, shorts receive) since past funding rates are not in the candle feed.
USDT/INR is held at today's rate. Results are estimates; use them to compare rules, then paper trade live.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime, timezone

from . import api, config
from .bars import Aggregator
from .broker import Book, summary
from .rules import RULES

FUNDING_ASSUMED = 0.0001


def load(sym, days, cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"{sym}_5m_{days}d.json"
    if f.exists() and time.time() - f.stat().st_mtime < 6 * 3600:
        return json.loads(f.read_text())
    now = int(time.time())
    bars = api.candles(sym, "5", now - days * 86400, now)
    bars = [b for b in bars if b["ts"] + 300_000 <= now * 1000]  # drop the forming bar
    f.write_text(json.dumps(bars))
    return bars


def run(series: dict[str, list[dict]], rules: list[str], fx: float, specs: dict | None = None, base_min=5,
        slippage=None, funding=FUNDING_ASSUMED):
    specs = specs or {}
    books = {r: Book(r, slippage=config.SLIPPAGE if slippage is None else slippage) for r in rules}
    strat = {(r, s): RULES[r]() for r in rules for s in series}
    aggs = {(r, s): Aggregator(RULES[r].frame, base_min) for r in rules for s in series}
    for s, bars in series.items():
        for b in bars:
            hour_start = b["ts"] % 3_600_000 == 0 and (b["ts"] // 3_600_000) % 24 in (0, 8, 16)
            for r in rules:
                bk = books[r]
                if hour_start:
                    bk.fund(s, funding, b["o"])
                if bk.check(s, b, fx):
                    strat[(r, s)].closed()
                for fb in aggs[(r, s)].add(b):
                    it = strat[(r, s)].on_bar(fb, bk.pos.get(s))
                    if not it:
                        continue
                    ts = fb["ts"] + RULES[r].frame * 60_000
                    if it["act"] == "enter":
                        bk.open(s, it["side"], fb["c"], it["stop"], it["target"], ts, it["why"], fx, specs.get(s))
                    elif it["act"] == "exit":
                        if bk.close(s, fb["c"], ts, it["why"], fx):
                            strat[(r, s)].closed()
                    elif it["act"] == "stop" and s in bk.pos:
                        bk.pos[s]["stop"] = it["stop"]
        for r in rules:
            if s in books[r].pos and bars:
                books[r].close(s, bars[-1]["c"], bars[-1]["ts"], "end of data", fx)
    return books


def report(books, days, out_dir=None):
    print(f"\n{days} days, capital ₹{config.CAPITAL_INR:,.0f} per rule, risk {config.RISK_PCT}% per trade, max {config.MAX_LEVERAGE:g}x\n")
    hdr = f"{'rule':<5}{'trades':>7}{'win%':>6}{'net ₹':>11}{'₹/trade':>9}{'PF':>6}{'max DD ₹':>11}{'fees ₹':>9}{'after tax ₹ (strict / netted)':>32}"
    print(hdr)
    rows = []
    for r, bk in books.items():
        sm = summary(bk.trades)
        rows += bk.trades
        print(f"{r:<5}{sm['trades']:>7}{sm['win_pct']:>5.0f}%{sm['net_inr']:>11,.0f}{sm['avg_inr']:>9,.0f}{sm['pf']:>6.2f}"
              f"{sm['max_dd_inr']:>11,.0f}{sm['fees_inr']:>9,.0f}{sm['after_tax_strict']:>16,.0f} / {sm['after_tax_netted']:,.0f}")
        for sym in sorted({t['sym'] for t in bk.trades}):
            s2 = summary([t for t in bk.trades if t["sym"] == sym])
            print(f"   {sym:<14}{s2['trades']:>4} trades  net ₹{s2['net_inr']:>9,.0f}  win {s2['win_pct']:.0f}%")
    print("\nAfter-tax 'strict' = 30% on every winning trade with no loss set-off (the cautious reading of India's VDA rules).")
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        f = out_dir / "backtest_trades.csv"
        with f.open("w", newline="") as fh:
            if rows:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                for t in rows:
                    w.writerow({**t, "entry_ts": _iso(t["entry_ts"]), "exit_ts": _iso(t["exit_ts"])})
        print(f"Trades written to {f}")


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    p = argparse.ArgumentParser(prog="python -m cdx.backtest")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--symbols", default=",".join(config.SYMBOLS))
    p.add_argument("--rules", default=",".join(config.RULES))
    a = p.parse_args()
    syms = [s for s in a.symbols.split(",") if s]
    fx = api.usdt_inr()
    specs = {s: api.instrument(s) for s in syms}
    series = {s: load(s, a.days, config.DATA / "cache") for s in syms}
    for s, b in series.items():
        print(f"{s}: {len(b)} bars from {_iso(b[0]['ts'])} to {_iso(b[-1]['ts'])} UTC")
    books = run(series, [r for r in a.rules.split(",") if r], fx, specs)
    report(books, a.days, config.DATA / "reports")


if __name__ == "__main__":
    main()
