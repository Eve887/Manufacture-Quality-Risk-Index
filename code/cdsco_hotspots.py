"""
cdsco_hotspots.py   (Step 18 of the plan: where are the repeat-failure factories?)

What it does
  1. turns each factory's PIN code into a STATE and DISTRICT (using the offline package 'indiapins')
  2. counts, per district: factories, repeat-failure factories (3+ NSQ incidents), NSQ incidents, spurious incidents
  3. runs the Getis-Ord Gi* hotspot test. It answers: "is this district's count high, AND are its neighbours high too,
     more than chance would give?"  A hot spot is a CLUSTER of districts, not just one big district.
  4. writes hotspot_results.xlsx (plain values only) and hotspot_map.png

One-time install:  py -m pip install indiapins esda libpysal matplotlib openpyxl
Run:               py cdsco_hotspots.py
Needs: factory_table.csv (made by cdsco_factory_matcher.py) and cdsco_manufacturer_parser_v2.py in the same folder.

Limits (say these in your methods note):
  * Only factories with a PIN code can be placed (the others are counted as 'not placed').
  * A district's position is the middle of its post offices, so the map is approximate.
  * The map has no state borders; the dots themselves outline India.
  * Counts are raw failures, not rates (no sample-count denominator yet).
"""
import bz2
import json
import os
import sys
import warnings

import numpy as np
import pandas as pd

from cdsco_manufacturer_parser_v2 import OUT_DIR

FT_FILE = OUT_DIR / "factory_table.csv"
OUT_XLSX = OUT_DIR / "hotspot_results.xlsx"
OUT_PNG = OUT_DIR / "hotspot_map.png"
REPEAT_MIN = 3          # a "repeat-failure factory" has at least this many NSQ incidents
K_NEIGHBOURS = 8        # each district is compared with its 8 nearest districts (and itself)
PERMUTATIONS = 999      # random re-shuffles used to get the p-value


def load_post_offices():
    import indiapins
    try:
        recs = indiapins.core._zips
        recs = recs() if callable(recs) else recs
        if not recs:
            raise ValueError
    except Exception:
        path = os.path.join(os.path.dirname(indiapins.__file__), "pins.json.bz2")
        with bz2.open(path, "rt", encoding="utf-8") as fh:
            recs = json.load(fh)
    df = pd.DataFrame(recs)[["Pincode", "State", "District", "Latitude", "Longitude"]]
    df["Pincode"] = df.Pincode.astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df["Latitude"] = pd.to_numeric(df.Latitude, errors="coerce")
    df["Longitude"] = pd.to_numeric(df.Longitude, errors="coerce")
    ok = df.Latitude.between(6, 38) & df.Longitude.between(66, 100)
    df.loc[~ok, ["Latitude", "Longitude"]] = np.nan
    df = df[df.State.notna() & df.District.notna()].copy()      # some post offices have no district: skip them
    df["State"] = df.State.astype(str).str.strip()
    df["District"] = df.District.astype(str).str.strip()
    return df[(df.State != "") & (df.District != "")]


