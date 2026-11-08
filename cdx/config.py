"""Settings from the environment (.env in the repo root is loaded automatically). Keys never live in code."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


SYMBOLS = [s.strip() for s in env("SYMBOLS", "B-BTC_USDT,B-ETH_USDT").split(",") if s.strip()]
RULES = [s.strip() for s in env("RULES", "dc1h,dc4h").split(",") if s.strip()]
CAPITAL_INR = float(env("CAPITAL_INR", "100000"))      # paper capital per rule (each rule has its own book)
RISK_PCT = float(env("RISK_PCT", "0.5"))               # % of capital lost if the stop is hit
MAX_LEVERAGE = float(env("MAX_LEVERAGE", "3"))         # notional cap = capital x this
POLL_S = float(env("POLL_S", "5"))
DAILY_LOSS_LIMIT_INR = float(env("DAILY_LOSS_LIMIT_INR", "3000"))  # per rule, per UTC day
SLACK_WEBHOOK_URL = env("SLACK_WEBHOOK_URL")

# CoinDCX futures: 0.05% taker + 18% GST = 0.059% of notional per side (read from the instrument at start-up when online)
TAKER_FEE = float(env("TAKER_FEE", "0.00059"))
SLIPPAGE = float(env("SLIPPAGE", "0.0001"))            # backtest only: 0.01% worse than the bar price per side
TAX_RATE = 0.30
