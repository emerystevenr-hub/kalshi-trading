"""ADP-NFP residual study.

Decision rule (per HANDOFF_2026-05-09_session2.md):
  If σ on (NFP - ADP) exceeds Kalshi NFP contract bin spacing, T8 collapses
  to T3b-with-extra-steps. Archive pre-deployment.

Data:
  PAYEMS         — BLS Total Nonfarm Payrolls (level, K), 1939+, current.
  USPRIV         — BLS Total Private Nonfarm (level, K), 1939+, current.
  ADPMNUSNERSA   — ADP National Employment Report (level, jobs), 2010+, current.
                   Legacy ADP/Moody's pre-Aug 2022; ADP-Stanford Aug 2022+.
  NPPTTL         — ADP Research Inst. legacy series (level, K), 2002-Apr to 2022-May.

Eras:
  legacy   : 2010-02 .. 2022-07  (149 months)
  stanford : 2022-08 .. present  (~45 months)
"""
import csv
from datetime import date
from statistics import stdev, mean, median, pstdev

def load_fred(path):
    out = {}
    with open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            d = date.fromisoformat(row["observation_date"])
            v = row[next(k for k in row if k != "observation_date")]
            if v in ("", ".", None): continue
            out[d] = float(v)
    return out

payems = load_fred("/tmp/payems.csv")
usp    = load_fred("/tmp/usp.csv")
adp    = load_fred("/tmp/adp_m.csv")
nppttl = load_fred("/tmp/nppttl.csv")

# Convert ADPMNUSNERSA to thousands (jobs / 1000)
adp = {d: v/1000.0 for d, v in adp.items()}

# Build month-over-month change series
def mom(series):
    keys = sorted(series.keys())
    out = {}
    for i in range(1, len(keys)):
        prev, cur = keys[i-1], keys[i]
        # Only consecutive months
        if (cur.year - prev.year)*12 + (cur.month - prev.month) == 1:
            out[cur] = series[cur] - series[prev]
    return out

nfp_mom    = mom(payems)
priv_mom   = mom(usp)
adp_mom    = mom(adp)
npp_mom    = mom(nppttl)

# Build residuals
def residuals(nfp_series, adp_series, start, end):
    """For each month in [start,end] where both exist, residual = nfp - adp."""
    rows = []
    for d, nfp in nfp_series.items():
        if d < start or d > end: continue
        if d not in adp_series: continue
        rows.append((d, nfp, adp_series[d], nfp - adp_series[d]))
    return rows

# Eras
LEGACY_START   = date(2010, 2, 1)
LEGACY_END     = date(2022, 7, 1)
STANFORD_START = date(2022, 8, 1)
COVID_START    = date(2020, 3, 1)
COVID_END      = date(2020, 12, 1)
TODAY = date(2026, 5, 9)

