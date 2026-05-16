# Kalshi Thesis Factory — Review (2026-04-26 17:57 UTC)

Ranked thesis candidates from automated scan. Human approves before
any thesis enters `kalshi_catalyst.py`. Approval checklist per thesis:

1. Does the market title match the factory's auto-description?
2. Is our_probability defensible against a 30-second news check?
3. Are entry/target/stop bounds tight enough vs. the book?
4. Is the correlation_group correct, or should it roll up further?

## Scan stats

- scan time: 1073.4s
- raw markets seen: 754,526
- after filters: 140
- total candidates (pre-rank): 14
- kept after top-N per engine: 14
- resolution-window arb findings: 0

## Sub-engine: `calendar_fade` (12 candidates)

| # | Ticker | Implied YES | DTR | Vol/day | Edge | Entry (NO) | Target | Stop | Score |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | `KXKASHOUT-26APR-JUN01` | 57% | 35d | $72,570 | 0.285 | 0.470 | 0.570 | 0.260 | 0.101 |
| 2 | `KXFISAEXTEND-26APR-MAY01` | 58% | 5d | $7,575 | 0.292 | 0.450 | 0.550 | 0.240 | 0.075 |
| 3 | `KXHORMUZNORM-26MAR17-B260701` | 50% | 72d | $58,502 | 0.253 | 0.530 | 0.630 | 0.320 | 0.057 |
| 4 | `KXTRUMPCHINA-26-MAY15` | 62% | 19d | $11,703 | 0.315 | 0.440 | 0.540 | 0.230 | 0.056 |
| 5 | `KXKASHOUT-26APR-JUL01` | 67% | 65d | $23,546 | 0.370 | 0.380 | 0.480 | 0.170 | 0.055 |
| 6 | `KXVOTEHUBTRUMPUPDOWN-26APR30` | 50% | 4d | $5,138 | 0.247 | 0.540 | 0.640 | 0.330 | 0.049 |
| 7 | `KXHORMUZNORM-26MAR17-B260601` | 30% | 37d | $57,294 | 0.147 | 0.740 | 0.840 | 0.530 | 0.046 |
| 8 | `KXTRUMPCHINA-26-JUN01` | 73% | 36d | $5,175 | 0.430 | 0.310 | 0.410 | 0.100 | 0.030 |
| 9 | `KXDHSFUND-26JUN01` | 67% | 36d | $6,012 | 0.370 | 0.370 | 0.470 | 0.160 | 0.029 |
| 10 | `KXDHSFUND-26MAY15` | 46% | 19d | $5,808 | 0.228 | 0.580 | 0.680 | 0.370 | 0.024 |
| 11 | `KXLEAVEPOWELLGOV-26AUG01-JUN` | 50% | 35d | $7,655 | 0.247 | 0.540 | 0.640 | 0.330 | 0.024 |
| 12 | `KXKASHANNOUNCEOUT-26APR-JUN01` | 62% | 35d | $5,624 | 0.315 | 0.440 | 0.540 | 0.230 | 0.024 |

### Details

**#1 — Will Kash Patel leaves as FBI Director before Jun 1, 2026?**

- id: `calfade__kxkashout-26apr-jun01`
- ticker: `KXKASHOUT-26APR-JUN01`
- side: **NO** @ entry ≤ $0.470
- target: $0.570   stop: $0.260
- our_probability: 29%  (implied YES: 57%)
- correlation_group: `KXKASHOUT-26APR`
- size default: $75
- valid_until: 2026-06-01T03:59:00+00:00
- rationale: Calendar fade: market at 57% YES with 35 DTR. Base-rate prior suggests ~29%. Edge 0.285 in probability dollars. NO entry at $0.440.

**#2 — Will legislation that reauthorizes FISA Section 702 authority become law before May 1, 2026?**

- id: `calfade__kxfisaextend-26apr-may01`
- ticker: `KXFISAEXTEND-26APR-MAY01`
- side: **NO** @ entry ≤ $0.450
- target: $0.550   stop: $0.240
- our_probability: 29%  (implied YES: 58%)
- correlation_group: `KXFISAEXTEND-26APR`
- size default: $75
- valid_until: 2026-05-01T14:00:00+00:00
- rationale: Calendar fade: market at 58% YES with 5 DTR. Base-rate prior suggests ~29%. Edge 0.292 in probability dollars. NO entry at $0.420.

**#3 — Will the 7-day moving average of transit calls through the Strait of Hormuz as reported by the IMF PortWatch be above 60 before July 1, 2026?**

