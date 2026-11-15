"""
cdsco_score_check.py
Tests WHICH parts of the risk score actually help to predict later failures.
It repeats the backtest for three different cut-off years and compares:
   - the full score (count + severity + recency + spread)
   - each part on its own, and a few simpler mixes
Two goals are tested:
   A) will the factory have ANY NSQ failure in the later years?
   B) will the factory have a SERIOUS failure (assay, sterility, identification) in the later years?
"lift 2.0" means: the top group fails again twice as often as the average scored factory.

Run:  py cdsco_score_check.py        (needs cdsco_analysis.py in the same folder)
"""
import sys

import numpy as np
import pandas as pd

from cdsco_analysis import (ROWS_FILE, MAP_FILE, categorize, parse_month, pct, score_factories, SERIOUS)
from cdsco_manufacturer_parser_v2 import OUT_DIR

OUT_XLSX = OUT_DIR / "score_variant_check.xlsx"
CUTOFFS = [2021, 2022, 2023]
# part -> column in the score table
PARTS = {"repeat": "nsq_incidents", "severity": "severity_share_shrunk",
         "recency": "recency_weighted_incidents", "spread": "spread_raw"}
VARIANTS = {
    "A full score (30/20/20/15)":        {"repeat": .30, "severity": .20, "recency": .20, "spread": .15},
    "B plain count only":                {"repeat": 1.0},
    "C recency only":                    {"recency": 1.0},
    "D count + recency (50/50)":         {"repeat": .5, "recency": .5},
    "E no severity (40/35/25)":          {"repeat": .40, "recency": .35, "spread": .25},
    "F severity only":                   {"severity": 1.0},
    "G count + severity (70/30)":        {"repeat": .7, "severity": .3},
}


def load_nsq():
    rows = pd.read_csv(ROWS_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    mp = pd.read_csv(MAP_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    rows["factory_id"] = mp.factory_id.values
    rows["month"], _ = parse_month(rows)
    nsq = rows[rows.category == "NSQ"].copy()
    nsq["serious"] = nsq["str_nsq_result"].map(categorize).isin(SERIOUS)
    nsq = nsq[nsq.month.notna()]
    if "str_reported_by_lab_or_state" not in nsq.columns:
        nsq["str_reported_by_lab_or_state"] = ""
    return nsq


def evaluate(nsq, cut):
    train = nsq[nsq.year.astype(int) <= cut]
    test = nsq[nsq.year.astype(int) > cut]
    if len(train) < 200 or len(test) < 100:
        return []
    sc = score_factories(train, pd.Timestamp(year=cut, month=12, day=1))
    any_fail = sc.index.isin(test.factory_id.unique())
    serious_fail = sc.index.isin(test.loc[test.serious, "factory_id"].unique())
    out = []
    for name, w in VARIANTS.items():
        s = sum(w[p] * pct(sc[PARTS[p]]) for p in w) / sum(w.values())
        s = s + 1e-6 * pct(sc.nsq_incidents)              # tie-break by count
        order = s.rank(ascending=False, method="first")
        for top in (0.10, 0.20):
            sel = (order <= max(1, round(top * len(sc)))).values
            out.append({"cutoff_year": cut, "variant": name, "top_share": f"top {int(top*100)}%",
                        "lift_any_failure": round(any_fail[sel].mean() / any_fail.mean(), 2),
                        "lift_serious_failure": round(serious_fail[sel].mean() / serious_fail.mean(), 2) if serious_fail.mean() > 0 else np.nan})
    return out


def main():
    nsq = load_nsq()
    res = []
    for c in CUTOFFS:
        res += evaluate(nsq, c)
    if not res:
        sys.exit("Not enough data for the backtests.")
    df = pd.DataFrame(res)
    avg = (df.groupby(["variant", "top_share"])[["lift_any_failure", "lift_serious_failure"]].mean().round(2).reset_index())
    pd.set_option("display.width", 200)
    print("\n--- AVERAGE LIFT over the cut-off years", CUTOFFS, "(higher is better) ---")
    for top in ("top 10%", "top 20%"):
        print(f"\n{top}:")
        print(avg[avg.top_share == top].drop(columns="top_share").sort_values("lift_any_failure", ascending=False).to_string(index=False))
    with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as xw:
        avg.to_excel(xw, sheet_name="average_lift", index=False)
        df.to_excel(xw, sheet_name="by_cutoff_year", index=False)
    print(f"\nWritten: {OUT_XLSX}")


if __name__ == "__main__":
    main()
