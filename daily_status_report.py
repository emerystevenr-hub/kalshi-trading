"""Daily status emailer for the Kalshi trading portfolio.

Composes a plain-text snapshot of T6 + T7 state and emails it to
GMAIL_TO via Gmail SMTP. Designed to fire once per morning while the
operator is offline (travel, weekends) so anything anomalous surfaces
before it compounds.

State sources (all reused from shadow_dashboard.gather_state — single
source of truth; do NOT re-derive any field here):
  - shadow_pnl/engines.json                 (engine roster, bankrolls)
  - shadow_pnl/ledger.jsonl                 (raw + clean stats via T6 milestone)
  - terminal{6,7}_data/paper_trader.log     (daemon liveness)
  - scheduler_status.json / scheduler.pid   (scheduler health)
  - scheduler_logs/t6_mlb_lines_puller.log  (Odds API remaining credits)
  - freshness_alarm.flag / macro_cap_alarm.flag

Credential file (one-time setup; ~/Documents/.env is gitignored, this is
named .env so it inherits the existing gitignore rule):

    ~/Documents/gmail_smtp.env

        GMAIL_USER=youraccount@gmail.com
        GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
        GMAIL_TO=youraccount@gmail.com

  Generate an app password at https://myaccount.google.com/apppasswords
  (2-step verification must be enabled on the account first).

Subject line:
    Kalshi Trading — Daily Status YYYY-MM-DD — OK
    Kalshi Trading — Daily Status YYYY-MM-DD — ALERT

ALERT triggers (any of):
    - freshness_alarm.flag present
    - macro_cap_alarm.flag present
    - scheduler PID file exists but process is dead
    - T6 gate == EARLY_KILL or DEAD
    - Odds API credits < 50
    - any active engine's WS logger silent > DAEMON_STALE_SEC (5 min)
    - any scheduler job last_exit_code != 0

Usage:
    python3 ~/Documents/daily_status_report.py                # send now
    python3 ~/Documents/daily_status_report.py --no-send      # render to stdout, no SMTP
    python3 ~/Documents/daily_status_report.py --scheduled    # send only if 08:00 PT
                                                              # window AND not yet sent today

The scheduler (portfolio_scheduler.py) fires this every 5 min with
--scheduled. The script no-ops silently unless current PT time is in
[08:00, 08:10) AND the marker file does not already contain today's
date (PT). This pattern lets us use the existing interval scheduler
for a time-of-day job without bolting on cron.
"""

from __future__ import annotations

import argparse
import os
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# Allow imports from ~/Documents
DOCS = Path.home() / "Documents"
sys.path.insert(0, str(DOCS))

import shadow_dashboard  # noqa: E402  — reuse gather_state, no duplication

PT = ZoneInfo("America/Los_Angeles")
SEND_HOUR_PT = 8           # 08:00 PT
SEND_WINDOW_MIN = 10       # accept any tick within [08:00, 08:10) PT
MARKER_FILE = DOCS / "terminal6_data" / "daily_report_last_sent.txt"
SMTP_ENV = DOCS / "gmail_smtp.env"


# ─────────────────────────────────────────────────────────────────────
# Credential loading
# ─────────────────────────────────────────────────────────────────────

def _load_smtp_env() -> Dict[str, str]:
    """Parse KEY=VALUE lines from gmail_smtp.env. Fail loud."""
    if not SMTP_ENV.exists():
        raise FileNotFoundError(
            f"Missing {SMTP_ENV}. Create it with GMAIL_USER, GMAIL_APP_PASSWORD, "
            f"GMAIL_TO (see file header for format)."
        )
    out: Dict[str, str] = {}
    for line in SMTP_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    for required in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "GMAIL_TO"):
        if not out.get(required):
            raise RuntimeError(f"{SMTP_ENV} missing required key: {required}")
    return out


# ─────────────────────────────────────────────────────────────────────
# State enrichment — "since yesterday" metrics not in dashboard state
# ─────────────────────────────────────────────────────────────────────

