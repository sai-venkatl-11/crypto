"""Trading rules for 24/7 crypto perpetuals. One object per (rule, symbol); feed it closed bars of its frame in order.

on_bar(bar, pos) returns None or an intent:
  {"act": "enter", "side": "long"|"short", "stop": price, "target": price|None, "why": str}
  {"act": "exit", "why": str}
  {"act": "stop", "stop": price}            move the protective stop (only ever tightens)
The broker executes intents at the bar close and checks stops/targets between bars (ticks live, bar high/low in backtests).

nz  noise area (Zarattini, Barbon & Aziz 2024), adapted to the UTC day: 30-min closes outside a time-of-day band around
    the day's open; trail at max(band, VWAP); flat at the end of the UTC day (05:30 IST). Port of v2 from the options desk.
dc  Donchian trend: 15-min close beyond the 24-hour high/low; 2 ATR stop, then a 3 ATR chandelier trail. Holds across days.
    dc1h / dc4h: the same rule on 1-hour bars with a 7-day channel, and on 4-hour bars with a 1-day channel (5 ATR trail).
pb  pullback: 5-min higher low in a VWAP + rising-EMA20 trend (port of v3); stop beyond the pullback, 2R target, 12 h max.
"""
from __future__ import annotations

from collections import deque

from .bars import utc_day


class _Base:
    frame = 5
    name = "?"

    def __init__(self, keep: int = 400):
        self.bars: deque = deque(maxlen=keep)
        self._ema: dict[int, float] = {}

    def _push(self, b):
        prev = self.bars[-1] if self.bars else None
        tr = b["h"] - b["l"] if prev is None else max(b["h"] - b["l"], abs(b["h"] - prev["c"]), abs(b["l"] - prev["c"]))
        b = dict(b, tr=tr)
        self.bars.append(b)
        return b

    def atr(self, n=14):
        w = list(self.bars)[-n:]
        return sum(x["tr"] for x in w) / len(w)

    def closed(self):
        """Called by the broker when a position of this rule is closed."""

    def ema(self, n, x):
        k = 2 / (n + 1)
        self._ema[n] = x if n not in self._ema else x * k + self._ema[n] * (1 - k)
        return self._ema[n]


