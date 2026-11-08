"""Paper broker: sizing, fills, fees, funding, stops/targets, daily loss limit and the trade ledger. Shared by the
backtest and the live paper engine so both book trades the same way. Amounts are kept in USDT and converted to INR
at the USDT/INR rate of the moment."""
from __future__ import annotations

import math

from . import config

FUNDING_HOURS = (0, 8, 16)  # CoinDCX funding_frequency = 8 h


class Book:
    def __init__(self, rule: str, capital_inr=None, risk_pct=None, max_lev=None, fee=None, daily_loss_limit=None, slippage=0.0):
        self.rule = rule
        self.capital = capital_inr or config.CAPITAL_INR
        self.risk_pct = risk_pct or config.RISK_PCT
        self.max_lev = max_lev or config.MAX_LEVERAGE
        self.fee = config.TAKER_FEE if fee is None else fee
        self.limit = config.DAILY_LOSS_LIMIT_INR if daily_loss_limit is None else daily_loss_limit
        self.slip = slippage
        self.pos: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.day_pnl: dict[int, float] = {}
        self.skipped: list[dict] = []

    # ------------------------------------------------------------------ helpers
    def _adverse(self, px, side, opening):
        worse_up = (side == "long") == opening  # buying: pay more; selling: receive less
        return px * (1 + self.slip) if worse_up else px * (1 - self.slip)

    def blocked(self, ts) -> bool:
        return self.day_pnl.get(ts // 86_400_000, 0.0) <= -self.limit

    # ------------------------------------------------------------------ actions
    def open(self, sym, side, px, stop, target, ts, why, fx, spec=None):
        spec = spec or {}
        step = spec.get("quantity_increment", 0.001)
        if sym in self.pos:
            return None
        if self.blocked(ts):
            self.skipped.append(dict(sym=sym, ts=ts, why="daily loss limit"))
            return None
        fill = self._adverse(px, side, True)
        dist = abs(fill - stop)
        if dist <= 0 or (side == "long" and stop >= fill) or (side == "short" and stop <= fill):
            return None
        qty = self.capital * self.risk_pct / 100 / fx / dist
        qty = min(qty, self.capital * self.max_lev / fx / fill)
        qty = math.floor(qty / step + 1e-9) * step
        if qty < spec.get("min_quantity", step) or qty * fill < spec.get("min_notional", 0):
            self.skipped.append(dict(sym=sym, ts=ts, why="below minimum size"))
            return None
        p = dict(sym=sym, side=side, entry=fill, qty=qty, stop=stop, target=target, ts=ts, why=why, risk=dist,
                 fees=fill * qty * self.fee, funding=0.0)
        self.pos[sym] = p
        return p

    def close(self, sym, px, ts, why, fx, raw=False):
        p = self.pos.pop(sym, None)
        if not p:
            return None
        fill = px if raw else self._adverse(px, p["side"], False)
        s = 1 if p["side"] == "long" else -1
        fees = p["fees"] + fill * p["qty"] * self.fee
        gross = s * (fill - p["entry"]) * p["qty"]
        net = gross - fees - p["funding"]
        t = dict(rule=self.rule, sym=sym, side=p["side"], entry_ts=p["ts"], exit_ts=ts, entry=p["entry"], exit=fill,
                 qty=p["qty"], notional_inr=p["entry"] * p["qty"] * fx, margin_inr=p["entry"] * p["qty"] * fx / self.max_lev,
                 gross_inr=gross * fx, fees_inr=fees * fx, funding_inr=p["funding"] * fx, pnl_inr=net * fx,
                 R=s * (fill - p["entry"]) / p["risk"], why_in=p["why"], why_out=why)
        self.trades.append(t)
        d = ts // 86_400_000
        self.day_pnl[d] = self.day_pnl.get(d, 0.0) + t["pnl_inr"]
        return t

    def check(self, sym, bar, fx):
        """Stop / target against a bar's range (a live tick is a bar with o = h = l = c). Stop wins a tie."""
        p = self.pos.get(sym)
        if not p:
            return None
        if p["side"] == "long":
            if bar["l"] <= p["stop"]:
                return self.close(sym, min(bar["o"], p["stop"]), bar["ts"], "stop", fx)
            if p["target"] and bar["h"] >= p["target"]:
                return self.close(sym, max(bar["o"], p["target"]), bar["ts"], "target", fx, raw=True)
        else:
            if bar["h"] >= p["stop"]:
                return self.close(sym, max(bar["o"], p["stop"]), bar["ts"], "stop", fx)
            if p["target"] and bar["l"] <= p["target"]:
                return self.close(sym, min(bar["o"], p["target"]), bar["ts"], "target", fx, raw=True)
        return None

    def fund(self, sym, rate, mark):
        """Apply one funding payment: longs pay a positive rate, shorts receive it."""
        p = self.pos.get(sym)
        if p:
            p["funding"] += (1 if p["side"] == "long" else -1) * rate * mark * p["qty"]

    def unrealized_inr(self, marks: dict, fx):
        u = 0.0
        for sym, p in self.pos.items():
            if sym in marks:
                s = 1 if p["side"] == "long" else -1
                u += (s * (marks[sym] - p["entry"]) * p["qty"] - p["fees"] - p["funding"]) * fx
        return u


def summary(trades: list[dict]) -> dict:
    """Totals, plus two readings of India's 30% VDA tax: 'strict' taxes every winning trade and ignores losses
    (no set-off), 'netted' taxes only the net profit. Which applies to INR/USDT futures is unsettled: ask a CA."""
    n = len(trades)
    pnl = [t["pnl_inr"] for t in trades]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    net = sum(pnl)
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    tax_strict = config.TAX_RATE * sum(wins)
    tax_net = config.TAX_RATE * max(net, 0)
    return dict(trades=n, win_pct=100 * len(wins) / n if n else 0, net_inr=net, avg_inr=net / n if n else 0,
                fees_inr=sum(t["fees_inr"] for t in trades), funding_inr=sum(t["funding_inr"] for t in trades),
                pf=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf") if wins else 0,
                max_dd_inr=dd, avg_R=sum(t["R"] for t in trades) / n if n else 0,
                after_tax_strict=net - tax_strict, after_tax_netted=net - tax_net)
