"""Live paper trading, 24/7: CoinDCX futures prices in, simulated fills out. No API keys, no real orders.

Loop (every POLL_S seconds):
  1. last price per symbol -> stops / targets of open paper positions
  2. once per minute: newly closed 5-min candles -> each rule's frame (1 h, 4 h, ...) -> rule decisions
  3. entries and rule exits fill at the live best ask (buys) / best bid (sells); fees 0.059% per side
  4. funding at 00:00, 08:00, 16:00 UTC from the live funding rate
State is saved to data/state.json after every loop and trades to data/paper.sqlite, so a restart resumes open positions.
Create the file data/KILL to close every paper position and stop opening new ones.
"""
from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timezone

from . import alerts, api, config, ledger
from .bars import Aggregator
from .broker import Book
from .rules import RULES

STATE = config.DATA / "state.json"
KILL = config.DATA / "KILL"
WARM_DAYS = 30


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class Engine:
    def __init__(self, symbols=None, rules=None, notify=True):
        self.symbols = symbols or config.SYMBOLS
        self.rules = rules or config.RULES
        self.notify = notify
        self.specs = {s: api.instrument(s) for s in self.symbols}
        fee = self.specs[self.symbols[0]].get("taker_fee")
        self.fee = fee / 100 if fee else config.TAKER_FEE
        self.fx, self.fx_at = api.usdt_inr(), time.time()
        self.books = {r: Book(r, fee=self.fee) for r in self.rules}
        self.strat = {(r, s): RULES[r]() for r in self.rules for s in self.symbols}
        self.aggs = {(r, s): Aggregator(RULES[r].frame, 5) for r in self.rules for s in self.symbols}
        self.last5 = {s: 0 for s in self.symbols}
        self.last_funding_hour = now_ms() // 3_600_000  # never charge twice for an hour after a restart
        self.last_prices: dict = {}
        self.killed = False
        ledger.init()
        self._restore()
        self._warm_up()

    # -------------------------------------------------------------- start-up
    def _restore(self):
        if not STATE.exists():
            return
        st = json.loads(STATE.read_text())
        for r, positions in st.get("positions", {}).items():
            if r in self.books:
                self.books[r].pos.update(positions)
        for r, dp in st.get("day_pnl", {}).items():
            if r in self.books:
                self.books[r].day_pnl.update({int(k): v for k, v in dp.items()})

    def _warm_up(self):
        """Feed the last WARM_DAYS of 5-min candles through the rules so channels, ATR and bands are ready. No trades."""
        end = int(time.time())
        for s in self.symbols:
            bars = [b for b in api.candles(s, "5", end - WARM_DAYS * 86400, end) if b["ts"] + 300_000 <= now_ms()]
            for b in bars:
                for r in self.rules:
                    for fb in self.aggs[(r, s)].add(b):
                        self.strat[(r, s)].on_bar(fb, self.books[r].pos.get(s))
            self.last5[s] = bars[-1]["ts"] if bars else 0
        self._say(f"Paper engine started: {', '.join(self.symbols)} · rules {', '.join(self.rules)} · "
                  f"₹{config.CAPITAL_INR:,.0f} per rule · USDT/INR {self.fx:.2f}")

    # -------------------------------------------------------------- loop
    def run(self):
        while True:
            t0 = time.time()
            try:
                self.step()
            except Exception:  # keep running 24/7; log and retry next loop
                traceback.print_exc()
                time.sleep(10)
            time.sleep(max(0.5, config.POLL_S - (time.time() - t0)))

    def step(self):
        if time.time() - self.fx_at > 1800:
            self.fx, self.fx_at = api.usdt_inr(), time.time()
        px = api.prices()
        self.last_prices = {s: px[s] for s in self.symbols if s in px}
        ts = now_ms()
        if KILL.exists() and not self.killed:
            self.killed = True
            for r, bk in self.books.items():
                for s in list(bk.pos):
                    self._exit(r, s, "kill switch")
            self._say("Kill switch: all paper positions closed; no new entries until data/KILL is removed.")
        elif not KILL.exists():
            self.killed = False
        self._funding(ts)
        for s, p in self.last_prices.items():  # 1. stops and targets on the live price
            tick = dict(ts=ts, o=p["last"], h=p["last"], l=p["last"], c=p["last"])
            for r, bk in self.books.items():
                t = bk.check(s, tick, self.fx)
                if t:
                    self.strat[(r, s)].closed()
                    self._booked(t)
        if (ts // 1000) % 60 < config.POLL_S + 2 or ts - max(self.last5.values()) > 11 * 60_000:
            self._new_candles()
        self._save()

    def _new_candles(self):
        end = int(time.time())
        for s in self.symbols:
            bars = [b for b in api.candles(s, "5", end - 3600, end) if b["ts"] > self.last5[s] and b["ts"] + 300_000 <= now_ms()]
            for b in bars:
                self.last5[s] = b["ts"]
                for r in self.rules:
                    for fb in self.aggs[(r, s)].add(b):
                        self._decide(r, s, fb)

    def _decide(self, r, s, fb):
        bk, st = self.books[r], self.strat[(r, s)]
        it = st.on_bar(fb, bk.pos.get(s))
        if not it:
            return
        if it["act"] == "stop" and s in bk.pos:
            bk.pos[s]["stop"] = it["stop"]
        elif it["act"] == "exit":
            self._exit(r, s, it["why"])
        elif it["act"] == "enter" and not self.killed:
            bid, ask = api.best_bid_ask(s)
            p = bk.open(s, it["side"], ask if it["side"] == "long" else bid, it["stop"], it["target"], now_ms(), it["why"],
                        self.fx, self.specs[s])
            if p:
                n = p["entry"] * p["qty"] * self.fx
                tgt = f" · target {p['target']:.2f}" if p["target"] else ""
                verb = "BUY" if p["side"] == "long" else "SELL"
                self._say(f"🟢 PAPER {r} {verb} {p['qty']:g} {s} at {p['entry']:.2f} (₹{n:,.0f} notional, "
                          f"₹{n / bk.max_lev:,.0f} margin) · stop {p['stop']:.2f}{tgt} · {it['why']}")

    def _exit(self, r, s, why):
        bk = self.books[r]
        if s not in bk.pos:
            return
        bid, ask = api.best_bid_ask(s)
        t = bk.close(s, bid if bk.pos[s]["side"] == "long" else ask, now_ms(), why, self.fx)
        self.strat[(r, s)].closed()
        self._booked(t)

    def _booked(self, t):
        ledger.add(t)
        self._say(f"{'✅' if t['pnl_inr'] > 0 else '🔴'} PAPER {t['rule']} closed {t['side']} {t['sym']} {t['entry']:.2f} → {t['exit']:.2f} "
                  f"· net ₹{t['pnl_inr']:,.0f} (fees ₹{t['fees_inr']:,.0f}) · {t['why_out']}")

    def _funding(self, ts):
        hour = ts // 3_600_000
        if hour % 24 in (0, 8, 16) and hour != self.last_funding_hour:
            self.last_funding_hour = hour
            for s, p in self.last_prices.items():
                for bk in self.books.values():
                    bk.fund(s, p["funding"], p["mark"])

    def _save(self):
        marks = {s: p["mark"] for s, p in self.last_prices.items()}
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(
            updated=iso(now_ms()), fx=self.fx, prices=self.last_prices, killed=self.killed,
            positions={r: bk.pos for r, bk in self.books.items()},
            unrealized_inr={r: bk.unrealized_inr(marks, self.fx) for r, bk in self.books.items()},
            day_pnl={r: bk.day_pnl for r, bk in self.books.items()}), default=str))
        tmp.replace(STATE)

    def _say(self, msg):
        print(f"[{iso(now_ms())}] {msg}", flush=True)
        if self.notify:
            alerts.slack(msg)
