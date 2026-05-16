# Deploy Engine 3 to fly.io — one-shot recipe

Get Engine 3 off your home IP forever. ~10 min setup, $0/mo, runs 24/7.

## Step 1: install flyctl (one time)

```
brew install flyctl
```

## Step 2: sign up + log in (one time)

```
fly auth signup    # opens browser; create account
                   # OR if you already have one:
fly auth login
```

Free tier requires a credit card on file but doesn't charge unless you exceed limits. Engine 3 is well within the free tier (1 shared-cpu VM, 256MB RAM).

## Step 3: launch the app

```
cd ~/Documents
fly launch --name polymarket-engine3 --region lhr --no-deploy
```

When prompted:
- "Would you like to set up a Postgres database?" → **No**
- "Would you like to set up an Upstash Redis database?" → **No**
- "Create .dockerignore from .gitignore?" → **No** (we already have one)
- Anything else → accept defaults

This generates the app on fly.io but doesn't deploy yet.

## Step 4: deploy

```
fly deploy
```

Builds the Docker image (uses `Dockerfile`), runs the test suite during build (so a broken engine never deploys), pushes to fly.io's London region, starts the engine.

## Step 5: watch logs

```
fly logs
```

You should see within 30-60 seconds:

```
[engine3/ws] initial universe scan via REST...
[engine3/ws] universe installed: <N> mutex-eligible events ...
[engine3/xvenue] 3 pairs configured ...
[engine3/ws] subscribed to <N> token_ids in 1 batches
[engine3/ws] HH:MM:SS  events=<N> tokens=<N> book_updates=<climbing> signals=<N>
```

Ctrl-C exits the log tail (engine keeps running).

## Day-to-day commands

| Action | Command |
|---|---|
| Check status | `fly status` |
| Tail logs | `fly logs` |
| Restart engine | `fly apps restart polymarket-engine3` |
| SSH into VM (rare) | `fly ssh console` |
| Stop engine (no charge while stopped) | `fly scale count 0` |
| Resume engine | `fly scale count 1` |
| Update engine code | edit `polymarket_engine3.py`, then `fly deploy` |

## Cost monitoring

`fly orgs show personal` shows your usage vs. free-tier limits. Engine 3 is far below the cap; no realistic scenario charges anything unless you scale to multiple instances.

## Troubleshooting

**"App polymarket-engine3 already exists"** — pick a different name in step 3.

**"Cannot find region lhr"** — try `fra` (Frankfurt), `ams` (Amsterdam), or `cdg` (Paris). All work for Polymarket — anywhere except US.

**Engine connects but disconnects after subscribe** — fly.io's data centers are not blocklisted by Polymarket, so this shouldn't happen. If it does, the issue is in our code; SSH in (`fly ssh console`) and inspect.

**Want to run from your Mac instead** — kill the deployment (`fly scale count 0`), run `python3 polymarket_engine3.py` locally as before. Hybrid is fine; no conflict.

## What this gives you

- Engine 3 running 24/7 from a London IP that Polymarket doesn't block
- No more home-IP rate limits from restart churn
- Logs available anytime via `fly logs`
- Engine restarts automatically on crash (fly.io handles process supervision)
- Free tier covers it indefinitely at this scale
- Update code: edit local file, `fly deploy`, done

## Stopping it forever

```
fly apps destroy polymarket-engine3
```

Removes the app entirely, no further charges.
