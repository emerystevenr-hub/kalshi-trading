# ECMWF Open Data — Signup Guide

**Purpose:** Access ECMWF HRES and AIFS forecast GRIB files for Terminal 1.
**Cost:** $0. Anonymous access, no account required for basic data.
**Time to complete:** 5 minutes.

---

## The Good News

ECMWF Open Data (launched 2022, fully open since 2024) requires **no account, no API key, no auth** for the core forecast products we need.

Data is served via HTTPS direct download from:
- Primary: `https://data.ecmwf.int/forecasts/`
- Mirror: `https://data-mirror.ecmwf.int/forecasts/`

## What You Actually Need to Do

### Step 1: Verify you can pull a sample file

Open Terminal and run:

```bash
curl -I "https://data.ecmwf.int/forecasts/20260421/00z/ifs/0p25/oper/20260421000000-6h-oper-fc.grib2"
```

Expected response: `HTTP/1.1 200 OK` with `Content-Length` header.

If this works, you're done. No signup needed for HRES or AIFS.

### Step 2: (Optional but recommended) Create an ECMWF account for ECMWF ENS ensemble data

We don't need this for Phase 1 launch (our 4 primary models don't include ENS). Skip unless we decide to add ensemble uncertainty bands in Phase 2.

If/when we need it:
1. Go to https://www.ecmwf.int/user
2. Click "Register"
3. Fill in: name, email, affiliation ("independent researcher" is fine)
4. Confirm email
5. Generate an API key at https://api.ecmwf.int/v1/key/
6. Save key to `~/.ecmwfapirc`:
   ```
   {
       "url": "https://api.ecmwf.int/v1",
       "key": "YOUR_KEY_HERE",
       "email": "emery.stevenr@gmail.com"
   }
   ```

## Products We'll Pull

| Product | Path | Size/run | Our use |
|---------|------|----------|---------|
| HRES deterministic | `/forecasts/{YYYYMMDD}/{HH}z/ifs/0p25/oper/` | ~200 MB | Primary ECMWF physics forecast |
| AIFS | `/forecasts/{YYYYMMDD}/{HH}z/aifs/0p25/oper/` | ~150 MB | AI-driven forecast |

Run times: 00Z and 12Z (twice daily).
Latency: typically 5–8 hours after run time (published around 05Z and 17Z UTC).

## Pull Schedule (Terminal 1)

```
06:00 UTC  (23:00 PT prev day) — ECMWF 00Z run publishes around this time
18:00 UTC  (11:00 PT same day) — ECMWF 12Z run publishes around this time
```

We retry with exponential backoff if the run hasn't published yet.

## Verification Command (run this now, before proceeding)

```bash
# Today's 00Z HRES forecast at T+24h
curl -o /tmp/ecmwf_test.grib2 \
  "https://data.ecmwf.int/forecasts/$(date -u +%Y%m%d)/00z/ifs/0p25/oper/$(date -u +%Y%m%d)000000-24h-oper-fc.grib2"

# Check size > 1 MB
ls -lh /tmp/ecmwf_test.grib2
```

If the file downloads successfully (≥ 100 MB), ECMWF access is verified. Report back yes/no.

---

## Action Required From You

**Run the verification command above and report back.**

If it succeeds, we're unblocked.
If it fails (404, timeout, etc.), paste the error and I'll diagnose — likely a date adjustment since the current UTC run may not have published yet.

## What I'm Doing in Parallel

While you verify ECMWF, I'm building:
1. `terminal1_backtest.py` — main backtest engine skeleton
2. `terminal1_kalshi_logger.py` — live data logger (starts accumulating real Kalshi prices today)
3. Model pull modules (`pull_gfs.py`, `pull_hrrr.py`, etc.)
4. NWS actuals puller

All free, all public APIs. No other credentials needed.
