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


def test_soccer_parser():
    print("Fetching active events from Polymarket Gamma API...")

    params = {"active": "true", "closed": "false", "limit": 100}

    try:
        response = requests.get(GAMMA_EVENTS_URL, params=params, timeout=15)
        response.raise_for_status()
        events = response.json()
    except Exception as e:
        print(f"API Error: {e}")
        return

    print(f"Received {len(events)} events")

    vs_count = 0
    valid_groups = 0
    partial_count = 0

    for event in events:
        title = event.get("title", "")
        markets = event.get("markets", [])

        if " vs " not in title and " vs. " not in title:
            continue

        vs_count += 1

        teams = title.replace(" vs. ", " vs ").split(" vs ")
        if len(teams) != 2:
            continue

        home_team, away_team = teams[0].strip(), teams[1].strip()
        parsed_outcomes = {}

        for m in markets:
            group_title = m.get("groupItemTitle", "").strip()

            # CRITICAL FIX: clobTokenIds may come as a JSON string, not a list
            clob_tokens = m.get("clobTokenIds", [])
            if isinstance(clob_tokens, str):
                clob_tokens = json.loads(clob_tokens)

            if not clob_tokens or len(clob_tokens) < 2:
                continue

            yes_token = clob_tokens[0]

            if group_title == home_team:
                parsed_outcomes["home"] = {"name": home_team, "yes_token": yes_token}
            elif group_title == away_team:
                parsed_outcomes["away"] = {"name": away_team, "yes_token": yes_token}
            elif group_title.lower() == "draw":
                parsed_outcomes["draw"] = {"name": "Draw", "yes_token": yes_token}

        if "home" in parsed_outcomes and "away" in parsed_outcomes and "draw" in parsed_outcomes:
            valid_groups += 1
            print(f"\nMatch Found: {title} (ID: {event.get('id')})")
            print(f"  HOME ({parsed_outcomes['home']['name']}): {parsed_outcomes['home']['yes_token']}")
            print(f"  AWAY ({parsed_outcomes['away']['name']}): {parsed_outcomes['away']['yes_token']}")
            print(f"  DRAW: {parsed_outcomes['draw']['yes_token']}")

            # Fetch actual prices from CLOB
            token_ids = [
                parsed_outcomes["home"]["yes_token"],
                parsed_outcomes["away"]["yes_token"],
                parsed_outcomes["draw"]["yes_token"],
            ]
            try:
                prices = get_prices(token_ids)
                print(f"  PRICES: {prices}")

                # Check for Dutch opportunity (sum of implied probs)
                prob_sum = sum(float(p) for p in prices.values() if p)
                print(f"  IMPLIED PROB SUM: {prob_sum:.4f}")
                if prob_sum < 1.0:
                    print(f"  >>> DUTCH OPPORTUNITY: {((1.0 - prob_sum) * 100):.2f}% edge")
            except Exception as e:
                print(f"  CLOB price error: {e}")

            if valid_groups >= 5:
                break
        elif parsed_outcomes:
            partial_count += 1
            missing = [k for k in ["home", "away", "draw"] if k not in parsed_outcomes]
            print(f"\nPartial: {title} — have {list(parsed_outcomes.keys())}, missing {missing}")

    print(f"\nDone. Events: {len(events)} | vs-titles: {vs_count} | 3-way: {valid_groups} | partial: {partial_count}")


if __name__ == "__main__":
    test_soccer_parser()