def _closes_since(engine: str, since_ts: float) -> Tuple[int, float]:
    """Return (count, sum_realized_pnl_usd) of closes for engine after since_ts.
    Walks the raw ledger directly. For T6 we count clean (post-contamination-
    filter) closes only — the same definition the milestone check uses."""
    ledger = shadow_dashboard._read_jsonl(shadow_dashboard.LEDGER)
    if engine == "T6":
        clean_closes = [
            c for c in shadow_dashboard.load_t6_closed()
            if not shadow_dashboard._is_contaminated(c)
        ]
        n = 0
        total = 0.0
        for c in clean_closes:
            try:
                ts = datetime.fromisoformat(
                    (c.get("close_ts") or "").replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError):
                continue
            if ts >= since_ts:
                n += 1
                total += float(c.get("realized_pnl_usd") or 0)
        return n, round(total, 2)

    # Generic path for other engines (T7 — pair opens/closes from raw ledger)
    n = 0
    total = 0.0
    for r in ledger:
        if r.get("engine") != engine or r.get("type") != "close":
            continue
        try:
            ts = datetime.fromisoformat(
                (r.get("ts") or "").replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError):
            continue
        if ts >= since_ts:
            n += 1
            total += float(r.get("realized_pnl_usd") or 0)
    return n, round(total, 2)


# ─────────────────────────────────────────────────────────────────────
# Report rendering
# ─────────────────────────────────────────────────────────────────────

def _fmt_ts(epoch: Optional[float]) -> str:
    if epoch is None:
        return "none"
    dt_utc = datetime.fromtimestamp(epoch, tz=timezone.utc)
    age = time.time() - epoch
    return f"{dt_utc.strftime('%Y-%m-%d %H:%M UTC')} ({shadow_dashboard._fmt_age(age)})"


def render_report(state: dict) -> Tuple[str, str]:
    """Return (subject, body). Subject ends in 'OK' or 'ALERT'."""
    now_pt = datetime.now(PT)
    date_str = now_pt.strftime("%Y-%m-%d")
    one_day_ago_ts = time.time() - 86400

    alerts = state.get("alerts") or []
    status_tag = "ALERT" if alerts else "OK"
    subject = f"Kalshi Trading — Daily Status {date_str} — {status_tag}"

    lines: List[str] = []
    lines.append(f"Kalshi Trading — Daily Status")
    lines.append(f"Date:        {date_str} ({now_pt.strftime('%H:%M %Z')})")
    lines.append(f"Status:      {status_tag}")
    lines.append("=" * 60)
    lines.append("")

    # Per-engine sections
    for eid in state.get("active_engine_order") or []:
        e = state["per_engine"].get(eid, {})
        meta = state["engines_meta"].get(eid, {})
        n24, pnl24 = _closes_since(eid, one_day_ago_ts)

        lines.append(f"[{eid}] {meta.get('name', '')}")
        lines.append(f"  bankroll:        ${e.get('bankroll', 0):,.0f}  (engines.json)")
        lines.append(f"  gate:            {e.get('gate', '?')}"
                     + (f"  ({e.get('gate_n')}/{e.get('gate_target')})"
                        if e.get('gate_target') else ""))
        lines.append(f"  W / L / open:    {e.get('wins', 0)} / "
                     f"{e.get('losses', 0)} / {e.get('open', 0)}")
        lines.append(f"  clean realized:  ${e.get('realized', 0):+,.2f}")
        lines.append(f"  closes 24h:      {n24}  (P&L 24h: ${pnl24:+,.2f})")
        lines.append(f"  last fire:       {_fmt_ts(e.get('last_fire_ts'))}")
        lines.append(f"  daemon alive:    {'YES' if e.get('daemon_alive') else 'NO'}")
        # Profit factor + annualized Sharpe (currently T6 only; T7 shows n/a)
        pf = e.get('profit_factor')
        sa = e.get('sharpe_annualized')
        gp = e.get('gross_profit')
        gl = e.get('gross_loss')
        if gp is not None or gl is not None:
            lines.append(f"  gross P / L:     "
                         f"${(gp or 0):+,.2f} / ${(gl or 0):,.2f}")
        lines.append(f"  profit factor:   "
                     f"{f'{pf:.2f}' if pf is not None else 'n/a'}   (target ≥ 1.5)")
        lines.append(f"  sharpe (annual): "
                     f"{f'{sa:+.2f}' if sa is not None else 'n/a'}   (target ≥ 2.0)")
        if e.get('pf_sharpe_warning'):
            lines.append(f"  ** PF/SHARPE FLAG: {e['pf_sharpe_warning']}  (diagnostic only) **")
        lines.append("")

    # Infrastructure
    sh = state["scheduler_health"]
    sched_status = state.get("scheduler_status") or {}
    uptime = sched_status.get("scheduler_uptime_sec")
    cap_pct = state.get("macro_cap_pct")
    credits = state.get("odds_api_credits")

    lines.append("Infrastructure")
    lines.append(f"  scheduler:       "
                 f"{'alive' if sh.get('alive') else 'DOWN'} "
                 f"(pid {sh.get('pid')}"
                 + (f", uptime {uptime/86400:.1f}d" if uptime else "")
                 + ")")
    lines.append(f"  odds api credits:"
                 f" {credits if credits is not None else '?'}")
    lines.append(f"  macro cap %:     "
                 f"{cap_pct:.1f}%" if cap_pct is not None else "  macro cap %:     n/a")
    lines.append(f"  portfolio realized total: ${state.get('portfolio_realized', 0):+,.2f}")
    lines.append("")

    # Alerts
    lines.append("Alerts")
    if not alerts:
        lines.append("  none")
    else:
        for sev, msg in alerts:
            lines.append(f"  [{sev.upper()}] {msg}")
    lines.append("")

    lines.append("=" * 60)
    lines.append("Source: ~/Documents/daily_status_report.py "
                 "(dashboard.gather_state + 24h delta)")
    return subject, "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# SMTP send
# ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str, env: Dict[str, str]) -> None:
    msg = EmailMessage()
    msg["From"] = env["GMAIL_USER"]
    msg["To"] = env["GMAIL_TO"]
    msg["Subject"] = subject
    msg.set_content(body)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as s:
        s.login(env["GMAIL_USER"], env["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


# ─────────────────────────────────────────────────────────────────────
# Time-of-day gating (--scheduled mode)
# ─────────────────────────────────────────────────────────────────────

def _in_send_window() -> bool:
    now_pt = datetime.now(PT)
    return now_pt.hour == SEND_HOUR_PT and now_pt.minute < SEND_WINDOW_MIN


def _already_sent_today() -> bool:
    if not MARKER_FILE.exists():
        return False
    try:
        last = MARKER_FILE.read_text().strip()
    except OSError:
        return False
    today_pt = datetime.now(PT).strftime("%Y-%m-%d")
    return last == today_pt


def _mark_sent() -> None:
    MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    today_pt = datetime.now(PT).strftime("%Y-%m-%d")
    MARKER_FILE.write_text(today_pt + "\n")


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-send", action="store_true",
                    help="Render report to stdout, do not send email.")
    ap.add_argument("--scheduled", action="store_true",
                    help="Only send if in 08:00 PT window AND not yet sent today. "
                         "Used by the scheduler to gate time-of-day on an "
                         "otherwise interval-based scheduler.")
    args = ap.parse_args()

    if args.scheduled:
        if not _in_send_window():
            # Silent no-op — scheduler hits this every 5 min
            return 0
        if _already_sent_today():
            return 0

    state = shadow_dashboard.gather_state()
    subject, body = render_report(state)

    if args.no_send:
        print(subject)
        print()
        print(body)
        return 0

    env = _load_smtp_env()
    try:
        send_email(subject, body, env)
    except (smtplib.SMTPException, OSError) as e:
        # Print to stderr; scheduler will capture in scheduler_logs/<name>.log
        print(f"ERROR sending daily report: {e}", file=sys.stderr)
        return 1

    if args.scheduled:
        _mark_sent()

    print(f"sent: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