def stats(name, residuals_list, exclude_covid=True):
    if exclude_covid:
        residuals_list = [r for r in residuals_list
                          if not (COVID_START <= r[0] <= COVID_END)]
    diffs = [r[3] for r in residuals_list]
    if not diffs:
        return f"  {name}: NO DATA"
    s = stdev(diffs) if len(diffs) > 1 else float('nan')
    ps = pstdev(diffs) if len(diffs) > 1 else float('nan')
    m = mean(diffs)
    md = median(diffs)
    abs_diffs = sorted(abs(x) for x in diffs)
    p50 = abs_diffs[len(abs_diffs)//2]
    p90 = abs_diffs[int(len(abs_diffs)*0.90)]
    p95 = abs_diffs[int(len(abs_diffs)*0.95)]
    return (f"  {name} (n={len(diffs)}, COVID excluded={exclude_covid}):\n"
            f"    mean (NFP - ADP):       {m:+8.1f} K\n"
            f"    median:                 {md:+8.1f} K\n"
            f"    σ (sample):             {s:8.1f} K\n"
            f"    σ (population):         {ps:8.1f} K\n"
            f"    p50 |residual|:         {p50:8.1f} K\n"
            f"    p90 |residual|:         {p90:8.1f} K\n"
            f"    p95 |residual|:         {p95:8.1f} K")

print("=" * 78)
print(" ADP-NFP RESIDUAL STUDY (T8 GATE)")
print("=" * 78)
print(f" Data through: {sorted(payems.keys())[-1]} (NFP), "
      f"{sorted(adp.keys())[-1]} (ADP)")
print()
print(" PRIMARY: Kalshi NFP contracts settle on PAYEMS headline → (PAYEMS - ADP)")
print(" SECONDARY: USPRIV is the cleanest private-vs-private comparison")
print()

# PAYEMS vs ADP (the trade-relevant residual)
print(" === PAYEMS (headline NFP) − ADPMNUSNERSA ===")
res_legacy   = residuals(nfp_mom, adp_mom, LEGACY_START, LEGACY_END)
res_stanford = residuals(nfp_mom, adp_mom, STANFORD_START, TODAY)
print(stats("LEGACY ERA  2010-02 → 2022-07", res_legacy))
print()
print(stats("STANFORD ERA 2022-08 → present", res_stanford))
print()

# USPRIV vs ADP (private-vs-private)
print(" === USPRIV (BLS private NFP) − ADPMNUSNERSA ===")
res_legacy_p   = residuals(priv_mom, adp_mom, LEGACY_START, LEGACY_END)
res_stanford_p = residuals(priv_mom, adp_mom, STANFORD_START, TODAY)
print(stats("LEGACY ERA", res_legacy_p))
print()
print(stats("STANFORD ERA", res_stanford_p))
print()

# Headline: σ trends
print("=" * 78)
print(" HEADLINE")
print("=" * 78)
def get_sigma(rl, exclude_covid=True):
    if exclude_covid:
        rl = [r for r in rl if not (COVID_START <= r[0] <= COVID_END)]
    diffs = [r[3] for r in rl]
    return stdev(diffs)

s_legacy_h   = get_sigma(res_legacy)
s_stanford_h = get_sigma(res_stanford)
s_legacy_p   = get_sigma(res_legacy_p)
s_stanford_p = get_sigma(res_stanford_p)

print(f" σ on (PAYEMS - ADP) — LEGACY:    {s_legacy_h:.1f}K")
print(f" σ on (PAYEMS - ADP) — STANFORD:  {s_stanford_h:.1f}K  "
      f"(Δ = {s_stanford_h - s_legacy_h:+.1f}K vs legacy)")
print(f" σ on (USPRIV - ADP) — LEGACY:    {s_legacy_p:.1f}K")
print(f" σ on (USPRIV - ADP) — STANFORD:  {s_stanford_p:.1f}K  "
      f"(Δ = {s_stanford_p - s_legacy_p:+.1f}K vs legacy)")
print()

# Decision against typical Kalshi bin spacing
print("=" * 78)
print(" DECISION RULE")
print("=" * 78)
print(" Kalshi NFP markets historically use 25K or 50K bin spacing.")
print(" (Verify exact spacing on next NFP market — KXNFPJOBS-26MAY etc.)")
print()
for bin_K in (25, 50):
    print(f" Bin spacing = {bin_K}K:")
    print(f"   σ_stanford / bin = {s_stanford_h / bin_K:.2f}x  "
          f"({'PASS' if s_stanford_h <= bin_K else 'FAIL — σ exceeds bin spacing'})")
print()

# Recent residuals — is the relationship stable?
print("=" * 78)
print(" LAST 12 RESIDUALS (PAYEMS - ADP, recent first)")
print("=" * 78)
recent = sorted(res_stanford, reverse=True)[:12]
for d, nfp, a, r in recent:
    print(f"  {d}   NFP={nfp:+7.1f}K  ADP={a:+7.1f}K  residual={r:+7.1f}K")