class NoiseArea(_Base):
    """30-min marks. sigma_k = mean |close_k / day_open - 1| over the last N complete UTC days (floor 0.1%)."""
    frame, name = 30, "nz"

    def __init__(self, N=14, last_entry_k=39, floor=0.001):
        super().__init__(keep=100)
        self.N, self.last_entry_k, self.floor = N, last_entry_k, floor
        self.hist: deque = deque(maxlen=N)  # each: list of 48 abs moves
        self.day, self.day_open, self.moves, self.pv, self.vv = None, None, [], 0.0, 0.0

    def on_bar(self, b, pos):
        b = self._push(b)
        d = utc_day(b["ts"])
        if d != self.day:
            if self.day is not None and len(self.moves) == 48:
                self.hist.append(self.moves)
            self.day, self.day_open, self.moves, self.pv, self.vv = d, b["o"], [], 0.0, 0.0
        k = (b["ts"] // 1_800_000) % 48
        self.moves.append(abs(b["c"] / self.day_open - 1))
        v = b["v"] or 1.0
        self.pv += (b["h"] + b["l"] + b["c"]) / 3 * v
        self.vv += v
        vwap = self.pv / self.vv
        if len(self.hist) < self.N:
            return {"act": "exit", "why": "warm-up"} if pos else None
        sig = max(self.floor, sum(h[k] for h in self.hist) / len(self.hist))
        ub, lb = self.day_open * (1 + sig), self.day_open * (1 - sig)
        if pos:
            if k == 47:
                return {"act": "exit", "why": "UTC day end"}
            if pos["side"] == "long" and b["c"] < max(ub, vwap):
                return {"act": "exit", "why": "trailing max(band, VWAP)"}
            if pos["side"] == "short" and b["c"] > min(lb, vwap):
                return {"act": "exit", "why": "trailing min(band, VWAP)"}
            return None
        if k > self.last_entry_k:
            return None
        a = self.atr()
        if b["c"] > ub:
            return {"act": "enter", "side": "long", "stop": b["c"] - 3 * a, "target": None, "why": f"above noise band {ub:.2f}"}
        if b["c"] < lb:
            return {"act": "enter", "side": "short", "stop": b["c"] + 3 * a, "target": None, "why": f"below noise band {lb:.2f}"}
        return None


class Donchian(_Base):
    frame, name = 15, "dc"

    def __init__(self, n=96, stop_atr=2.0, trail_atr=3.0):
        super().__init__(keep=n + 50)
        self.n, self.stop_atr, self.trail_atr = n, stop_atr, trail_atr

    def on_bar(self, b, pos):
        prior = list(self.bars)[-self.n:]
        b = self._push(b)
        if len(prior) < self.n:
            return None
        a = self.atr()
        if pos:
            best = pos.get("best_close", pos["entry"])
            if pos["side"] == "long":
                best = max(best, b["c"])
                pos["best_close"] = best
                ns = best - self.trail_atr * a
                return {"act": "stop", "stop": ns} if ns > pos["stop"] else None
            best = min(best, b["c"])
            pos["best_close"] = best
            ns = best + self.trail_atr * a
            return {"act": "stop", "stop": ns} if ns < pos["stop"] else None
        hi, lo = max(x["h"] for x in prior), min(x["l"] for x in prior)
        if b["c"] > hi:
            return {"act": "enter", "side": "long", "stop": b["c"] - self.stop_atr * a, "target": None, "why": f"above 24h high {hi:.2f}"}
        if b["c"] < lo:
            return {"act": "enter", "side": "short", "stop": b["c"] + self.stop_atr * a, "target": None, "why": f"below 24h low {lo:.2f}"}
        return None


class Pullback(_Base):
    frame, name = 5, "pb"

    def __init__(self, max_hold=144):
        super().__init__(keep=300)
        self.max_hold = max_hold
        self.e20: list[float] = []
        self.vw: list[float] = []
        self.day, self.pv, self.vv, self.n_day, self.cool = None, 0.0, 0.0, 0, 0

    def on_bar(self, b, pos):
        b = self._push(b)
        d = utc_day(b["ts"])
        if d != self.day:
            self.day, self.pv, self.vv, self.n_day = d, 0.0, 0.0, 0
        v = b["v"] or 1.0
        self.pv += (b["h"] + b["l"] + b["c"]) / 3 * v
        self.vv += v
        self.n_day += 1
        self.vw.append(self.pv / self.vv)
        self.e20.append(self.ema(20, b["c"]))
        self.vw, self.e20 = self.vw[-300:], self.e20[-300:]
        if self.cool:
            self.cool -= 1
        C = list(self.bars)
        if pos:
            pos["held"] = pos.get("held", 0) + 1
            return {"act": "exit", "why": "12h max hold"} if pos["held"] >= self.max_hold else None
        if len(C) < 13 or self.n_day < 6 or self.cool:
            return None
        a, p = self.atr(), C[-2]
        look, recent = C[-13:-1], C[-5:-1]
        vw_recent = self.vw[-5:-1]
        if b["c"] > self.vw[-1] and self.e20[-1] > self.e20[-4]:
            Hs, Ls = max(x["h"] for x in look), min(x["l"] for x in recent)
            held = all(x["c"] >= vw_recent[k] - 0.5 * a for k, x in enumerate(recent))
            if Hs - Ls >= 0.8 * a and held and b["c"] > b["o"] and b["c"] > p["h"] and b["l"] >= Ls:
                stop = Ls - 0.25 * a
                return {"act": "enter", "side": "long", "stop": stop, "target": b["c"] + 2 * (b["c"] - stop), "why": "pullback higher low in uptrend"}
        elif b["c"] < self.vw[-1] and self.e20[-1] < self.e20[-4]:
            Ls, Hs = min(x["l"] for x in look), max(x["h"] for x in recent)
            held = all(x["c"] <= vw_recent[k] + 0.5 * a for k, x in enumerate(recent))
            if Hs - Ls >= 0.8 * a and held and b["c"] < b["o"] and b["c"] < p["l"] and b["h"] <= Hs:
                stop = Hs + 0.25 * a
                return {"act": "enter", "side": "short", "stop": stop, "target": b["c"] - 2 * (stop - b["c"]), "why": "pullback lower high in downtrend"}
        return None

    def closed(self):
        self.cool = 2


def _donchian(name, frame, days, stop_atr, trail_atr):
    n = days * 1440 // frame
    return type(name, (Donchian,), {"frame": frame, "name": name,
                                    "__init__": lambda self: Donchian.__init__(self, n=n, stop_atr=stop_atr, trail_atr=trail_atr)})


# Defaults are the three variants that held up best in a 365-day backtest (Nov 2025 - Nov 2026, BTC + ETH):
#   dc4h  4-hour bars, 1-day channel, 5 ATR trail     dc1h  1-hour bars, 7-day channel, 5 ATR trail
# Faster rules (dc 15-min, pb) traded so often that fees (0.118% per round trip) wiped out any edge.
RULES = {
    "nz": NoiseArea,
    "dc": Donchian,
    "dc1h": _donchian("dc1h", 60, 7, 2.0, 5.0),
    "dc4h": _donchian("dc4h", 240, 1, 2.0, 5.0),
    "pb": Pullback,
}
