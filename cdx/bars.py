"""Aggregates base candles (1- or 5-minute) into larger UTC-aligned frames. Bar = {ts, o, h, l, c, v}, ts = start (ms)."""
from __future__ import annotations

DAY_MS = 86_400_000


def utc_day(ts_ms: int) -> int:
    return ts_ms // DAY_MS


class Aggregator:
    """Feed base bars in time order; add() returns the frame bar when its last base bar arrives (or a later one does)."""

    def __init__(self, frame_min: int, base_min: int):
        assert frame_min % base_min == 0
        self.ms, self.base_ms, self.cur = frame_min * 60_000, base_min * 60_000, None

    def add(self, b: dict) -> list[dict]:
        out = []
        start = b["ts"] - b["ts"] % self.ms
        if self.cur and self.cur["ts"] != start:
            out.append(self.cur)  # a gap skipped the closing bar: emit what we have
            self.cur = None
        if self.cur is None:
            self.cur = dict(ts=start, o=b["o"], h=b["h"], l=b["l"], c=b["c"], v=b["v"])
        else:
            c = self.cur
            c["h"], c["l"], c["c"], c["v"] = max(c["h"], b["h"]), min(c["l"], b["l"]), b["c"], c["v"] + b["v"]
        if b["ts"] + self.base_ms >= start + self.ms:
            out.append(self.cur)
            self.cur = None
        return out
