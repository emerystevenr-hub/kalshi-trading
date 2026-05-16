"""Polymarket WebSocket protocol probe.

Connects to Polymarket CLOB WS and tries 5 different subscribe message
formats with ONE token_id. Logs what the server does for each.

Whichever variant doesn't get disconnected is the correct protocol —
we update Engine 3 to match.

Run:
  python3 polymarket_ws_probe.py
  python3 polymarket_ws_probe.py --token <token_id>  # use specific token
"""

import argparse
import json
import sys
import time
import threading

import websocket
import requests


CANDIDATES = [
    # (label, ws_url, subscribe_payload)
    ("A: /ws/market + type=market + assets_ids",
     "wss://ws-subscriptions-clob.polymarket.com/ws/market",
     lambda tid: {"type": "market", "assets_ids": [tid]}),

    ("B: /ws/market + type=market + custom_feature_enabled",
     "wss://ws-subscriptions-clob.polymarket.com/ws/market",
     lambda tid: {"type": "market", "assets_ids": [tid], "custom_feature_enabled": True}),

    ("C: /ws/market + NO type (URL path implies channel)",
     "wss://ws-subscriptions-clob.polymarket.com/ws/market",
     lambda tid: {"assets_ids": [tid]}),

    ("D: /ws/ (no channel) + type=Market + markets field",
     "wss://ws-subscriptions-clob.polymarket.com/ws/",
     lambda tid: {"type": "Market", "markets": [tid]}),

    ("E: /ws/market + auth object + markets field",
     "wss://ws-subscriptions-clob.polymarket.com/ws/market",
     lambda tid: {"auth": {}, "type": "market", "markets": [tid]}),
]


def fetch_one_token_id():
    """Grab one valid token_id from an active Polymarket event via Gamma."""
    r = requests.get("https://gamma-api.polymarket.com/markets",
                     params={"active": "true", "closed": "false", "limit": 20,
                             "order": "volume24hr", "ascending": "false"},
                     timeout=15)
    r.raise_for_status()
    for m in r.json():
        cids = m.get("clobTokenIds")
        if isinstance(cids, str):
            try:
                cids = json.loads(cids)
            except Exception:
                continue
        if isinstance(cids, list) and cids:
            return str(cids[0])
    raise RuntimeError("no active markets with clobTokenIds found")


def probe(label, url, payload, wait_sec=8):
    """Open WS, send payload, listen for wait_sec seconds. Report what happened."""
    print(f"\n--- {label} ---")
    print(f"  URL: {url}")
    print(f"  Payload: {json.dumps(payload)[:120]}")

    state = {"messages": 0, "errors": [], "closed": False, "first_msg": None,
             "opened": False}

    def on_open(ws):
        state["opened"] = True
        print("  [open] connected, sending subscribe...")
        try:
            ws.send(json.dumps(payload))
            print("  [send] ok")
        except Exception as e:
            state["errors"].append(f"send: {e}")

    def on_message(ws, msg):
        state["messages"] += 1
        if state["first_msg"] is None:
            state["first_msg"] = msg[:160]

    def on_error(ws, err):
        state["errors"].append(str(err))

    def on_close(ws, code, msg):
        state["closed"] = True

    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message,
                                 on_error=on_error, on_close=on_close)
    t = threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 20})
    t.daemon = True
    t.start()

    # Send an application-level PING a few times to test if that's enough
    start = time.time()
    last_ping = start
    while time.time() - start < wait_sec:
        time.sleep(0.25)
        if state["closed"] or state["errors"]:
            break
        # App-level PING every 9s, per Polymarket docs
        if state["opened"] and time.time() - last_ping > 9:
            try:
                ws.send("PING")
            except Exception:
                pass
            last_ping = time.time()

    try:
        ws.close()
    except Exception:
        pass
    t.join(timeout=2)

    verdict = (
        "ALIVE (messages received)" if state["messages"] > 0
        else "ALIVE (no messages but stayed connected)" if not state["closed"] and not state["errors"]
        else "DISCONNECTED"
    )
    print(f"  VERDICT: {verdict}")
    print(f"    opened: {state['opened']}  messages: {state['messages']}  "
          f"closed: {state['closed']}")
    if state["first_msg"]:
        print(f"    first msg: {state['first_msg']}")
    if state["errors"]:
        print(f"    errors: {state['errors']}")
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=None, help="Specific token_id to probe with")
    ap.add_argument("--wait", type=int, default=8, help="Seconds to hold each connection")
    args = ap.parse_args()

    token = args.token or fetch_one_token_id()
    print(f"Probing Polymarket WS with token_id: {token}")
    print(f"Hold time per variant: {args.wait}s")

    results = []
    for label, url, payload_fn in CANDIDATES:
        verdict = probe(label, url, payload_fn(token), wait_sec=args.wait)
        results.append((label, verdict))
        time.sleep(2)

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    for label, verdict in results:
        print(f"  {verdict:<40s}  {label}")


if __name__ == "__main__":
    main()
