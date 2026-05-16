"""Shared helpers for traders consulting entropy_alerts.jsonl.

Phase 1 use: DEFENSIVE only. Traders should refuse new positions whose side
would FADE a recent collapse (i.e., go opposite the informed flow).

Usage in a paper trader:

    from entropy_alert_helpers import should_block_for_collapse
    blocked, reason = should_block_for_collapse(
        ticker=sig["ticker"],
        proposed_side=sig["side"],
        engine="T3b",
    )
    if blocked:
        log(f"  [entropy-block] {sig['ticker']} {reason}")
        continue
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Tuple

ALERTS_PATH = Path.home() / "Documents" / "entropy_alerts.jsonl"
ALERT_LOOKBACK_MIN = 30  # consider alerts from the last 30 minutes


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def latest_watch_alert(ticker: str, engine: Optional[str] = None) -> Optional[dict]:
    """Return the most recent 'watch'-level alert for this ticker within the
    lookback window, or None."""
    if not ALERTS_PATH.exists():
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ALERT_LOOKBACK_MIN)
    latest: Optional[dict] = None
    try:
        with open(ALERTS_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    a = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if a.get("ticker") != ticker:
                    continue
                if engine and a.get("engine") != engine:
                    continue
                if a.get("alert_level") != "watch":
                    continue
                ts = _parse_iso(a.get("ts"))
                if ts is None or ts < cutoff:
                    continue
                if latest is None or ts > _parse_iso(latest["ts"]):
                    latest = a
    except OSError:
        return None
    return latest


def should_block_for_collapse(ticker: str, proposed_side: str,
                              engine: Optional[str] = None) -> Tuple[bool, str]:
    """Return (block: bool, reason: str).

    Block when there is a recent 'watch' alert AND the proposed trade would
    fade the alert direction (sell into informed flow).

    Mapping:
      direction='rising_yes'  → informed buyers; YES is being lifted.
        Don't open NO (would fade them). YES is fine (would join them, but
        Phase 1 is defensive-only — we just don't fade, we don't auto-join).
      direction='rising_no'   → informed buyers on NO side; YES is dropping.
        Don't open YES.
      direction='flat'        → no clear direction; don't block.
    """
    alert = latest_watch_alert(ticker, engine=engine)
    if alert is None:
        return False, ""
    direction = alert.get("direction") or "flat"
    side = (proposed_side or "").upper()
    if direction == "rising_yes" and side == "NO":
        return True, (f"entropy collapse z={alert.get('z_score')} "
                      f"rising_yes — not fading informed flow")
    if direction == "rising_no" and side == "YES":
        return True, (f"entropy collapse z={alert.get('z_score')} "
                      f"rising_no — not fading informed flow")
    return False, ""
