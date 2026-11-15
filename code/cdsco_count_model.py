"""
cdsco_count_model.py   (Step 16 of the plan: count model for repeat failures)

Question it answers: which factory features go together with MORE NSQ failures?
Features used (all available now, no licence data needed yet):
  - PIN area (first two digits of the PIN code)  -> a rough region / industrial cluster
  - what kind of medicine the failed products are (injection, tablet/capsule, liquid)
  - whether the company runs more than one factory

Important detail: our table only contains factories that failed AT LEAST ONCE (nobody has 0).
An ordinary count model assumes zeros are visible, so it would give wrong answers.
This script therefore uses a ZERO-TRUNCATED model (it knows the zeros are missing).
It fits two versions, Poisson and Negative Binomial, and keeps the one with the better (lower) AIC.
The Negative Binomial is the one that allows "a few factories with a LOT of failures".

One-time install:  py -m pip install statsmodels patsy openpyxl
Run:               py cdsco_count_model.py
Needs: the files made by the parser and the matcher, and cdsco_manufacturer_parser_v2.py in the same folder.
"""
import argparse
import re
import sys
import warnings

import numpy as np
import pandas as pd

from cdsco_manufacturer_parser_v2 import OUT_DIR, PRODUCT_COL

ROWS_FILE = OUT_DIR / "rows_with_manufacturer_split.csv"
MAP_FILE = OUT_DIR / "row_to_factory.csv"
FT_FILE = OUT_DIR / "factory_table.csv"
OUT_XLSX = OUT_DIR / "count_model_results.xlsx"
MIN_FACTORIES_PER_REGION = 25      # smaller PIN areas are grouped as "Other PIN areas"

FORM_RULES = [
    ("injectable", r"inject|\binj\b|infusion|vial|ampoule|ampule|parenteral|\biv\b|i\.v\.|cannula|syringe"),
    ("oral_solid", r"tablet|\btab\b|\btabs\b|capsule|\bcap\b|\bcaps\b|lozenge|\bsr\b|\bxr\b|\ber\b|dispersible"),
    ("oral_liquid", r"syrup|suspension|solution|oral liquid|elixir|drops|\bsusp\b|emulsion|linctus|\bliquid\b"),
]


def dosage_form(name):
    t = str(name).lower()
    for form, pat in FORM_RULES:
        if re.search(pat, t):
            return form
    return "other_form"


def fit_nb(y, X, start_pois):
    """Fit the zero-truncated Negative Binomial.
    Step 1: a fast fit (BFGS) to find the answer.  Step 2: a few 'polishing' steps (Newton) from that answer,
    which is what makes the uncertainty (standard errors) calculable."""
    from statsmodels.discrete.truncated_model import TruncatedLFNegativeBinomialP
    start = np.append(np.asarray(start_pois), 1.0)
    quick = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for method in ("bfgs", "lbfgs"):
            try:
                quick = TruncatedLFNegativeBinomialP(y, X, truncation=0).fit(start_params=start, method=method, maxiter=600, disp=0)
                break
            except Exception:
                continue
        if quick is None:
            return None
        if np.isfinite(np.asarray(quick.bse, dtype=float)).all():
            return quick
        try:
            polished = TruncatedLFNegativeBinomialP(y, X, truncation=0).fit(
                start_params=np.asarray(quick.params), method="newton", maxiter=6, disp=0)
            if np.isfinite(np.asarray(polished.bse, dtype=float)).all() and polished.llf >= quick.llf - 1e-3:
                return polished
        except Exception:
            pass
    return quick


def bootstrap_se(y, X, start, B=100, seed=1):
    """Plan B for the uncertainty: refit the model on 100 random re-samples of the factories."""
    from statsmodels.discrete.truncated_model import TruncatedLFNegativeBinomialP
    rng = np.random.default_rng(seed)
    n, draws = len(y), []
    for b in range(B):
        idx = rng.integers(0, n, n)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = TruncatedLFNegativeBinomialP(y[idx], X[idx], truncation=0).fit(start_params=start, method="bfgs", maxiter=200, disp=0)
            pr = np.asarray(r.params)
            if np.isfinite(pr).all() and np.abs(pr[:-1]).max() < 15:
                draws.append(pr)
        except Exception:
            pass
        if (b + 1) % 25 == 0:
            print(f"   bootstrap {b + 1}/{B} ...", flush=True)
    if len(draws) < 30:
        return None, len(draws)
    return np.std(np.array(draws), axis=0, ddof=1), len(draws)


