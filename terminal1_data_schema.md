# Terminal 1 — Canonical Data Schemas

**Purpose:** lock the output shapes so upstream loggers and downstream backtest / live trading all speak the same format. Breaks here cost days to chase down. Add a version bump before changing any field.

**Schema version:** `v1` (2026-04-22)

---

## 1. Kalshi Market Snapshot (`kalshi_{station}_{YYYY-MM-DD}.jsonl`)

Written by `terminal1_kalshi_logger.py`. One JSON object per line. One line = one market observed at one timestamp.

| Field | Type | Units | Notes |
|-------|------|-------|-------|
| `ts_utc` | string | ISO 8601 | Snapshot timestamp. Always UTC, always tzinfo-aware. |
| `station` | string | — | One of `"NYC"`, `"ORD"`, `"LAX"`. Matched by `_match_station()` from ticker/title. |
| `ticker` | string | — | Full Kalshi market ticker, e.g., `KXHIGHNY-26APR22-B72`. |
| `title` | string | — | Human-readable market title. |
| `event_ticker` | string | — | Parent event grouping (day × station). |
| `close_time` | string | ISO 8601 | When the market stops trading. |
| `expiration_time` | string | ISO 8601 | When the market resolves. |
| `yes_bid` | float | USD | Best bid on YES side (e.g., 0.42). `null` if no bid. |
| `yes_ask` | float | USD | Best ask on YES side. `null` if no ask. |
| `no_bid` | float | USD | Best bid on NO side. |
| `no_ask` | float | USD | Best ask on NO side. |
| `last_price` | float | USD | Last trade price. |
| `volume` | int | contracts | Total lifetime volume on this market. |
| `volume_24h` | float | USD | Rolling 24-hour dollar volume. |
| `open_interest` | int | contracts | Current open interest. |
| `book` | object / null | — | Full ladder. See §1.1. `null` if fetch failed. |

### 1.1 Book Sub-schema

The `book` object mirrors Kalshi's `/orderbook` response:

```json
{
  "yes": [[price_int_cents, qty], ...],   // list of [price, qty] pairs, ascending price
  "no":  [[price_int_cents, qty], ...]    // list of [price, qty] pairs, ascending price
}
```

**Note: Kalshi uses integer cents in book ladder.** `yes[0] = [42, 100]` means best bid is $0.42 for 100 contracts. Normalize to dollars in analysis code.

### 1.2 Sample record

```json
{
  "ts_utc": "2026-04-22T15:00:12.441+00:00",
  "station": "NYC",
  "ticker": "KXHIGHNY-26APR22-B72",
  "title": "Will NYC's high on Apr 22 be 72°F?",
  "event_ticker": "KXHIGHNY-26APR22",
  "close_time": "2026-04-22T23:59:00+00:00",
  "expiration_time": "2026-04-23T04:00:00+00:00",
  "yes_bid": 0.18, "yes_ask": 0.21,
  "no_bid": 0.79,  "no_ask": 0.82,
  "last_price": 0.20,
  "volume": 450, "volume_24h": 89.0, "open_interest": 215,
  "book": {"yes": [[18, 50], [17, 100]], "no": [[79, 80], [78, 120]]}
}
```

---

## 2. Forecast Record (`forecasts_{model}_{station}.jsonl`)

Written by each model puller (`pull_gfs.py`, `pull_ecmwf.py`, etc.). Canonical format across all 4 models.

| Field | Type | Units | Notes |
|-------|------|-------|-------|
| `station` | string | — | `"NYC"`, `"ORD"`, `"LAX"`. |
| `model` | string | — | `"gfs"`, `"ecmwf_hres"`, `"hrrr"`, `"aifs"`. |
| `run_time_utc` | string | ISO 8601 | Model cycle start (e.g., `"2026-04-22T00:00:00+00:00"` for 00Z run). |
| `valid_time_utc` | string | ISO 8601 | Forecast target timestamp. |
| `lead_hours` | int | hours | `(valid_time − run_time)`. |
| `target_date_local` | string | YYYY-MM-DD | Local calendar date at station (for matching to Kalshi daily markets). |
| `temp_f` | float | °F | Forecast temperature at 2m above ground. |
| `temp_c` | float | °C | Same, in Celsius. |
| `daily_high_f` | float / null | °F | Max forecast temp for the local day (if this record represents a daily agg). |
| `daily_low_f` | float / null | °F | Min forecast temp for the local day. |
| `grib_filename` | string | — | Source GRIB filename, for traceability. |
| `pulled_ts_utc` | string | ISO 8601 | When we pulled this forecast. |

**Convention:** hourly forecasts at individual valid times use `temp_f` and leave `daily_high_f` / `daily_low_f` null. Daily aggregates (for scoring against Kalshi markets) have all three populated.

