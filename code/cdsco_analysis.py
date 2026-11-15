"""
cdsco_analysis.py   (Phase 5 + Phase 6 of the plan, first version)
Reads the files made by the parser and the matcher and produces:
  1. failure categories (assay, sterility, ...) from the test-result text
  2. Kaplan-Meier "time until the next failure" analysis + a chart (km_curves.png)
  3. a 0-100 risk score per factory (count 40% + recency 35% + spread 25%), with tiers
  4. a backtest: score built on OLD years only, checked against LATER years
  5. one Excel workbook (analysis_results.xlsx) with plain typed values, no formulas, no merged cells

One-time install:  py -m pip install lifelines matplotlib openpyxl
Run:               py cdsco_analysis.py
Needs, in the same folder: cdsco_manufacturer_parser_v2.py and manufacturer_split.py
"""
import re
import sys

import numpy as np
import pandas as pd

from cdsco_manufacturer_parser_v2 import OUT_DIR, PRODUCT_COL

ROWS_FILE = OUT_DIR / "rows_with_manufacturer_split.csv"
MAP_FILE = OUT_DIR / "row_to_factory.csv"
FT_FILE = OUT_DIR / "factory_table.csv"
OUT_XLSX = OUT_DIR / "analysis_results.xlsx"
OUT_PNG = OUT_DIR / "km_curves.png"

BACKTEST_YEARS_HELD_OUT = 3     # the last 3 years are the "future" the score must predict
RECENCY_HALF_LIFE_YEARS = 2.0   # an incident 2 years old counts half as much as one today
SHRINK_K = 5                    # how strongly small factories are pulled toward the average severity
WEIGHTS = {"repeat": 0.40, "recency": 0.35, "spread": 0.25}   # chosen with cdsco_score_check.py (variant E). Severity is shown as a column but NOT in the score.
SERIOUS = {"assay", "sterility", "identification"}

CATEGORY_RULES = [   # first match wins, serious ones first
    ("sterility", r"steril|endotoxin|pyrogen|bacterial|microbial|mould|mold|fung"),
    ("identification", r"identif"),
    ("assay", r"assay|potency|strength|content of|% of label|active ingredient"),
    ("dissolution", r"dissol"),
    ("disintegration", r"disintegr"),
    ("particulate", r"particul|foreign matter|visible"),
    ("description", r"descri|appear|colou?r|clarity|clear|turbid|discolo|cracked|broken|capping|sticky|mottl"),
    ("labelling", r"label|packaging|leaflet"),
    ("physicochemical", r"\bph\b|water|moisture|loss on drying|uniformity|weight|hardness|friab|related sub|impurit|volume|viscos|density"),
]


def categorize(text):
    t = str(text).lower()
    for cat, pat in CATEGORY_RULES:
        if re.search(pat, t):
            return cat
    return "other"


def parse_month(df):
    """Return the first day of the reporting month for every row, and say how each date was found."""
    def to_dt(s):
        try:
            return pd.to_datetime(s, errors="coerce", format="mixed", dayfirst=True)
        except TypeError:
            return pd.to_datetime(s, errors="coerce", dayfirst=True)
    d1 = to_dt(df["dt_reporting_month_year"]) if "dt_reporting_month_year" in df.columns else pd.Series(pd.NaT, index=df.index)
    lab = df["month_label"].astype(str) if "month_label" in df.columns else pd.Series("", index=df.index)
    txt = np.where(lab.str.contains(r"\d{4}"), lab, lab + " " + df["year"].astype(str))
    d2 = to_dt(pd.Series(txt, index=df.index))
    d3 = pd.to_datetime(df["year"].astype(str) + "-01-01", errors="coerce")
    out = d1.fillna(d2)
    src = np.where(d1.notna(), "dt_reporting_month_year", np.where(d2.notna(), "month_label+year", np.where(d3.notna(), "year_only", "none")))
    out = out.fillna(d3)
    return out.dt.to_period("M").dt.to_timestamp(), pd.Series(src, index=df.index)


