"""
cdsco_package.py   (Phase 8 of the plan: Steps 23, 24 and 25)  - the LAST step

Collects everything the other scripts made into one folder, E:\\cdsco_mqri\\final_package :
  CDSCO_NSQ_Project_Workbook.xlsx   all tables, plain values, no formulas, no merged cells
  charts\\                          6 PNG charts
  README.md                         the 3 headline findings + how to read the files (numbers are filled in automatically)
  METHODS_NOTE.md                   data sources, cleaning, matching, models, limits, wording rules
  code\\                            copies of all your scripts (for GitHub)
  integrity_checks (a sheet in the workbook and printed on screen)

Run:  py cdsco_package.py      (run it AFTER the other scripts; it skips anything that is missing and tells you)
One-time install if needed:  py -m pip install matplotlib openpyxl lifelines
"""
import shutil
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from cdsco_manufacturer_parser_v2 import OUT_DIR, NSQ_FILE, SPURIOUS_FILE, BASE

PKG = BASE / "final_package"
CH = PKG / "charts"
CODE = PKG / "code"
WB = PKG / "CDSCO_NSQ_Project_Workbook.xlsx"
SCRIPTS = ["manufacturer_split.py", "cdsco_manufacturer_parser_v2.py", "cdsco_factory_matcher.py", "cdsco_analysis.py",
           "cdsco_count_model.py", "cdsco_score_check.py", "cdsco_hotspots.py", "cdsco_package.py",
           "cdsco_alerts_downloader.py", "cdsco_nsq_portal_downloader.py"]


