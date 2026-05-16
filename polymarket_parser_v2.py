import requests
import json

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE = "https://clob.polymarket.com"


def get_prices(token_ids):
    """Fetch buy prices for multiple tokens from CLOB API."""
    headers = {"Content-Type": "application/json"}
    payload = [{"token_id": tid, "side": "buy"} for tid in token_ids]
    resp = requests.post(f"{CLOB_BASE}/prices", json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def find_3way_events():
    print("Fetching events from Polymarket Gamma API...")

    # Fetch more events and also try with tag filter
    all_events = []

    # Batch 1: large general pull
    for offset in [0, 100, 200, 300, 400]:
        params = {"active": "true", "closed": "false", "limit": 100, "offset": offset}
        try:
            resp = requests.get(GAMMA_EVENTS_URL, params=params, timeout=15)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_events.extend(batch)
            print(f"  Fetched batch at offset {offset}: {len(batch)} events")
        except Exception as e:
            print(f"  Batch error at offset {offset}: {e}")
            break

    print(f"\nTotal events fetched: {len(all_events)}")

    # Strategy 1: Look for "vs" in title (soccer, MMA, etc.)
    vs_count = 0
    valid_groups = 0
    partial_count = 0

    # Strategy 2: Look for any event with exactly 3 markets including a "Draw" option
    threeway_no_vs = 0

    for event in all_events:
        title = event.get("title", "")
        markets = event.get("markets", [])

        # --- Strategy 1: vs-based parsing ---
        has_vs = " vs " in title or " vs. " in title
        if has_vs:
            vs_count += 1
            teams = title.replace(" vs. ", " vs ").split(" vs ")
            if len(teams) == 2:
                home_team, away_team = teams[0].strip(), teams[1].strip()
                parsed = try_parse_3way(markets, home_team, away_team)
                if parsed:
                    valid_groups += 1
                    print_match(title, event.get("id"), parsed)
                    fetch_and_check_prices(parsed)
                elif any(m.get("groupItemTitle") for m in markets):
                    partial_count += 1
                continue

        # --- Strategy 2: any 3-market event with Draw ---
        group_titles = [m.get("groupItemTitle", "").strip() for m in markets if m.get("groupItemTitle")]
        has_draw = any(g.lower() == "draw" for g in group_titles)
        non_draw = [g for g in group_titles if g.lower() != "draw"]

        if has_draw and len(non_draw) == 2:
            home_team, away_team = non_draw[0], non_draw[1]
            parsed = try_parse_3way(markets, home_team, away_team)
            if parsed:
                threeway_no_vs += 1
                print_match(title, event.get("id"), parsed, label="3-WAY (no vs)")
                fetch_and_check_prices(parsed)

    total_found = valid_groups + threeway_no_vs
    print(f"\n--- SUMMARY ---")
    print(f"Events scanned: {len(all_events)}")
    print(f"vs-titles found: {vs_count}")
    print(f"3-way from vs parsing: {valid_groups}")
    print(f"3-way from Draw detection: {threeway_no_vs}")
    print(f"Partial matches: {partial_count}")
    print(f"TOTAL 3-WAY MARKETS FOUND: {total_found}")


def try_parse_3way(markets, home_team, away_team):
    parsed = {}
    for m in markets:
        group_title = m.get("groupItemTitle", "").strip()
        clob_tokens = m.get("clobTokenIds", [])
        if isinstance(clob_tokens, str):
            clob_tokens = json.loads(clob_tokens)
        if not clob_tokens or len(clob_tokens) < 2:
            continue
        yes_token = clob_tokens[0]

        if group_title == home_team:
            parsed["home"] = {"name": home_team, "yes_token": yes_token}
        elif group_title == away_team:
            parsed["away"] = {"name": away_team, "yes_token": yes_token}
        elif group_title.lower() == "draw":
            parsed["draw"] = {"name": "Draw", "yes_token": yes_token}

    if "home" in parsed and "away" in parsed and "draw" in parsed:
        return parsed
    return None


def print_match(title, event_id, parsed, label="MATCH"):
    print(f"\n{label}: {title} (ID: {event_id})")
    print(f"  HOME ({parsed['home']['name']}): {parsed['home']['yes_token']}")
    print(f"  AWAY ({parsed['away']['name']}): {parsed['away']['yes_token']}")
    print(f"  DRAW: {parsed['draw']['yes_token']}")


def fetch_and_check_prices(parsed):
    token_ids = [
        parsed["home"]["yes_token"],
        parsed["away"]["yes_token"],
        parsed["draw"]["yes_token"],
    ]
    try:
        prices = get_prices(token_ids)
        print(f"  PRICES: {prices}")
        prob_sum = sum(float(p) for p in prices.values() if p)
        print(f"  IMPLIED PROB SUM: {prob_sum:.4f}")
        if prob_sum < 1.0:
            edge = (1.0 - prob_sum) * 100
            print(f"  >>> DUTCH OPPORTUNITY: {edge:.2f}% edge")
        else:
            print(f"  No edge (overround: {((prob_sum - 1.0) * 100):.2f}%)")
    except Exception as e:
        print(f"  CLOB price error: {e}")


if __name__ == "__main__":
    find_3way_events()