- id: `calfade__kxhormuznorm-26mar17-b260701`
- ticker: `KXHORMUZNORM-26MAR17-B260701`
- side: **NO** @ entry ≤ $0.530
- target: $0.630   stop: $0.320
- our_probability: 25%  (implied YES: 50%)
- correlation_group: `KXHORMUZNORM-26MAR17`
- size default: $75
- valid_until: 2026-07-07T13:59:00+00:00
- rationale: Calendar fade: market at 50% YES with 72 DTR. Base-rate prior suggests ~25%. Edge 0.253 in probability dollars. NO entry at $0.500.

**#4 — Will Donald Trump visit China before May 15, 2026?**

- id: `calfade__kxtrumpchina-26-may15`
- ticker: `KXTRUMPCHINA-26-MAY15`
- side: **NO** @ entry ≤ $0.440
- target: $0.540   stop: $0.230
- our_probability: 30%  (implied YES: 62%)
- correlation_group: `KXTRUMPCHINA-26`
- size default: $75
- valid_until: 2026-05-15T14:00:00+00:00
- rationale: Calendar fade: market at 62% YES with 19 DTR. Base-rate prior suggests ~30%. Edge 0.315 in probability dollars. NO entry at $0.410.

**#5 — Will Kash Patel leaves as FBI Director before Jul 1, 2026?**

- id: `calfade__kxkashout-26apr-jul01`
- ticker: `KXKASHOUT-26APR-JUL01`
- side: **NO** @ entry ≤ $0.380
- target: $0.480   stop: $0.170
- our_probability: 30%  (implied YES: 67%)
- correlation_group: `KXKASHOUT-26APR`
- size default: $75
- valid_until: 2026-07-01T03:59:00+00:00
- rationale: Calendar fade: market at 67% YES with 65 DTR. Base-rate prior suggests ~30%. Edge 0.370 in probability dollars. NO entry at $0.350.

**#6 — Will Donald Trump's approval rating be above 38.7% for Apr 30, 2026?**

- id: `calfade__kxvotehubtrumpupdown-26apr30`
- ticker: `KXVOTEHUBTRUMPUPDOWN-26APR30`
- side: **NO** @ entry ≤ $0.540
- target: $0.640   stop: $0.330
- our_probability: 25%  (implied YES: 50%)
- correlation_group: `KXVOTEHUBTRUMPUPDOWN-26APR30`
- size default: $75
- valid_until: 2026-05-01T03:59:00+00:00
- rationale: Calendar fade: market at 50% YES with 4 DTR. Base-rate prior suggests ~25%. Edge 0.247 in probability dollars. NO entry at $0.510.

**#7 — Will the 7-day moving average of transit calls through the Strait of Hormuz as reported by the IMF PortWatch be above 60 before June 1, 2026?**

- id: `calfade__kxhormuznorm-26mar17-b260601`
- ticker: `KXHORMUZNORM-26MAR17-B260601`
- side: **NO** @ entry ≤ $0.740
- target: $0.840   stop: $0.530
- our_probability: 15%  (implied YES: 30%)
- correlation_group: `KXHORMUZNORM-26MAR17`
- size default: $75
- valid_until: 2026-06-02T13:59:00+00:00
- rationale: Calendar fade: market at 30% YES with 37 DTR. Base-rate prior suggests ~15%. Edge 0.147 in probability dollars. NO entry at $0.710.

**#8 — Will Donald Trump visit China before Jun 1, 2026?**

- id: `calfade__kxtrumpchina-26-jun01`
- ticker: `KXTRUMPCHINA-26-JUN01`
- side: **NO** @ entry ≤ $0.310
- target: $0.410   stop: $0.100
- our_probability: 30%  (implied YES: 73%)
- correlation_group: `KXTRUMPCHINA-26`
- size default: $75
- valid_until: 2026-06-01T14:00:00+00:00
- rationale: Calendar fade: market at 73% YES with 36 DTR. Base-rate prior suggests ~30%. Edge 0.430 in probability dollars. NO entry at $0.280.

**#9 — Will DHS funding bill become law before Jun 1, 2026?**

- id: `calfade__kxdhsfund-26jun01`
- ticker: `KXDHSFUND-26JUN01`
- side: **NO** @ entry ≤ $0.370
- target: $0.470   stop: $0.160
- our_probability: 30%  (implied YES: 67%)
- correlation_group: `KXDHSFUND`
- size default: $75
- valid_until: 2026-06-01T14:00:00+00:00
- rationale: Calendar fade: market at 67% YES with 36 DTR. Base-rate prior suggests ~30%. Edge 0.370 in probability dollars. NO entry at $0.340.