def rd(name, sheet=None, folder=OUT_DIR):
    f = folder / name
    if not f.exists():
        print(f"  (missing, skipped: {name})")
        return None
    try:
        if name.endswith(".xlsx"):
            return pd.read_excel(f, sheet_name=sheet)
        return pd.read_csv(f, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except Exception as e:                                   # noqa
        print(f"  (could not read {name}{' / ' + sheet if sheet else ''}: {e})")
        return None


def num(s):
    return pd.to_numeric(s, errors="coerce")


def pc(x, d=0):
    return f"{100 * x:.{d}f}%"


def main():
    PKG.mkdir(exist_ok=True); CH.mkdir(exist_ok=True); CODE.mkdir(exist_ok=True)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("Run:  py -m pip install matplotlib openpyxl lifelines")

    print("Reading files ...")
    ok = rd("rows_with_manufacturer_split.csv"); rev = rd("manufacturer_needs_review.csv"); nom = rd("rows_no_manufacturer.csv")
    r2f = rd("row_to_factory.csv"); ft = rd("factory_table.csv"); chk = rd("match_check_sample.csv")
    if ok is None or ft is None or r2f is None:
        sys.exit("The parser and matcher outputs are missing. Run cdsco_manufacturer_parser_v2.py and cdsco_factory_matcher.py first.")
    sc = rd("analysis_results.xlsx", "factory_risk_scores"); sv = rd("analysis_results.xlsx", "survival_summary")
    svf = rd("analysis_results.xlsx", "survival_factory_data"); bt = rd("analysis_results.xlsx", "backtest")
    fc = rd("analysis_results.xlsx", "failure_categories")
    cm = rd("count_model_results.xlsx", "model_results"); cmc = rd("count_model_results.xlsx", "model_comparison")
    hd = rd("hotspot_results.xlsx", "district_hotspots"); hs = rd("hotspot_results.xlsx", "state_rollup")

    for c in ("nsq_incidents", "spurious_incidents", "nsq_distinct_years", "nsq_distinct_products"):
        if c in ft.columns:
            ft[c] = num(ft[c]).fillna(0).astype(int)
    nsq_ok = ok[ok.category == "NSQ"]
    years = sorted(y for y in nsq_ok.year.unique() if y)

    # ------------------------------------------------------------------ numbers for the write-up
    n_fac, n_comp = len(ft), ft.company_id.nunique()
    rep2 = int((ft.nsq_distinct_years >= 2).sum()); rep3 = int((ft.nsq_incidents >= 3).sum())
    multi_var = int((num(ft.name_variants_count) > 1).sum()) if "name_variants_count" in ft.columns else None
    top5 = ft.sort_values("nsq_incidents", ascending=False).head(5)

    km_line, p_sev = None, None
    if sv is not None and len(sv):
        a = sv[sv.group == "All factories"].iloc[0]
        km_line = dict(n=int(a.factories), second=int(a.second_failure_seen), med=a.median_months_until_next_failure,
                       s12=1 - a.share_NOT_failed_again_by_12m, s24=1 - a.share_NOT_failed_again_by_24m, s36=1 - a.share_NOT_failed_again_by_36m)
    if svf is not None and len(svf):
        try:
            from lifelines.statistics import logrank_test
            s_ = svf[svf.first_was_serious.astype(str) == "True"]; n_ = svf[svf.first_was_serious.astype(str) != "True"]
            p_sev = logrank_test(s_.months_to_next_failure_or_end, n_.months_to_next_failure_or_end,
                                 s_.next_failure_seen, n_.next_failure_seen).p_value
        except Exception:                                      # noqa
            pass
    bt_d = {}
    if bt is not None and "item" in bt.columns:
        bt_d = dict(zip(bt["item"].astype(str), bt["value"]))
    def btv(key):
        for k, v in bt_d.items():
            if key in k:
                return v
        return None
    base_hit = btv("Share of scored factories that failed again")
    top10_hit = btv("Top 10% by risk score: share of these factories that failed again")
    top10_lift = btv("Top 10% by risk score: lift")
    cnt10_lift = btv("Top 10% by plain count only: lift")
    cut_info = f"{btv('Training years')} -> {btv('Test years')}"

    prec = None
    if chk is not None and "your_check_SAME_or_DIFFERENT" in chk.columns:
        v = chk["your_check_SAME_or_DIFFERENT"].astype(str).str.strip().str.upper()
        same, diff = int((v == "SAME").sum()), int((v == "DIFFERENT").sum())
        if same + diff > 0:
            prec = (same, same + diff)

    cm_lines = []
    if cm is not None and len(cm):
        cmx = cm.copy()
        cmx["p_value"] = num(cmx.p_value)
        for _, r in cmx[cmx.p_value < 0.05].sort_values("rate_ratio", ascending=False).iterrows():
            cm_lines.append(f"- {r.feature}: {r.rate_ratio:.2f}x (95% range {r.rate_ratio_low_95:.2f} to {r.rate_ratio_high_95:.2f})")
    hot_lines, hot_n = [], 0
    if hd is not None and len(hd):
        hdx = hd.copy()
        hot = hdx[(hdx.hotspot == "Hot spot (95%)") & (num(hdx.repeat_failure_factories) > 0)]
        hot_n = len(hot)
        for _, r in hdx.sort_values("repeat_failure_factories", ascending=False).head(6).iterrows():
            hot_lines.append(f"- {str(r.District).title()}, {str(r.State).title()}: {int(r.repeat_failure_factories)} repeat-failure factories, "
                             f"{int(r.nsq_incidents)} NSQ incidents ({r.hotspot})")

    # ------------------------------------------------------------------ charts
    print("Drawing charts ...")
    foot = "Raw counts from public CDSCO regulatory records; no samples-tested denominator."
    t30 = ft.sort_values("nsq_incidents", ascending=False).head(30).iloc[::-1]
    fig, ax = plt.subplots(figsize=(9, 9))
    ax.barh([str(n)[:42] for n in t30.factory_name], t30.nsq_incidents, color="#4c78a8")
    ax.set_xlabel("NSQ incidents"); ax.set_title("Top 30 factories by number of NSQ incidents"); fig.text(0.01, 0.005, foot, fontsize=7)
    fig.tight_layout(rect=(0, 0.02, 1, 1)); fig.savefig(CH / "1_top30_factories.png", dpi=150); plt.close(fig)

    ct = pd.crosstab(nsq_ok.year, nsq_ok.source_clean)
    fig, ax = plt.subplots(figsize=(8, 5)); ct.plot(kind="bar", stacked=True, ax=ax, color=["#4c78a8", "#f2a541", "#bbbbbb"][:ct.shape[1]])
    ax.set_title("NSQ alerts per year by who reported them"); ax.set_xlabel(""); ax.set_ylabel("NSQ alert rows")
    fig.text(0.01, 0.005, "State-lab alerts grew sharply from 2024-25: read this as more reporting, not as a change in quality. 2026 is a partial year.", fontsize=7)
    fig.tight_layout(rect=(0, 0.03, 1, 1)); fig.savefig(CH / "2_alerts_by_year_and_source.png", dpi=150); plt.close(fig)

    if fc is not None and len(fc):
        f2 = fc.sort_values("nsq_rows")
        fig, ax = plt.subplots(figsize=(8, 5)); ax.barh(f2.failure_category, f2.nsq_rows, color="#59a14f")
        ax.set_title("Why samples failed (grouped from the test-result text)"); ax.set_xlabel("NSQ alert rows")
        fig.tight_layout(); fig.savefig(CH / "3_failure_categories.png", dpi=150); plt.close(fig)
    if sc is not None and "risk_tier" in sc.columns:
        order = ["Critical", "High", "Watch", "Low"]; tc = sc.risk_tier.value_counts().reindex(order).fillna(0)
        fig, ax = plt.subplots(figsize=(6, 4)); ax.bar(tc.index, tc.values, color=["#d62728", "#f2a541", "#edc948", "#bab0ac"])
        for i, v in enumerate(tc.values):
            ax.text(i, v, int(v), ha="center", va="bottom", fontsize=8)
        ax.set_title("Factories by risk tier"); fig.tight_layout(); fig.savefig(CH / "4_risk_tiers.png", dpi=150); plt.close(fig)
    for src, dst in (("km_curves.png", "5_time_until_next_failure.png"), ("hotspot_map.png", "6_hotspot_map.png")):
        if (OUT_DIR / src).exists():
            shutil.copy(OUT_DIR / src, CH / dst)
        else:
            print(f"  (chart skipped, {src} not found)")

    # ------------------------------------------------------------------ integrity checks (Step 24)
    print("Running integrity checks ...")
    raw_n = raw_s = None
    try:
        raw_n = len(pd.read_csv(NSQ_FILE, dtype=str, usecols=[0])); raw_s = len(pd.read_csv(SPURIOUS_FILE, dtype=str, usecols=[0]))
    except Exception:                                          # noqa
        pass
    n_ok, n_rev, n_nom = len(ok), len(rev) if rev is not None else 0, len(nom) if nom is not None else 0
    checks = []
    def chk_(name, passed, detail):
        checks.append({"check": name, "result": "PASS" if passed else "CHECK", "detail": detail})
    if raw_n is not None:
        chk_("Every raw portal row is accounted for", raw_n + raw_s == n_ok + n_rev + n_nom,
             f"raw {raw_n} NSQ + {raw_s} spurious = {raw_n + raw_s};  split {n_ok} + review {n_rev} + no manufacturer {n_nom} = {n_ok + n_rev + n_nom}")
    chk_("row_to_factory has one line per split row", len(r2f) == n_ok, f"{len(r2f)} vs {n_ok}")
    chk_("NSQ and spurious counts add up to the split rows (spurious never added to NSQ counts)",
         int(ft.nsq_incidents.sum() + ft.spurious_incidents.sum()) == n_ok,
         f"{int(ft.nsq_incidents.sum())} NSQ + {int(ft.spurious_incidents.sum())} spurious = {int(ft.nsq_incidents.sum() + ft.spurious_incidents.sum())} vs {n_ok}")
    if sc is not None:
        missing = set(sc.factory_id) - set(r2f.factory_id)
        chk_("Every risk score traces back to alert rows", len(missing) == 0, f"{len(sc)} scored factories; {len(missing)} without rows")
    chk_("Factory ids are unique", ft.factory_id.is_unique, f"{len(ft)} factories")
    ck = pd.DataFrame(checks)

    # ------------------------------------------------------------------ workbook (Step 23)
    print("Writing the workbook ...")
    company = (ft.assign(spurious_incidents=ft.spurious_incidents)
               .groupby("company_id").agg(company_name_example=("factory_name", "first"), factories=("factory_id", "size"),
                                          nsq_incidents=("nsq_incidents", "sum"), spurious_incidents=("spurious_incidents", "sum"),
                                          factories_with_3plus_incidents=("nsq_incidents", lambda s: int((s >= 3).sum())))
               .reset_index().sort_values("nsq_incidents", ascending=False))
    dd = pd.DataFrame([
        ("factory_table", "One row per factory (draft matching of spelling variants). nsq_incidents = failed-sample rows; spurious_incidents counted separately."),
        ("master_alerts", "One row per alert row that could be split into manufacturer name + address, with factory_id and company_id. raw_file / row_index_in_month point to the original portal data."),
        ("factory_risk_scores", "0-100 score = 40% number of incidents + 35% recency (half-life 2 years) + 25% spread (years + reporting places), as percentile ranks. Severity share is shown but not in the score."),
        ("company_rollup", "Factories grouped by company_id."),
        ("survival_summary", "Share of factories that have NOT had a second NSQ failure after 6/12/24/36 months (Kaplan-Meier)."),
        ("backtest", "Score built on early years only, checked against later years."),
        ("count_model", "Zero-truncated Negative Binomial. rate_ratio 1.5 = about 50% more failures than the reference group. Descriptive, not causal."),
        ("district_hotspots / state_rollup", "Factories placed in districts through their PIN code. Gi* hotspot test on repeat-failure factories (3+ incidents)."),
        ("alerts_by_year_source", "NSQ alert rows per year by reporting source."),
        ("integrity_checks", "Automatic consistency checks."),
        ("IMPORTANT", "All numbers are raw counts from public CDSCO regulatory records. They are not failure rates and do not show that any company acted wrongly."),
    ], columns=["sheet", "what it contains"])
    sheets = [("README_data_dictionary", dd), ("factory_table", ft), ("factory_risk_scores", sc), ("company_rollup", company),
              ("master_alerts", r2f), ("survival_summary", sv), ("backtest", bt), ("count_model", cm), ("count_model_fit", cmc),
              ("district_hotspots", hd), ("state_rollup", hs), ("failure_categories", fc),
              ("alerts_by_year_source", ct.reset_index()), ("match_check_sample", chk), ("integrity_checks", ck)]
    with pd.ExcelWriter(WB, engine="openpyxl") as xw:
        for name, df in sheets:
            if df is not None:
                df.to_excel(xw, sheet_name=name[:31], index=False)
    import openpyxl
    wbk = openpyxl.load_workbook(WB)
    n_formula = sum(1 for ws in wbk for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("="))
    n_merged = sum(len(ws.merged_cells.ranges) for ws in wbk)
    ck.loc[len(ck)] = ["Workbook is fully editable (no formulas, no merged cells)", "PASS" if n_formula == n_merged == 0 else "CHECK",
                       f"{n_formula} formula cells, {n_merged} merged ranges"]
    with pd.ExcelWriter(WB, engine="openpyxl", mode="a", if_sheet_exists="replace") as xw:
        ck.to_excel(xw, sheet_name="integrity_checks", index=False)

    # ------------------------------------------------------------------ README + methods note (Step 25 wording)
    print("Writing README and methods note ...")
    f1 = (f"- About {pc(rep2 / n_fac)} of factories ({rep2:,} of {n_fac:,}) appear in the NSQ alerts in two or more different years, "
          f"and {rep3:,} factories have three or more NSQ incidents.")
    f2 = (f"- About {pc(km_line['s12'])} of factories with a first NSQ alert appear in another alert within 12 months, {pc(km_line['s24'])} within 24 months "
          f"({km_line['second']:,} of {km_line['n']:,} have had a second alert so far). "
          + (f"The type of the first failure made no clear difference to the timing (p = {p_sev:.2f})." if p_sev is not None and p_sev > 0.05 else
             (f"Factories whose first failure was serious came back at a different pace (p = {p_sev:.3f})." if p_sev is not None else ""))) if km_line else "- (survival analysis not found)"
    f3 = (f"- Past alerts predict later alerts: trained on earlier years only, the top 10% of factories by score were flagged again in the later years "
          f"{pc(float(top10_hit))} of the time, against {pc(float(base_hit))} for all scored factories (lift {float(top10_lift):.1f}; a plain count of past alerts gave {float(cnt10_lift):.1f}).") if top10_hit is not None else "- (backtest not found)"
    readme = f"""# CDSCO NSQ and spurious drug alerts: repeat-failure analysis

Based on public CDSCO regulatory records. Raw counts only: no samples-tested denominator is available, so these are not failure rates.
Nothing here says that any named company acted wrongly.

## Three headline findings
{f1}
{f2}
{f3}

## More results
- Data: {len(nsq_ok):,} NSQ alert rows ({years[0] if years else ''} to {years[-1] if years else ''}; the last year is partial) and {int((ok.category == 'Spurious').sum())} spurious rows with a manufacturer name. {n_fac:,} factories grouped under {n_comp:,} companies (spelling variants merged: {multi_var if multi_var is not None else 'n/a'} factories had more than one spelling).
- Alerts per year jumped in 2025 mainly because state laboratories began reporting separately (see chart 2). Do not read the jump as a change in quality.
""" + ("- Associations from the count model (compared with the largest PIN area; descriptive, not causal):\n" + "\n".join("  " + l for l in cm_lines) + "\n" if cm_lines else "") + \
    (f"- Hotspot test: {hot_n} districts with repeat-failure factories fall in a statistically significant cluster. Largest districts:\n" + "\n".join("  " + l for l in hot_lines) + "\n" if hot_lines else "") + f"""
## Files
- `CDSCO_NSQ_Project_Workbook.xlsx`: all tables (see the first sheet for a data dictionary).
- `charts/`: 1 top factories, 2 alerts by year and source, 3 failure categories, 4 risk tiers, 5 time until next failure, 6 hotspot map.
- `METHODS_NOTE.md`: how everything was made, and the limits.
- `code/`: the scripts, in the order: downloaders -> `cdsco_manufacturer_parser_v2.py` -> `cdsco_factory_matcher.py` -> `cdsco_analysis.py` -> `cdsco_count_model.py` -> `cdsco_hotspots.py` -> `cdsco_package.py`.

Built {date.today().isoformat()}.
"""
    prec_txt = (f"{prec[0]} of {prec[1]} merged name pairs were checked by hand and judged to be the same factory ({pc(prec[0] / prec[1])} precision)."
                if prec else "Precision check NOT YET DONE: open match_check_sample.csv, type SAME or DIFFERENT for each pair, then re-run this script.")
    methods = f"""# Methods note

**Wording rule:** results are described as based on public CDSCO regulatory records. Counts are raw failures, not rates.

## 1. Data
- NSQ and spurious alert tables downloaded from the CDSCO online NSQ portal (https://cdscoonline.gov.in/CDSCO/viewPublicNSQDrug), {raw_n if raw_n else 'n/a'} NSQ rows and {raw_s if raw_s else 'n/a'} spurious rows.
- Reporting source spellings were standardised to two values: CDSCO lab and State lab.

## 2. Spurious alerts
- Spurious alerts are kept in a separate column and never added to a factory's NSQ count. The company named on a spurious product's label may not have made it. Most spurious rows have no manufacturer ("Under Investigation") and are not attached to any factory.

## 3. Splitting name and address, and matching spellings
- The text in the "manufactured by" field was split into company name and address with rules (company words such as Pharma, Labs, Ltd; PIN codes; address words). Rows the rules could not split are in manufacturer_needs_review.csv ({n_rev} rows), placeholders ("Under Investigation", "Not applicable") in rows_no_manufacturer.csv ({n_nom} rows).
- Spelling variants were matched within the same PIN-code area using name similarity (rapidfuzz token-sort ratio of at least 90 and the same first word). A company id groups factories of the same company (similarity of at least 94). {prec_txt}
- Factories without a location are matched only by name inside their own group and are not merged with located factories.

## 4. Analyses
- Failure categories come from keywords in the test-result text; assay, sterility and identification are treated as serious.
- Time until the next failure: Kaplan-Meier curves; the event is a second NSQ alert in a different reporting month; factories without one are censored at the end of the data.
- Risk score (0-100): percentile ranks of number of incidents (40%), recency-weighted incidents (35%, half-life 2 years) and spread over years and reporting places (25%). Weights were chosen by comparing variants in time-split backtests (cdsco_score_check.py), so the reported lift is probably a little optimistic. Backtest: {cut_info}.
- Count model: zero-truncated Negative Binomial on incidents per factory (only factories with at least one failure are in the data). It uses PIN area, share of injectable / oral solid / oral liquid products and whether a company has several factories. Uncertainty is from the model or, if that fails, from a bootstrap.
- Hotspots: PIN code -> district with the offline package indiapins; Getis-Ord Gi* on repeat-failure factories (3+ incidents), 8 nearest districts, 999 permutations.

## 5. Limits
- No denominator: the number of samples tested per factory is not published, so large and heavily sampled factories look worse. RTIs for this were planned.
- Alert volume rose sharply from 2024-25 because state laboratories began reporting; this is a reporting change.
- The first alert seen in the data may not be a factory's first ever alert.
- Disputed results (Section 25 of the Drugs and Cosmetics Act, re-testing at CDL) are not tracked.
- Factories without a PIN code cannot be placed on the map; location-unknown factories have few details and look better than they may be.
- Government blacklists, licence data and factory size are not included yet.
"""
    (PKG / "README.md").write_text(readme, encoding="utf-8")
    (PKG / "METHODS_NOTE.md").write_text(methods, encoding="utf-8")

    # ------------------------------------------------------------------ code folder
    for s in SCRIPTS:
        if (BASE / s).exists():
            shutil.copy(BASE / s, CODE / s)
    (CODE / "requirements.txt").write_text("pandas\nnumpy\nopenpyxl\nrapidfuzz\nlifelines\nstatsmodels\npatsy\nmatplotlib\nindiapins\nesda\nlibpysal\n", encoding="utf-8")

    print("\n--- INTEGRITY CHECKS ---")
    print(ck.to_string(index=False))
    print(f"\nDone. Everything is in: {PKG}")
    if not prec:
        print("TODO: do the precision check in match_check_sample.csv (SAME / DIFFERENT), then run this script again.")


if __name__ == "__main__":
    main()
