"""Check if ADPMNUSNERSA pre-2022 == NPPTTL legacy series, and try a clean
no-COVID-aftermath window 2014-2019 for the legacy era stats.
"""
import csv
from datetime import date
from statistics import stdev, mean

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
adp_m  = load_fred("/tmp/adp_m.csv")
adp_m  = {d: v/1000.0 for d, v in adp_m.items()}
nppttl = load_fred("/tmp/nppttl.csv")

def mom(s):
    keys = sorted(s)
    out = {}
    for i in range(1, len(keys)):
        if (keys[i].year-keys[i-1].year)*12 + (keys[i].month-keys[i-1].month)==1:
            out[keys[i]] = s[keys[i]] - s[keys[i-1]]
    return out

nfp_mom    = mom(payems)
adp_m_mom  = mom(adp_m)
npp_mom    = mom(nppttl)

# Compare ADPMNUSNERSA m/m vs NPPTTL m/m for same months
print("=== ADPMNUSNERSA m/m vs NPPTTL m/m (overlap window) ===")
common = sorted(set(adp_m_mom) & set(npp_mom))
divergence = []
for d in common[-12:]:
    a = adp_m_mom[d]
    n = npp_mom[d]
    diff = a - n
    print(f"  {d}: ADPMNUSNERSA={a:+7.1f}K  NPPTTL={n:+7.1f}K  diff={diff:+6.1f}K")
diffs = [adp_m_mom[d] - npp_mom[d] for d in common]
print(f"  All overlap n={len(common)}: mean diff={mean(diffs):+.1f}K, σ={stdev(diffs):.1f}K")
print()

# CLEAN WINDOW: 2014-01 to 2019-12 — pre-COVID, post-recovery
# This is the cleanest "normal economy" sample for legacy methodology
CLEAN_START = date(2014, 1, 1)
CLEAN_END   = date(2019, 12, 1)

def residuals_clean(adp_series, name):
    out = []
    for d, nfp in nfp_mom.items():
        if not (CLEAN_START <= d <= CLEAN_END): continue
        if d not in adp_series: continue
        out.append((d, nfp, adp_series[d], nfp - adp_series[d]))
    diffs = [r[3] for r in out]
    if not diffs: return
    abs_diffs = sorted(abs(x) for x in diffs)
    print(f"=== Clean window 2014-2019 (n={len(diffs)}, no COVID) ===")
    print(f"  source: {name}")
    print(f"  σ (NFP - ADP):          {stdev(diffs):8.1f} K")
    print(f"  mean:                   {mean(diffs):+8.1f} K")
    print(f"  p50 |residual|:         {abs_diffs[len(abs_diffs)//2]:8.1f} K")
    print(f"  p90 |residual|:         {abs_diffs[int(len(abs_diffs)*0.9)]:8.1f} K")
    print(f"  p95 |residual|:         {abs_diffs[int(len(abs_diffs)*0.95)]:8.1f} K")

residuals_clean(adp_m_mom, "ADPMNUSNERSA")
print()
residuals_clean(npp_mom,   "NPPTTL")

# Stanford era ex-2022 (let new methodology bed in)
print()
print("=== STANFORD ERA, 2023-01 → present (excl. methodology bed-in) ===")
S_START = date(2023, 1, 1)
S_END   = date(2026, 4, 1)
out = []
for d, nfp in nfp_mom.items():
    if not (S_START <= d <= S_END): continue
    if d not in adp_m_mom: continue
    out.append((d, nfp, adp_m_mom[d], nfp - adp_m_mom[d]))
diffs = [r[3] for r in out]
abs_diffs = sorted(abs(x) for x in diffs)
print(f"  n={len(diffs)}")
print(f"  σ:              {stdev(diffs):8.1f} K")
print(f"  mean:           {mean(diffs):+8.1f} K")
print(f"  p50 |res|:      {abs_diffs[len(abs_diffs)//2]:8.1f} K")
print(f"  p90 |res|:      {abs_diffs[int(len(abs_diffs)*0.9)]:8.1f} K")

# Correlation as a sanity check — does ADP have any signal at all for NFP?
import math
def corr(xs, ys):
    n = len(xs)
    mx, my = mean(xs), mean(ys)
    num = sum((xs[i]-mx)*(ys[i]-my) for i in range(n))
    dx  = math.sqrt(sum((x-mx)**2 for x in xs))
    dy  = math.sqrt(sum((y-my)**2 for y in ys))
    return num / (dx*dy) if dx*dy > 0 else float('nan')

# Clean window correlation
clean_pairs = [(nfp_mom[d], adp_m_mom[d]) for d in nfp_mom
               if CLEAN_START <= d <= CLEAN_END and d in adp_m_mom]
nfps = [p[0] for p in clean_pairs]
adps = [p[1] for p in clean_pairs]
print()
print(f"=== CORRELATION ρ(NFP m/m, ADP m/m) ===")
print(f"  Legacy 2014-2019:  ρ = {corr(nfps, adps):.3f}  (n={len(clean_pairs)})")

stan_pairs = [(nfp_mom[d], adp_m_mom[d]) for d in nfp_mom
              if S_START <= d <= S_END and d in adp_m_mom]
nfps_s = [p[0] for p in stan_pairs]
adps_s = [p[1] for p in stan_pairs]
print(f"  Stanford 2023-now: ρ = {corr(nfps_s, adps_s):.3f}  (n={len(stan_pairs)})")

# Implication for trading: how much edge does ADP buy us?
# If we know ADP and need to predict NFP, the residual variance σ² is what we
# can't explain. Compare to σ on raw NFP m/m changes (the unconditional spread).
print()
print(f"=== σ comparison: conditional (residual) vs unconditional (NFP itself) ===")
nfp_window = [nfp_mom[d] for d in nfp_mom if CLEAN_START <= d <= CLEAN_END]
res_window = [nfp_mom[d] - adp_m_mom[d] for d in nfp_mom
              if CLEAN_START <= d <= CLEAN_END and d in adp_m_mom]
print(f"  Legacy 2014-2019:")
print(f"    σ(NFP m/m unconditional):        {stdev(nfp_window):.1f}K")
print(f"    σ(NFP - ADP residual):           {stdev(res_window):.1f}K")
print(f"    variance explained by ADP:       {1 - (stdev(res_window)/stdev(nfp_window))**2:.1%}")
print()
nfp_s = [nfp_mom[d] for d in nfp_mom if S_START <= d <= S_END]
res_s = [nfp_mom[d] - adp_m_mom[d] for d in nfp_mom
         if S_START <= d <= S_END and d in adp_m_mom]
print(f"  Stanford 2023-now:")
print(f"    σ(NFP m/m unconditional):        {stdev(nfp_s):.1f}K")
print(f"    σ(NFP - ADP residual):           {stdev(res_s):.1f}K")
print(f"    variance explained by ADP:       {1 - (stdev(res_s)/stdev(nfp_s))**2:.1%}")

