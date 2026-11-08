"""Closed paper trades in SQLite (data/paper.sqlite)."""
from __future__ import annotations

import sqlite3

from . import config

DB = config.DATA / "paper.sqlite"
COLS = ["rule", "sym", "side", "entry_ts", "exit_ts", "entry", "exit", "qty", "notional_inr", "margin_inr", "gross_inr",
        "fees_inr", "funding_inr", "pnl_inr", "R", "why_in", "why_out"]


def _con():
    DB.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB)


def init():
    with _con() as c:
        c.execute(f"CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, {', '.join(COLS)})")


def add(t: dict):
    with _con() as c:
        c.execute(f"INSERT INTO trades ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})", [t[k] for k in COLS])


def all_trades() -> list[dict]:
    if not DB.exists():
        return []
    with _con() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM trades ORDER BY exit_ts")]