# ------------------------------------------------------------------ Kaplan-Meier
def run_km(nsq, study_end):
    from lifelines import KaplanMeierFitter
    from lifelines.statistics import logrank_test
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    recs = []
    for f, d in nsq.groupby("factory_id"):
        months = sorted(d.month.dropna().unique())
        if not months:
            continue
        first = pd.Timestamp(months[0])
        first_serious = bool(d.loc[d.month == first, "serious"].any())
        if len(months) >= 2:
            dur = (months[1].year - first.year) * 12 + (months[1].month - first.month)
            ev = 1
        else:
            dur = (study_end.year - first.year) * 12 + (study_end.month - first.month)
            ev = 0
        recs.append({"factory_id": f, "first_failure_month": first.strftime("%Y-%m"), "first_was_serious": first_serious,
                     "months_to_next_failure_or_end": dur, "next_failure_seen": ev})
    km = pd.DataFrame(recs)
    km = km[km.months_to_next_failure_or_end >= 0]

    fig, ax = plt.subplots(figsize=(8, 5))
    rows = []
    for label, mask in (("All factories", km.index == km.index),
                        ("First failure serious", km.first_was_serious),
                        ("First failure not serious", ~km.first_was_serious)):
        sub = km[mask]
        if len(sub) < 5:
            continue
        k = KaplanMeierFitter().fit(sub.months_to_next_failure_or_end, sub.next_failure_seen, label=label)
        k.plot_survival_function(ax=ax, ci_show=(label == "All factories"))
        row = {"group": label, "factories": len(sub), "second_failure_seen": int(sub.next_failure_seen.sum()),
               "median_months_until_next_failure": k.median_survival_time_}
        for t in (6, 12, 24, 36):
            row[f"share_NOT_failed_again_by_{t}m"] = round(float(k.predict(t)), 3)
        rows.append(row)
    ax.set_xlabel("Months since a factory's first NSQ failure")
    ax.set_ylabel("Share of factories with NO second failure yet")
    ax.set_title("Time until a factory fails again (CDSCO NSQ alerts)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT_PNG, dpi=150)
    plt.close(fig)

    p = np.nan
    a, b = km[km.first_was_serious], km[~km.first_was_serious]
    if len(a) >= 5 and len(b) >= 5:
        p = logrank_test(a.months_to_next_failure_or_end, b.months_to_next_failure_or_end,
                         a.next_failure_seen, b.next_failure_seen).p_value
    return km, pd.DataFrame(rows), p


# ------------------------------------------------------------------ risk score
def pct(s):
    return s.rank(pct=True, method="average")


def score_factories(nsq, asof):
    g = nsq.groupby("factory_id")
    t = pd.DataFrame({
        "nsq_incidents": g.size(),
        "serious_incidents": g.serious.sum().astype(int),
        "distinct_years": g.year.nunique(),
        "distinct_reporting_places": g.str_reported_by_lab_or_state.nunique(),
    })
    age_years = (asof - nsq.month).dt.days.clip(lower=0) / 365.25
    nsq = nsq.assign(w=0.5 ** (age_years / RECENCY_HALF_LIFE_YEARS))
    t["recency_weighted_incidents"] = nsq.groupby("factory_id").w.sum()
    p0 = t.serious_incidents.sum() / max(t.nsq_incidents.sum(), 1)
    t["severity_share_shrunk"] = (t.serious_incidents + SHRINK_K * p0) / (t.nsq_incidents + SHRINK_K)
    t["spread_raw"] = t.distinct_years + t.distinct_reporting_places
    raw = (WEIGHTS["repeat"] * pct(t.nsq_incidents) + WEIGHTS["recency"] * pct(t.recency_weighted_incidents)
           + WEIGHTS["spread"] * pct(t.spread_raw))
    t["risk_score_0_100"] = (100 * raw / sum(WEIGHTS.values())).round(1)
    return t


def tier(score):
    q98, q90, q70 = score.quantile([0.98, 0.90, 0.70])
    return np.select([score >= q98, score >= q90, score >= q70], ["Critical", "High", "Watch"], "Low")


def backtest(nsq, study_end):
    last_year = int(nsq.year.astype(int).max())
    cut_year = last_year - BACKTEST_YEARS_HELD_OUT
    train = nsq[nsq.year.astype(int) <= cut_year]
    test = nsq[nsq.year.astype(int) > cut_year]
    if len(train) < 100 or len(test) < 50:
        return pd.DataFrame([{"note": "not enough rows to backtest"}]), cut_year
    asof = pd.Timestamp(year=cut_year, month=12, day=1)
    sc = score_factories(train, asof)
    sc["naive_count_score"] = sc.nsq_incidents
    test_by = test.groupby("factory_id").size()
    seen = test_by[test_by.index.isin(sc.index)]
    out = [{"item": "Training years", "value": f"up to {cut_year}"},
           {"item": "Test years", "value": f"{cut_year + 1} to {last_year}"},
           {"item": "Factories scored (had a failure in training years)", "value": len(sc)},
           {"item": "NSQ incidents in test years", "value": int(len(test))},
           {"item": "...of which from factories NOT seen in training years", "value": int(len(test) - seen.sum())}]
    base_hit = float((sc.index.isin(test_by.index)).mean())
    out.append({"item": "Share of scored factories that failed again in test years", "value": round(base_hit, 3)})
    for top in (0.10, 0.20):
        for name, col in (("risk score", "risk_score_0_100"), ("plain count only", "naive_count_score")):
            n = max(1, int(round(top * len(sc))))
            sel = sc.sort_values([col, "recency_weighted_incidents"], ascending=False).head(n)
            inc = int(test_by.reindex(sel.index).fillna(0).sum())
            hit = float(sel.index.isin(test_by.index).mean())
            out.append({"item": f"Top {int(top*100)}% by {name}: share of test-year incidents from scored factories",
                        "value": round(inc / max(seen.sum(), 1), 3)})
            out.append({"item": f"Top {int(top*100)}% by {name}: share of these factories that failed again",
                        "value": round(hit, 3)})
            out.append({"item": f"Top {int(top*100)}% by {name}: lift vs. all scored factories",
                        "value": round(hit / base_hit, 2) if base_hit else np.nan})
    return pd.DataFrame(out), cut_year


# ------------------------------------------------------------------ main
def main():
    for f in (ROWS_FILE, MAP_FILE, FT_FILE):
        if not f.exists():
            sys.exit(f"Not found: {f}\nRun the parser and the matcher first.")
    try:
        import lifelines, matplotlib, openpyxl  # noqa
    except ImportError:
        sys.exit("Missing a library. Run:  py -m pip install lifelines matplotlib openpyxl")

    rows = pd.read_csv(ROWS_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    mp = pd.read_csv(MAP_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if len(rows) != len(mp):
        sys.exit("rows_with_manufacturer_split.csv and row_to_factory.csv have different row counts. Re-run the matcher.")
    rows["factory_id"], rows["company_id"] = mp.factory_id.values, mp.company_id.values
    ft = pd.read_csv(FT_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    rows["month"], rows["date_source"] = parse_month(rows)
    nsq = rows[rows.category == "NSQ"].copy()
    nsq["failure_category"] = nsq["str_nsq_result"].map(categorize) if "str_nsq_result" in nsq.columns else "other"
    nsq["serious"] = nsq.failure_category.isin(SERIOUS)
    nsq = nsq[nsq.month.notna()]
    if "str_reported_by_lab_or_state" not in nsq.columns:
        nsq["str_reported_by_lab_or_state"] = ""
    study_end = nsq.month.max()

    print("\n--- DIAGNOSTICS (please send me these lines too) ---")
    print("Where the dates came from:", rows.loc[rows.category == "NSQ", "date_source"].value_counts().to_dict())
    print("Years in data:", nsq.year.value_counts().sort_index().to_dict())
    print("Failure categories:", nsq.failure_category.value_counts().to_dict())
    print(f"Serious share (assay + sterility + identification): {nsq.serious.mean():.1%}")

    km, km_sum, p = run_km(nsq, study_end)
    sc = score_factories(nsq, study_end)
    sc["risk_tier"] = tier(sc.risk_score_0_100)
    bt, cut_year = backtest(nsq, study_end)

    fac = ft[["factory_id", "company_id", "factory_name", "pin_example", "location_known", "spurious_incidents"]].merge(
        sc.reset_index(), on="factory_id", how="right").sort_values("risk_score_0_100", ascending=False)
    cats = nsq.groupby(["failure_category"]).size().rename("nsq_rows").reset_index().sort_values("nsq_rows", ascending=False)

    with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as xw:
        fac.to_excel(xw, sheet_name="factory_risk_scores", index=False)
        km_sum.to_excel(xw, sheet_name="survival_summary", index=False)
        km.to_excel(xw, sheet_name="survival_factory_data", index=False)
        bt.to_excel(xw, sheet_name="backtest", index=False)
        cats.to_excel(xw, sheet_name="failure_categories", index=False)

    print("\n--- SURVIVAL (time until a factory fails again) ---")
    print(km_sum.to_string(index=False))
    print(f"Log-rank test, serious vs not serious first failure: p = {p:.4f}" if p == p else "Log-rank test: not enough data")
    print("\n--- RISK TIERS ---")
    print(fac.risk_tier.value_counts().to_string())
    print("\nTop 10 by risk score:")
    print(fac[["factory_id", "factory_name", "nsq_incidents", "distinct_years", "serious_incidents", "risk_score_0_100", "risk_tier"]].head(10).to_string(index=False))
    print(f"\n--- BACKTEST (trained up to {cut_year}) ---")
    print(bt.to_string(index=False))
    print(f"\nWritten: {OUT_XLSX}\n         {OUT_PNG}")


if __name__ == "__main__":
    main()