def main(force_bootstrap=False):
    for f in (ROWS_FILE, MAP_FILE, FT_FILE):
        if not f.exists():
            sys.exit(f"Not found: {f}\nRun the parser and the matcher first.")
    try:
        import statsmodels  # noqa
        from patsy import dmatrix  # noqa
        from statsmodels.discrete.truncated_model import TruncatedLFPoisson, TruncatedLFNegativeBinomialP
    except ImportError:
        sys.exit("Missing a library. Run:  py -m pip install statsmodels patsy openpyxl\n"
                 "(If it is already installed, update it with:  py -m pip install --upgrade statsmodels)")

    rows = pd.read_csv(ROWS_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    mp = pd.read_csv(MAP_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    ft = pd.read_csv(FT_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if len(rows) != len(mp):
        sys.exit("Row counts differ between rows_with_manufacturer_split.csv and row_to_factory.csv. Re-run the matcher.")
    rows["factory_id"] = mp.factory_id.values
    nsq = rows[rows.category == "NSQ"].copy()
    nsq["form"] = nsq[PRODUCT_COL].map(dosage_form)

    # ---- one row per factory
    g = nsq.groupby("factory_id")
    f = pd.DataFrame({"n_incidents": g.size()})
    for form in ("injectable", "oral_solid", "oral_liquid"):
        f[f"share_{form}"] = g.form.apply(lambda s, form=form: (s == form).mean())
    f = f.reset_index().merge(ft[["factory_id", "company_id", "pin_example", "location_known", "factory_name"]], on="factory_id", how="left")
    n_fac = ft.groupby("company_id").factory_id.nunique()
    f["company_has_multiple_factories"] = (f.company_id.map(n_fac) >= 2).astype(int)
    pin2 = f.pin_example.str[:2].where(f.pin_example.str.len() == 6, "")
    counts = pin2[pin2 != ""].value_counts()
    big = set(counts[counts >= MIN_FACTORIES_PER_REGION].index)
    f["region"] = np.where(pin2 == "", "Location unknown",
                           np.where(pin2.isin(big), "PIN area " + pin2, "Other PIN areas"))
    ref = counts.index[0]
    ref_label = f"PIN area {ref}" if ref in big else "Other PIN areas"

    # ---- design matrix
    from patsy import dmatrices
    formula = (f"n_incidents ~ C(region, Treatment(reference='{ref_label}')) + share_injectable + share_oral_solid"
               f" + share_oral_liquid + company_has_multiple_factories")
    y, X = dmatrices(formula, f, return_type="dataframe")
    y = y.iloc[:, 0].astype(int).values
    names = [re.sub(r"C\(region, Treatment\(reference='.*?'\)\)\[T\.(.*)\]", r"\1", c) for c in X.columns]

    print("\n--- DATA CHECK ---")
    print(f"Factories in the model          : {len(f)}")
    print(f"Mean failures per factory       : {y.mean():.2f}   (variance {y.var():.2f}; variance much larger than mean = a few factories fail a lot)")
    print(f"Factories with exactly 1 failure: {(y == 1).sum()} ({(y == 1).mean():.0%})")
    print(f"Most failures at one factory    : {y.max()}")
    print(f"Reference PIN area (compared against): {ref_label}")

    # ---- fit both models
    fits = {}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fits["Poisson (zero-truncated)"] = TruncatedLFPoisson(y, X.values, truncation=0).fit(method="bfgs", maxiter=3000, disp=0)
    except Exception as e:                                         # noqa
        print("Poisson model failed:", e)
    nb = fit_nb(y, X.values, fits["Poisson (zero-truncated)"].params if fits else np.zeros(X.shape[1]))
    if nb is not None:
        fits["Negative Binomial (zero-truncated)"] = nb
    if not fits:
        sys.exit("Neither model could be fitted. Send me the DATA CHECK lines above.")

    comp = pd.DataFrame([{"model": k, "AIC (lower is better)": round(v.aic, 1), "log_likelihood": round(v.llf, 1)} for k, v in fits.items()])
    best_name = comp.sort_values("AIC (lower is better)").iloc[0].model
    best = fits[best_name]
    k = len(names)
    params = np.asarray(best.params)[:k]
    se_full = np.asarray(best.bse, dtype=float)
    how = "model"
    if force_bootstrap or not np.isfinite(se_full[:k]).all():
        print("\nThe model could not give its own uncertainty. Using the bootstrap instead (a few minutes) ...")
        se_b, ok = bootstrap_se(y, X.values, np.asarray(best.params))
        if se_b is None:
            se = np.full(k, np.nan); how = "NOT AVAILABLE"
        else:
            se_full, se, how = se_b, se_b[:k], f"bootstrap ({ok} refits)"
    else:
        se = se_full[:k]
    from scipy.stats import norm
    with np.errstate(all="ignore"):
        pv = 2 * (1 - norm.cdf(np.abs(params / se)))
    res = pd.DataFrame({
        "feature": names,
        "rate_ratio": np.exp(params).round(3),
        "rate_ratio_low_95": np.exp(params - 1.96 * se).round(3),
        "rate_ratio_high_95": np.exp(params + 1.96 * se).round(3),
        "p_value": np.round(pv, 4),
    })
    res["meaning"] = np.where(res.p_value.isna(), "could not be estimated",
                      np.where(res.p_value < 0.05,
                               np.where(res.rate_ratio > 1, "MORE failures (statistically clear)", "FEWER failures (statistically clear)"),
                               "no clear difference"))
    res = res[res.feature != "Intercept"]
    extra = []
    if "Negative" in best_name:
        extra.append({"item": "alpha (extra spread, 0 = plain Poisson)", "value": round(float(np.asarray(best.params)[-1]), 3)})
    extra += [{"item": "Model kept", "value": best_name}, {"item": "Factories", "value": len(f)},
              {"item": "How uncertainty was measured", "value": how}]

    with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as xw:
        res.sort_values("rate_ratio", ascending=False).to_excel(xw, sheet_name="model_results", index=False)
        comp.to_excel(xw, sheet_name="model_comparison", index=False)
        pd.DataFrame(extra).to_excel(xw, sheet_name="model_notes", index=False)
        f.to_excel(xw, sheet_name="factory_features", index=False)

    print("\n--- MODEL COMPARISON ---")
    print(comp.to_string(index=False))
    print(f"\nKept: {best_name}   (uncertainty measured by: {how})")
    print("\n--- RESULTS (rate_ratio 1.5 = about 50% more failures than the reference group, 0.7 = about 30% fewer) ---")
    print(res.sort_values("rate_ratio", ascending=False).to_string(index=False))
    print(f"\nWritten: {OUT_XLSX}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-bootstrap", action="store_true")
    main(ap.parse_args().force_bootstrap)