---

## 3. NWS Actuals (`nws_actuals_{station}.jsonl`)

Written by NWS puller. Ground-truth daily high/low observations.

| Field | Type | Units | Notes |
|-------|------|-------|-------|
| `station` | string | — | Station code. |
| `station_icao` | string | — | NWS ICAO code (`KNYC`, `KORD`, `KLAX`). |
| `date_local` | string | YYYY-MM-DD | Local calendar date. |
| `high_f` | float | °F | Observed daily high. |
| `low_f` | float | °F | Observed daily low. |
| `source` | string | — | `"NWS_DAILY"` or `"METAR_AGG"` (aggregated from hourly). |
| `observed_ts_utc` | string | ISO 8601 | When NWS published the final obs. |

---

## 4. Backtest Trade Record (`backtest_trades.jsonl`)

Written by `terminal1_backtest.py`. One line = one hypothetical trade fired during historical replay.

| Field | Type | Units | Notes |
|-------|------|-------|-------|
| `backtest_id` | string | — | UUID of the backtest run. |
| `signal_ts_utc` | string | ISO 8601 | When the edge gate fired. |
| `station` | string | — | |
| `ticker` | string | — | Kalshi market targeted. |
| `strike_bucket` | string | — | e.g., `"B72"` or range `"70-74"`. |
| `side` | string | — | `"YES"` or `"NO"`. |
| `model_p` | float | probability | Ensemble probability estimate. |
| `market_p` | float | probability | Market-implied (ask-side) probability. For Track A this is climatology; Track B uses logged Kalshi. |
| `fee_cents` | float | USD | Per-contract taker fee. |
| `edge_cents` | float | USD | `model_p − market_p − fee_cents`. |
| `contracts` | int | — | Quarter-Kelly-sized position. |
| `entry_cost_usd` | float | USD | `contracts × market_p`. |
| `outcome` | string | — | `"WIN"`, `"LOSS"`, or `"PENDING"`. |
| `payoff_usd` | float | USD | `contracts × 1.0` if WIN, else 0. |
| `realized_pnl_usd` | float | USD | `payoff − entry_cost − total_fees`. |
| `actual_high_f` / `actual_low_f` | float | °F | Ground truth from NWS. |

---

## 5. Phase 1 Report (`terminal1_phase1_report.json`)

Final deliverable. Matches Steve's requested ranking:

```json
{
  "run_id": "uuid",
  "generated_ts_utc": "...",
  "backtest_window": {"start": "2026-01-22", "end": "2026-04-22"},
  "config": {
    "min_edge_cents": 0.05,
    "min_model_agreement": 3,
    "max_lead_hours": 72,
    "stations": ["NYC", "ORD", "LAX"],
    "models": ["gfs", "ecmwf_hres", "hrrr", "aifs"]
  },
  "by_station": [
    {
      "station": "NYC",
      "edge_frequency": 0.18,          // fraction of markets where edge gate fired
      "total_signals": 156,
      "hit_rate": 0.64,                // fraction of wins
      "avg_expected_edge_cents": 0.082,
      "realized_pnl_by_bucket": {
        "0.02_0.05": {"n": 40, "pnl_usd": 12.40, "sharpe": 0.8},
        "0.05_0.08": {"n": 62, "pnl_usd": 38.10, "sharpe": 1.4},
        "0.08_0.12": {"n": 38, "pnl_usd": 31.20, "sharpe": 1.9},
        "0.12_plus": {"n": 16, "pnl_usd": 18.80, "sharpe": 2.3}
      },
      "total_pnl_usd": 100.50,
      "sharpe": 1.6,
      "max_drawdown_usd": -8.20,
      "calibration_mae": 0.04
    },
    {"station": "ORD", "...": "..."},
    {"station": "LAX", "...": "..."}
  ],
  "aggregate": {
    "total_pnl_usd": 312.40,
    "sharpe": 1.7,
    "max_drawdown_usd": -22.10,
    "concentration_check": {
      "top_station_pct_of_pnl": 0.42,   // NYC contributes 42% → not overly concentrated
      "verdict": "edge is broad"        // vs "edge is carried by NYC"
    }
  },
  "go_no_go": {
    "sharpe_pass": true,
    "calibration_pass": true,
    "ev_positive_pass": true,
    "recommendation": "PROCEED_TO_PHASE_2"
  }
}
```

---

## 6. Versioning Rules

- Any breaking change to a schema → bump `SCHEMA_VERSION` constant in the producer module and add a top-level `_schema_version` field to each JSONL record.
- Readers must check `_schema_version`; if missing, assume v1.
- Old data stays readable; never rewrite historical files.

**Current version across all schemas: `v1`.**