**#10 — Will legislation that, upon becoming law, results in the Department of Homeland Security being funded at 12:01 AM ET the calendar day after enactment become law before May 15, 2026?**

- id: `calfade__kxdhsfund-26may15`
- ticker: `KXDHSFUND-26MAY15`
- side: **NO** @ entry ≤ $0.580
- target: $0.680   stop: $0.370
- our_probability: 23%  (implied YES: 46%)
- correlation_group: `KXDHSFUND`
- size default: $75
- valid_until: 2026-05-15T14:00:00+00:00
- rationale: Calendar fade: market at 46% YES with 19 DTR. Base-rate prior suggests ~23%. Edge 0.228 in probability dollars. NO entry at $0.550.

**#11 — Will Jerome Powell leave Member of the Board of Governors of the Federal Reserve System before Jun 1, 2026?**

- id: `calfade__kxleavepowellgov-26aug01-jun`
- ticker: `KXLEAVEPOWELLGOV-26AUG01-JUN`
- side: **NO** @ entry ≤ $0.540
- target: $0.640   stop: $0.330
- our_probability: 25%  (implied YES: 50%)
- correlation_group: `KXLEAVEPOWELLGOV-26AUG01`
- size default: $75
- valid_until: 2026-06-01T03:59:00+00:00
- rationale: Calendar fade: market at 50% YES with 35 DTR. Base-rate prior suggests ~25%. Edge 0.247 in probability dollars. NO entry at $0.510.

**#12 — Will Kash Patel announce their departure as FBI Director before Jun 1, 2026?**

- id: `calfade__kxkashannounceout-26apr-jun01`
- ticker: `KXKASHANNOUNCEOUT-26APR-JUN01`
- side: **NO** @ entry ≤ $0.440
- target: $0.540   stop: $0.230
- our_probability: 30%  (implied YES: 62%)
- correlation_group: `KXKASHANNOUNCEOUT-26APR`
- size default: $75
- valid_until: 2026-06-01T03:59:00+00:00
- rationale: Calendar fade: market at 62% YES with 35 DTR. Base-rate prior suggests ~30%. Edge 0.315 in probability dollars. NO entry at $0.410.

## Sub-engine: `tail_probability` (2 candidates)

| # | Ticker | Implied YES | DTR | Vol/day | Edge | Entry (NO) | Target | Stop | Score |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | `KXHORMUZNORM-26MAR17-B260601` | 30% | 37d | $57,294 | 0.192 | 0.740 | 0.890 | 0.510 | 0.060 |
| 2 | `KXLEAVEPOWELLGOV-26AUG01-JUN` | 50% | 35d | $7,655 | 0.322 | 0.540 | 0.690 | 0.310 | 0.031 |

### Details

**#1 — Will the 7-day moving average of transit calls through the Strait of Hormuz as reported by the IMF PortWatch be above 60 before June 1, 2026?**

- id: `tail__kxhormuznorm-26mar17-b260601`
- ticker: `KXHORMUZNORM-26MAR17-B260601`
- side: **NO** @ entry ≤ $0.740
- target: $0.890   stop: $0.510
- our_probability: 10%  (implied YES: 30%)
- correlation_group: `KXHORMUZNORM-26MAR17`
- size default: $50
- valid_until: 2026-06-02T13:59:00+00:00
- rationale: Tail fade: 30% YES on improbable long-dated event (37 DTR). Decay edge. Our prob 10%, edge 0.192. NO entry at $0.710.

**#2 — Will Jerome Powell leave Member of the Board of Governors of the Federal Reserve System before Jun 1, 2026?**

- id: `tail__kxleavepowellgov-26aug01-jun`
- ticker: `KXLEAVEPOWELLGOV-26AUG01-JUN`
- side: **NO** @ entry ≤ $0.540
- target: $0.690   stop: $0.310
- our_probability: 17%  (implied YES: 50%)
- correlation_group: `KXLEAVEPOWELLGOV-26AUG01`
- size default: $50
- valid_until: 2026-06-01T03:59:00+00:00
- rationale: Tail fade: 50% YES on improbable long-dated event (35 DTR). Decay edge. Our prob 17%, edge 0.322. NO entry at $0.510.

## Resolution-window arbitrage findings

_None detected this scan._

---

_Generated by `kalshi_thesis_factory.py`._