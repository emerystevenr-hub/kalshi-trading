"""Terminal 2 — Catalyst (manual) shadow trade logger.

T2 is a manual engine. You hand-pick catalyst event trades on Kalshi (or
elsewhere). This tool logs them to the unified shadow P&L ledger so you
track paper P&L across your whole portfolio.

Typical workflow:

    # Log a new position (interactive — prompts for each field):
    python3 ~/Documents/terminal2_catalyst.py open

    # Or one-shot with args:
    python3 ~/Documents/terminal2_catalyst.py open \\
        --ticker KXHIGHNYC-26APR24-B73.5 --side YES --price 0.45 \\
        --size 10 --reason "weather.com projects 75F, my model says 73F — taking YES under 50c"

    # See what T2 has open:
    python3 ~/Documents/terminal2_catalyst.py list

    # Settle a position when market resolves (interactive or with args):
    python3 ~/Documents/terminal2_catalyst.py close
    # or:
    python3 ~/Documents/terminal2_catalyst.py close --id 8f6abc123def --settle 1.00 --outcome win

    # Check P&L and bankroll state:
    python3 ~/Documents/terminal2_catalyst.py status

All trades write to ~/Documents/shadow_pnl/ledger.jsonl and are
reflected in the portfolio-wide view: python3 ~/Documents/shadow_pnl_core.py status
"""

import argparse
import sys
from pathlib import Path

# Same-dir import
sys.path.insert(0, str(Path.home() / "Documents"))
from shadow_pnl_core import ShadowLedger, compute_state  # noqa: E402


ENGINE_ID = "T2"


def _prompt(label: str, default: str = "", cast=str, required: bool = True):
    hint = f" [{default}]" if default else ""
    while True:
        v = input(f"{label}{hint}: ").strip()
        if not v:
            v = default
        if not v and required:
            print("  (required)")
            continue
        try:
            return cast(v) if v else v
        except (ValueError, TypeError) as e:
            print(f"  invalid ({e}), try again")


def cmd_open(args) -> int:
    sl = ShadowLedger()

    venue = args.venue or _prompt("venue", default="kalshi")
    ticker = args.ticker or _prompt("ticker (e.g. KXHIGHNYC-26APR24-B73.5)")
    side = args.side or _prompt("side (YES/NO)", default="YES").upper()
    price = args.price if args.price is not None else _prompt("price ($0.01..$0.99)", cast=float)
    size = args.size if args.size is not None else _prompt("size (contracts, integer)", cast=int)
    fee = args.fee if args.fee is not None else float(_prompt("fee USD (total, default 0)", default="0", cast=float))
    reason = args.reason or _prompt("reason (why are you taking this trade?)", required=False)

    # Validate current bankroll
    state = compute_state()
    s = state.get(ENGINE_ID)
    cost = round(price * size + fee, 4)
    if s and cost > s["current_balance_usd"]:
        print(f"\n⚠ WARNING: T2 current balance is ${s['current_balance_usd']:.2f}, "
              f"this trade costs ${cost:.2f}. Proceeding anyway — shadow mode.")
    else:
        print(f"\nCost: ${cost:.2f}  (T2 balance before: ${s['current_balance_usd']:.2f})")

    confirm = input("Confirm OPEN? (y/N): ").strip().lower()
    if confirm != "y":
        print("cancelled.")
        return 1

    pid = sl.open(
        engine=ENGINE_ID,
        venue=venue,
        ticker=ticker,
        side=side,
        price=price,
        size=size,
        fee_usd=fee,
        reason=reason,
    )
    print(f"\n✓ Opened position {pid}")
    print(f"  {ticker} {side} {size}@${price:.2f}  cost=${cost:.2f}")
    return 0


def cmd_close(args) -> int:
    sl = ShadowLedger()
    state = compute_state()
    t2 = state.get(ENGINE_ID)
    if not t2 or not t2["positions_open"]:
        print("No open T2 positions to close.")
        return 1

    position_id = args.id
    if not position_id:
        print("Open T2 positions:")
        for p in t2["positions_open"]:
            print(f"  {p['position_id']}  {p['ticker']:<40} {p['side']} "
                  f"{p['size']}@${p['price']:.2f}")
        position_id = _prompt("\nwhich position_id to close?").strip()

    settle = args.settle if args.settle is not None else _prompt(
        "settle price (1.00 = YES resolved true, 0.00 = false, or exit mkt px)",
        cast=float,
    )
    outcome = args.outcome or _prompt("outcome (win/loss/partial/refund)", default="win")
    fee = args.fee if args.fee is not None else float(_prompt("exit fee USD (default 0)", default="0", cast=float))

    row = sl.close(position_id=position_id, settle_price=settle, outcome=outcome, fee_usd=fee)
    print(f"\n✓ Closed {position_id}")
    print(f"  outcome={row['outcome']}  proceeds=${row['proceeds_usd']:.2f}  "
          f"realized P&L=${row['realized_pnl_usd']:+.2f}")
    return 0


def cmd_list(args) -> int:
    state = compute_state()
    s = state.get(ENGINE_ID)
    if not s:
        print(f"No T2 config found.")
        return 1
    print(f"T2 ({s['name']})  balance=${s['current_balance_usd']:.2f}  "
          f"realized=${s['realized_pnl_usd']:+.2f}  "
          f"tied_up=${s['cost_tied_up_usd']:.2f}  "
          f"open={s['n_open']}  W/L={s['n_closed_win']}/{s['n_closed_loss']}")
    if not s["positions_open"]:
        print("(no open positions)")
        return 0
    print()
    for p in s["positions_open"]:
        print(f"  {p['position_id']}  {p['ticker']:<40} {p['side']} "
              f"{p['size']}@${p['price']:.2f}  cost=${p['cost_usd']:.2f}  "
              f"opened={p['opened_ts'][:19]}")
        if p["reason"]:
            print(f"    reason: {p['reason'][:120]}")
    return 0


def cmd_status(args) -> int:
    # Delegate to core status — shows all engines for context
    import subprocess
    script = str(Path.home() / "Documents" / "shadow_pnl_core.py")
    subprocess.run([sys.executable, script, "status"], check=False)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Terminal 2 — Catalyst manual shadow trade logger")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("open", help="Log a new catalyst position")
    sp.add_argument("--venue", default=None)
    sp.add_argument("--ticker", default=None)
    sp.add_argument("--side", default=None, choices=["YES", "NO", "yes", "no"])
    sp.add_argument("--price", type=float, default=None, help="Entry price in dollars (0.01..0.99)")
    sp.add_argument("--size", type=int, default=None, help="Contracts")
    sp.add_argument("--fee", type=float, default=None, help="Total fee USD (default 0)")
    sp.add_argument("--reason", default=None)
    sp.set_defaults(func=cmd_open)

    sp = sub.add_parser("close", help="Settle / close a T2 position")
    sp.add_argument("--id", default=None, help="position_id to close")
    sp.add_argument("--settle", type=float, default=None, help="Settle price (1.0=YES win, 0.0=YES loss)")
    sp.add_argument("--outcome", default=None, choices=["win", "loss", "partial", "refund"])
    sp.add_argument("--fee", type=float, default=None, help="Exit fee USD")
    sp.set_defaults(func=cmd_close)

    sp = sub.add_parser("list", help="List T2 open positions + stats")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("status", help="Portfolio-wide status (all engines)")
    sp.set_defaults(func=cmd_status)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
