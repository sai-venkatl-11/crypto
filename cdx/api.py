"""CoinDCX public market data (no keys needed). Standard library only.

Endpoints (checked 8 Nov 2026):
  candles     public.coindcx.com/market_data/candlesticks?pair=B-BTC_USDT&from=&to=&resolution=1|5|15|60|1D&pcode=f
  prices      public.coindcx.com/market_data/v3/current_prices/futures/rt   (last, mark, funding rate per pair)
  order book  public.coindcx.com/market_data/v3/orderbook/<pair>-futures/<depth>
  instrument  api.coindcx.com/exchange/v1/derivatives/futures/data/instrument?pair=&margin_currency_short_name=USDT
  ticker      api.coindcx.com/exchange/ticker  (spot; USDTINR gives the rupee rate)
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

PUBLIC = "https://public.coindcx.com"
API = "https://api.coindcx.com"
UA = {"User-Agent": "cdx-paper/0.1"}


def get_json(url: str, tries: int = 4, timeout: float = 15):
    delay = 1.0
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
            if i == tries - 1:
                raise RuntimeError(f"GET {url} failed: {e}") from e
            time.sleep(delay)
            delay *= 2


def candles(pair: str, resolution: str, frm: int, to: int) -> list[dict]:
    """Futures candles between epoch seconds frm and to, oldest first, as {ts, o, h, l, c, v} (ts = bar start, ms).
    Pages backwards because one call returns at most ~30,000 bars."""
    step = {"1": 60, "5": 300, "15": 900, "30": 1800, "60": 3600, "1D": 86400}[resolution]
    out: dict[int, dict] = {}
    end = to
    while end > frm:
        start = max(frm, end - step * 20000)
        j = get_json(f"{PUBLIC}/market_data/candlesticks?pair={pair}&from={start}&to={end}&resolution={resolution}&pcode=f")
        rows = j.get("data") or []
        for r in rows:
            out[int(r["time"])] = {"ts": int(r["time"]), "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]),
                                   "c": float(r["close"]), "v": float(r.get("volume") or 0)}
        end = start
        if not rows:
            break
    return [out[k] for k in sorted(out)]


def prices() -> dict:
    """{pair: {"last", "mark", "funding"}} for every futures pair."""
    p = get_json(f"{PUBLIC}/market_data/v3/current_prices/futures/rt")["prices"]
    return {k: {"last": float(v["ls"]), "mark": float(v.get("mp") or v["ls"]), "funding": float(v.get("fr") or 0)}
            for k, v in p.items() if v.get("ls") is not None}


def best_bid_ask(pair: str) -> tuple[float, float]:
    ob = get_json(f"{PUBLIC}/market_data/v3/orderbook/{pair}-futures/10")
    return max(float(x) for x in ob["bids"]), min(float(x) for x in ob["asks"])


def instrument(pair: str) -> dict:
    return get_json(f"{API}/exchange/v1/derivatives/futures/data/instrument?pair={pair}&margin_currency_short_name=USDT")["instrument"]


def usdt_inr() -> float:
    for t in get_json(f"{API}/exchange/ticker"):
        if t["market"] == "USDTINR":
            return float(t["last_price"])
    raise RuntimeError("USDTINR not in ticker")
