# Crypto paper trader (CoinDCX futures)

A 24/7 paper-trading engine for BTC and ETH perpetual futures on CoinDCX. It reads live public prices (no API keys),
simulates fills at the real bid/ask with CoinDCX's fees and funding, and keeps a trade ledger with an after-tax view
under India's 30% crypto tax. **It never places real orders.**

Standard library only (Python 3.10+). No `pip install` needed.

## Quick start (Mac or any Linux machine)

```bash
git clone https://github.com/sai-venkatl-11/crypto.git ~/crypto
cd ~/crypto
cp .env.example .env          # optional: add SLACK_WEBHOOK_URL for alerts
python3 -m unittest           # 11 offline tests
python3 -m cdx.app backtest --days 365
python3 -m cdx.app paper      # runs until Ctrl+C; restart resumes open positions
```

In a second Terminal window:

```bash
python3 -m cdx.app status     # open positions, prices, funding
python3 -m cdx.app report     # closed trades, fees, after-tax totals
```

On a Mac, keep it awake while it runs: `caffeinate -i python3 -m cdx.app paper`. For real 24/7 use a cloud server
(see `deploy/`).

**Kill switch:** `touch data/KILL` closes every paper position and blocks new entries. `rm data/KILL` resumes.

## Rules

| Rule | Bars | Entry | Exit |
|---|---|---|---|
| `dc4h` (default) | 4 h | close beyond the last 1 day's high/low | 2 ATR stop, then trail 5 ATR behind the best close |
| `dc1h` (default) | 1 h | close beyond the last 7 days' high/low | same |
| `nz` | 30 min | noise-area band around the UTC day's open (port of the options desk's v2) | trail max(band, VWAP); flat at 00:00 UTC |
| `dc` | 15 min | close beyond the last 24 h | 2 ATR stop, 3 ATR trail |
| `pb` | 5 min | pullback in a VWAP/EMA trend (port of v3) | stop below the pullback, 2R target |

Choose rules with `RULES=dc1h,dc4h,nz` in `.env`. Each rule has its own paper book of `CAPITAL_INR`.

**Sizing:**
- each trade risks `RISK_PCT` of capital (default 0.5%, i.e. ₹500 on ₹1,00,000) if the stop is hit;
- position value is capped at `MAX_LEVERAGE` × capital (default 3×);
- `DAILY_LOSS_LIMIT_INR` stops a rule's new entries for the rest of the UTC day.

## Backtest results (365 days to 8 Nov 2026, BTC + ETH, ₹1,00,000 per rule)

| Rule | Trades | Net ₹ | Fees ₹ | After 30% tax (strict / netted) |
|---|---|---|---|---|
| `dc4h` | 136 | +17,730 | 3,022 | +187 / +12,411 |
| `dc1h` | 123 | +8,499 | 4,589 | −6,881 / +5,949 |
| `nz` | 1,911 | −66,325 | 78,289 | −1,12,421 / −66,325 |
| `dc` (15 min) | 851 | −59,866 | 79,689 | −1,28,128 / −59,866 |
| `pb` (5 min) | 3,839 | −6,89,193 | 6,29,263 | — |

What this shows:
- **Fees decide everything.** A round trip costs about 0.12% plus slippage. The fast rules made roughly zero before fees,
  so their fees became their losses. Only slow rules that trade a few times a week survive.
- **The two survivors are not proven.** `dc1h` and `dc4h` were picked from a sweep on this same year, so these numbers flatter
  them. In that sweep almost every variant lost money from Nov 2025 to May 2026 and made money from May to Nov 2026.
  That makes it a regime bet, not an established edge. Paper trade for weeks before risking money.
- **Tax:** "strict" applies 30% to every winning trade with no loss set-off (the cautious reading of India's VDA rules).
  "Netted" taxes only the net profit. Ask a CA which applies to you.

Backtest assumptions:
- fills at the bar close or the stop price, 0.01% worse;
- 0.059% fee per side;
- funding of 0.01% every 8 h (longs pay, shorts receive);
- USDT/INR fixed at today's rate.

## Files

```
cdx/api.py       CoinDCX public market data (candles, prices, order book, instrument, USDT/INR)
cdx/rules.py     trading rules (one object per rule and symbol)
cdx/broker.py    paper broker: sizing, fills, fees, funding, stops, loss limit, tax summary
cdx/engine.py    live 24/7 loop, state in data/state.json, trades in data/paper.sqlite
cdx/backtest.py  history backtest with the same broker
cdx/app.py       command line
deploy/          systemd service for a cloud server
```

## Safety

- This repo is **public**: never commit `.env`, keys or the `data/` folder (all git-ignored).
  Making the repo private is recommended.
- Paper trading needs no API key. For live trading later:
  - create a CoinDCX key **without withdrawal permission**;
  - lock it to the server's IP address;
  - put it only in the server's `.env`.
