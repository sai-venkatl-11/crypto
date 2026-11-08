"""Offline tests: no network. Run with  python -m pytest tests  (or python -m unittest)."""
import random
import unittest

from cdx import backtest
from cdx.bars import Aggregator
from cdx.broker import Book, summary
from cdx.rules import RULES, Donchian, NoiseArea

FX = 100.0
SPEC = {"quantity_increment": 0.001, "min_quantity": 0.001, "min_notional": 0}


def bar(ts, o, h, l, c, v=1.0):
    return dict(ts=ts, o=o, h=h, l=l, c=c, v=v)


def walk(n, start=50_000.0, step_min=5, seed=1, drift=0.0, vol=0.002):
    rnd, p, out = random.Random(seed), start, []
    for i in range(n):
        o = p
        p = p * (1 + drift + rnd.gauss(0, vol))
        out.append(bar(i * step_min * 60_000, o, max(o, p) * 1.0005, min(o, p) * 0.9995, p, 10))
    return out


class TestBars(unittest.TestCase):
    def test_aggregate_5m_to_30m(self):
        ag = Aggregator(30, 5)
        out = []
        for i, b in enumerate(walk(12)):
            out += ag.add(b)
        self.assertEqual(len(out), 2)
        src = walk(12)
        self.assertEqual(out[0]["o"], src[0]["o"])
        self.assertEqual(out[0]["c"], src[5]["c"])
        self.assertEqual(out[0]["h"], max(b["h"] for b in src[:6]))
        self.assertEqual(out[1]["ts"], 30 * 60_000)


class TestBroker(unittest.TestCase):
    def book(self, **kw):
        return Book("t", capital_inr=100_000, risk_pct=0.5, max_lev=3, fee=0.00059, daily_loss_limit=3000, **kw)

    def test_sizing_by_risk(self):
        bk = self.book()
        p = bk.open("X", "long", 50_000, 49_000, None, 0, "t", FX, SPEC)
        # ₹500 risk / ₹100 per USDT / 1000 USDT stop distance = 0.005
        self.assertAlmostEqual(p["qty"], 0.005)

    def test_leverage_cap(self):
        bk = self.book()
        p = bk.open("X", "long", 50_000, 49_990, None, 0, "t", FX, SPEC)
        self.assertLessEqual(p["qty"] * 50_000 * FX, 300_000 + 1e-6)

    def test_stop_gap_fills_at_open_and_fees(self):
        bk = self.book()
        bk.open("X", "long", 50_000, 49_000, None, 0, "t", FX, SPEC)
        t = bk.check("X", bar(1, 48_500, 48_600, 48_400, 48_450), FX)
        self.assertEqual(t["exit"], 48_500)
        fees = (50_000 + 48_500) * 0.005 * 0.00059 * FX
        self.assertAlmostEqual(t["fees_inr"], fees)
        self.assertAlmostEqual(t["pnl_inr"], -1500 * 0.005 * FX - fees)

    def test_stop_wins_tie_with_target(self):
        bk = self.book()
        bk.open("X", "long", 100, 90, 120, 0, "t", FX, {"quantity_increment": 0.001})
        t = bk.check("X", bar(1, 100, 125, 85, 110), FX)
        self.assertEqual(t["why_out"], "stop")

    def test_daily_loss_limit(self):
        bk = self.book()
        bk.open("X", "long", 50_000, 49_000, None, 0, "t", FX, SPEC)
        bk.close("X", 40_000, 1, "x", FX)  # lose ~₹5,000
        self.assertIsNone(bk.open("X", "long", 50_000, 49_000, None, 2, "t", FX, SPEC))
        self.assertIsNotNone(bk.open("X", "long", 50_000, 49_000, None, 86_400_000, "t", FX, SPEC))

    def test_short_funding_received(self):
        bk = self.book()
        bk.open("X", "short", 50_000, 51_000, None, 0, "t", FX, SPEC)
        bk.fund("X", 0.0001, 50_000)
        self.assertLess(bk.pos["X"]["funding"], 0)

    def test_tax_views(self):
        T = [dict(pnl_inr=1000, fees_inr=0, funding_inr=0, R=1), dict(pnl_inr=-800, fees_inr=0, funding_inr=0, R=-1)]
        s = summary(T)
        self.assertAlmostEqual(s["after_tax_strict"], 200 - 300)
        self.assertAlmostEqual(s["after_tax_netted"], 200 - 60)


class TestRules(unittest.TestCase):
    def test_donchian_breakout_and_trail(self):
        d = Donchian(n=10, stop_atr=2, trail_atr=3)
        bars = [bar(i, 100, 101, 99, 100) for i in range(10)]
        for b in bars:
            self.assertIsNone(d.on_bar(b, None))
        it = d.on_bar(bar(10, 100, 103, 100, 102.5), None)
        self.assertEqual((it["act"], it["side"]), ("enter", "long"))
        pos = {"side": "long", "entry": 102.5, "stop": it["stop"]}
        moved = [d.on_bar(bar(11 + i, 102 + 2 * i, 104 + 2 * i, 102 + 2 * i, 104 + 2 * i), pos) for i in range(5)]
        stops = [m["stop"] for m in moved if m]
        self.assertTrue(stops and all(b > a for a, b in zip(stops, stops[1:])))

    def test_noise_area_needs_history_then_trades(self):
        nz = NoiseArea(N=3)
        acts = []
        for b in walk(48 * 5, step_min=30, seed=3, vol=0.004):
            it = nz.on_bar(b, None)
            if it:
                acts.append(it["act"])
        self.assertTrue(set(acts) <= {"enter"})
        self.assertGreater(len(acts), 0)

    def test_backtest_runs_offline(self):
        series = {"X": walk(288 * 20, seed=7, drift=0.0001)}
        books = backtest.run(series, list(RULES), FX, {"X": SPEC})
        for r, bk in books.items():
            self.assertFalse(bk.pos, r)  # everything closed at the end
            for t in bk.trades:
                self.assertAlmostEqual(t["pnl_inr"], t["gross_inr"] - t["fees_inr"] - t["funding_inr"], places=6)


if __name__ == "__main__":
    unittest.main()