def main():
    if not FT_FILE.exists():
        sys.exit(f"Not found: {FT_FILE}\nRun cdsco_factory_matcher.py first.")
    try:
        import indiapins, esda, libpysal, matplotlib, openpyxl  # noqa
    except ImportError:
        sys.exit("Missing a library. Run:  py -m pip install indiapins esda libpysal matplotlib openpyxl")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from esda.getisord import G_Local
    from libpysal.weights import KNN

    po = load_post_offices()
    pin_map = (po.groupby("Pincode")
               .agg(State=("State", lambda s: s.mode().iloc[0]), District=("District", lambda s: s.mode().iloc[0]))
               .reset_index())
    dist = (po.groupby(["State", "District"])[["Latitude", "Longitude"]].median().dropna().reset_index())

    ft = pd.read_csv(FT_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    for c in ("nsq_incidents", "spurious_incidents"):
        ft[c] = pd.to_numeric(ft[c], errors="coerce").fillna(0).astype(int)
    ft["Pincode"] = ft.pin_example.where(ft.pin_example.str.fullmatch(r"\d{6}"), "")
    ft = ft.merge(pin_map, on="Pincode", how="left")
    placed = ft[ft.District.notna()].copy()
    print("\n--- COVERAGE ---")
    print(f"Factories in the factory table : {len(ft)}")
    print(f"Placed in a district (PIN found): {len(placed)}  ({len(placed) / len(ft):.0%})")
    print(f"Not placed (no PIN or PIN unknown): {len(ft) - len(placed)}")

    placed["is_repeat"] = (placed.nsq_incidents >= REPEAT_MIN).astype(int)
    agg = placed.groupby(["State", "District"]).agg(
        factories=("factory_id", "size"), repeat_failure_factories=("is_repeat", "sum"),
        nsq_incidents=("nsq_incidents", "sum"), spurious_incidents=("spurious_incidents", "sum")).reset_index()
    d = dist.merge(agg, on=["State", "District"], how="left").fillna(
        {"factories": 0, "repeat_failure_factories": 0, "nsq_incidents": 0, "spurious_incidents": 0})
    for c in ("factories", "repeat_failure_factories", "nsq_incidents", "spurious_incidents"):
        d[c] = d[c].astype(int)
    lost = int(agg.factories.sum() - d.factories.sum())
    if lost:
        print(f"Note: {lost} placed factories sit in districts without coordinates and are left out of the hotspot test.")

    # ---- Getis-Ord Gi*
    rng = np.random.default_rng(1)
    xy = d[["Longitude", "Latitude"]].to_numpy() + rng.normal(0, 1e-5, (len(d), 2))   # tiny jitter: no identical points
    w = KNN.from_array(xy, k=K_NEIGHBOURS)
    w.transform = "B"
    y = d.repeat_failure_factories.to_numpy(dtype=float)
    if y.sum() == 0:
        sys.exit("No repeat-failure factories were placed in districts. Check pin_example in factory_table.csv.")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        g = G_Local(y, w, transform="B", star=True, permutations=PERMUTATIONS, seed=12345)
    d["gi_star_z"] = np.round(g.Zs, 2)
    d["p_value"] = np.round(g.p_sim, 4)
    d["hotspot"] = np.select(
        [(d.gi_star_z > 1.96) & (d.p_value < 0.05), (d.gi_star_z > 1.65) & (d.p_value < 0.10)],
        ["Hot spot (95%)", "Possible hot spot (90%)"], "not significant")
    d = d.sort_values(["repeat_failure_factories", "nsq_incidents"], ascending=False)
    state = (d.groupby("State")[["factories", "repeat_failure_factories", "nsq_incidents", "spurious_incidents"]].sum()
             .reset_index().sort_values(["repeat_failure_factories", "nsq_incidents"], ascending=False))
    notes = pd.DataFrame([
        {"item": "Repeat-failure factory means", "value": f"{REPEAT_MIN}+ NSQ incidents"},
        {"item": "Hotspot test", "value": f"Getis-Ord Gi*, {K_NEIGHBOURS} nearest districts, {PERMUTATIONS} permutations"},
        {"item": "Factories placed in a district", "value": f"{len(placed)} of {len(ft)}"},
        {"item": "Counts are", "value": "raw failures, not rates (no samples-tested denominator)"},
        {"item": "District position", "value": "median of its post offices (approximate)"}])

    # ---- map
    fig, ax = plt.subplots(figsize=(8.5, 9.5))
    ax.scatter(d.Longitude, d.Latitude, s=4, c="#d9d9d9", linewidths=0)
    ring = d[(d.hotspot == "Hot spot (95%)") & (d.repeat_failure_factories == 0)]
    ax.scatter(ring.Longitude, ring.Latitude, s=40, facecolors="none", edgecolors="#d62728", linewidths=0.8, label="district inside a hot-spot cluster (no repeat factory itself)")
    has = d[d.repeat_failure_factories > 0]
    hot = has.hotspot == "Hot spot (95%)"
    pos = has.hotspot == "Possible hot spot (90%)"
    size = 20 + 45 * np.sqrt(has.repeat_failure_factories)
    ax.scatter(has.Longitude[~hot & ~pos], has.Latitude[~hot & ~pos], s=size[~hot & ~pos], c="#4c78a8", alpha=.65, label="repeat-failure factories (not a significant cluster)")
    ax.scatter(has.Longitude[pos], has.Latitude[pos], s=size[pos], c="#f2a541", alpha=.85, label="possible hot spot (90%)")
    ax.scatter(has.Longitude[hot], has.Latitude[hot], s=size[hot], c="#d62728", alpha=.85, label="hot spot (95%)")
    for _, r in has.head(12).iterrows():
        ax.annotate(r.District.title(), (r.Longitude, r.Latitude), fontsize=7, xytext=(4, 4), textcoords="offset points")
    ax.set_aspect(1 / np.cos(np.radians(22)))
    ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
    ax.set_title(f"Districts with repeat-failure factories ({REPEAT_MIN}+ NSQ incidents)\nDot size = number of such factories; grey dots = all districts", fontsize=10)
    leg = ax.legend(loc="lower left", fontsize=7, frameon=True)
    for h in getattr(leg, "legend_handles", getattr(leg, "legendHandles", [])):
        try:
            h.set_sizes([40])
        except Exception:
            pass
    fig.tight_layout(); fig.savefig(OUT_PNG, dpi=150); plt.close(fig)

    with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as xw:
        d.rename(columns={"Latitude": "latitude", "Longitude": "longitude"}).to_excel(xw, sheet_name="district_hotspots", index=False)
        state.to_excel(xw, sheet_name="state_rollup", index=False)
        notes.to_excel(xw, sheet_name="notes", index=False)

    cols = ["State", "District", "factories", "repeat_failure_factories", "nsq_incidents", "gi_star_z", "p_value", "hotspot"]
    print("\n--- TOP 15 DISTRICTS (repeat-failure factories) ---")
    print(d[cols].head(15).to_string(index=False))
    print("\n--- HOT SPOTS (clusters, 95%) ---")
    hs_all = d[d.hotspot == "Hot spot (95%)"]
    hs = hs_all[hs_all.repeat_failure_factories > 0][cols]
    print(hs.to_string(index=False) if len(hs) else "none")
    print(f"({len(hs_all) - len(hs)} more hot-spot districts have no repeat-failure factory themselves; they sit inside the same cluster)")
    print(f"\nPossible hot spots (90%): {int((d.hotspot == 'Possible hot spot (90%)').sum())}")
    print("\n--- TOP 10 STATES ---")
    print(state.head(10).to_string(index=False))
    print(f"\nWritten: {OUT_XLSX}\n         {OUT_PNG}")


if __name__ == "__main__":
    main()
