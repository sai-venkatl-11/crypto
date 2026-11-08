"""Command line.

  python -m cdx.app paper              run the 24/7 paper engine (Ctrl+C to stop; restart resumes open positions)
  python -m cdx.app paper --no-alerts  same, without Slack
  python -m cdx.app status             open paper positions and unrealized P&L (from data/state.json)
  python -m cdx.app report             closed paper trades: totals per rule, fees, and after-tax views
  python -m cdx.app backtest --days 90 (same as python -m cdx.backtest)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import config, ledger
from .broker import summary


def cmd_paper(a):
    from .engine import Engine
    Engine(notify=not a.no_alerts).run()


def cmd_status(_):
    f = config.DATA / "state.json"
    if not f.exists():
        print("No state yet: start the engine with  python -m cdx.app paper")
        return
    st = json.loads(f.read_text())
    print(f"Updated {st['updated']} · USDT/INR {st['fx']:.2f}{' · KILL SWITCH ON' if st.get('killed') else ''}")
    for s, p in st["prices"].items():
        print(f"  {s:<12} last {p['last']:,.2f}  funding {100 * p['funding']:.4f}% / 8h")
    for r, pos in st["positions"].items():
        u = st["unrealized_inr"].get(r, 0)
        if not pos:
            print(f"{r}: flat")
        for s, p in pos.items():
            print(f"{r}: {p['side']} {p['qty']} {s} from {p['entry']:.2f} · stop {p['stop']:.2f} · unrealized ₹{u:,.0f}")


def cmd_report(_):
    T = ledger.all_trades()
    if not T:
        print("No closed paper trades yet.")
        return
    for r in sorted({t["rule"] for t in T}):
        sm = summary([t for t in T if t["rule"] == r])
        print(f"{r}: {sm['trades']} trades · win {sm['win_pct']:.0f}% · net ₹{sm['net_inr']:,.0f} · fees ₹{sm['fees_inr']:,.0f}"
              f" · max DD ₹{sm['max_dd_inr']:,.0f} · after tax ₹{sm['after_tax_strict']:,.0f} (strict) / ₹{sm['after_tax_netted']:,.0f} (netted)")
    print("\nLast 10 trades:")
    for t in T[-10:]:
        when = datetime.fromtimestamp(t["exit_ts"] / 1000, timezone.utc).strftime("%d %b %H:%M UTC")
        print(f"  {when}  {t['rule']:<5} {t['side']:<5} {t['sym']:<12} {t['entry']:.2f} → {t['exit']:.2f}"
              f"  margin ₹{t['margin_inr']:,.0f}  net ₹{t['pnl_inr']:,.0f}  ({t['why_out']})")


def main():
    p = argparse.ArgumentParser(prog="python -m cdx.app")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("paper")
    pp.add_argument("--no-alerts", action="store_true")
    pp.set_defaults(fn=cmd_paper)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    sub.add_parser("backtest", add_help=False).set_defaults(fn=None)
    a, rest = p.parse_known_args()
    if a.cmd == "backtest":
        from . import backtest
        sys.argv = ["cdx.backtest", *rest]
        backtest.main()
        return
    a.fn(a)


if __name__ == "__main__":
    main()
