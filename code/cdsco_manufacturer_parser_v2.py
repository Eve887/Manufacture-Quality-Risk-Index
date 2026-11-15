"""
cdsco_manufacturer_parser_v2.py
Reads the two portal files (NSQ and spurious), cleans the reporting source,
splits manufacturer name from address, and counts incidents per factory.

Put this file in the SAME folder as manufacturer_split.py (E:\\cdsco_mqri).
Run:   py cdsco_manufacturer_parser_v2.py --preview     (reads only, writes nothing)
       py cdsco_manufacturer_parser_v2.py               (full run)

Output goes to a NEW folder, cdsco_manufacturer_data_v2, so your old results are not touched.
"""
import argparse
import re
import sys
from pathlib import Path

import pandas as pd

from manufacturer_split import split_manufacturer, PIN_RE

BASE = Path(__file__).resolve().parent
NSQ_FILE = BASE / "cdsco_nsq_portal" / "nsq_drugs.csv"
SPURIOUS_FILE = BASE / "cdsco_nsq_portal" / "spurious_drugs.csv"
OUT_DIR = BASE / "cdsco_manufacturer_data_v2"

MFR_COL = "str_manufactured_by"
SOURCE_COL = "str_reporting_source"
PRODUCT_COL = "str_product_name"
OK_STATUSES = {"ok_full_address", "ok_place_only", "ok_name_only", "ok_guess_name"}


def load_data():
    for f in (NSQ_FILE, SPURIOUS_FILE):
        if not f.exists():
            sys.exit(f"File not found: {f}")
    nsq = pd.read_csv(NSQ_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    spu = pd.read_csv(SPURIOUS_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    # the spurious file calls the product column something different
    if PRODUCT_COL not in spu.columns and "product_name_from_dtl" in spu.columns:
        spu = spu.rename(columns={"product_name_from_dtl": PRODUCT_COL})
    nsq["category"] = "NSQ"          # category comes from WHICH FILE the row is in
    spu["category"] = "Spurious"
    for col in (MFR_COL, SOURCE_COL, PRODUCT_COL):
        for name, d in (("nsq", nsq), ("spurious", spu)):
            if col not in d.columns:
                sys.exit(f"Column '{col}' is missing in the {name} file. Columns found: {list(d.columns)}")
    df = pd.concat([nsq, spu], ignore_index=True, sort=False).fillna("")
    print(f"Loaded {len(df)} rows: {len(nsq)} NSQ + {len(spu)} spurious")
    return df


def clean_source(x):
    t = str(x).strip().lower()
    if not t:
        return ""
    if "cdsco" in t or "central" in t:
        return "CDSCO lab"
    if "state" in t:
        return "State lab"
    if "not applicable" in t:
        return "Not applicable"
    return str(x).strip()


def normalise_name(name):
    t = name.upper()
    t = re.sub(r"\(P\)", " PVT ", t)
    t = re.sub(r"[^A-Z0-9& ]", " ", t)
    t = re.sub(r"\bPHARMACEUTICALS?\b", "PHARMA", t)
    t = re.sub(r"\bLABORATORIES\b|\bLABORATORY\b", "LABS", t)
    t = re.sub(r"\bPRIVATE\b", "PVT", t)
    t = re.sub(r"\bLIMITED\b", "LTD", t)
    return re.sub(r"\s+", " ", t).strip()


def place_token(rest):
    """A rough location marker: PIN code if present, else the first city-like part."""
    m = PIN_RE.search(rest)
    if m:
        return re.sub(r"\s", "", m.group(0))
    first = rest.split(",")[0]
    first = re.sub(r"\(.*?\)", "", first)
    first = re.sub(r"[^A-Za-z ]", " ", first).strip().upper()
    return re.sub(r"\s+", " ", first) or "NOLOC"


def run(preview):
    df = load_data()
    if preview:
        sample = df[MFR_COL].drop_duplicates().sample(min(20, df[MFR_COL].nunique()), random_state=7)
        print("\n20 sample distinct manufacturer strings and how they would be split:\n")
        for s in sample:
            n, a, st = split_manufacturer(s)
            print(f"[{st}]\n   RAW : {re.sub(chr(10), ' ', s)[:130]}\n   NAME: {n}\n   REST: {a[:80]}\n")
        return

    df["source_clean"] = df[SOURCE_COL].map(clean_source)
    parts = df[MFR_COL].map(split_manufacturer)
    df["manufacturer_name"] = [p[0] for p in parts]
    df["address_part"] = [p[1] for p in parts]
    df["split_status"] = [p[2] for p in parts]
    df["factory_key_draft"] = [
        (normalise_name(n) + " | " + place_token(a)) if st in OK_STATUSES else ""
        for n, a, st in zip(df.manufacturer_name, df.address_part, df.split_status)
    ]

    OUT_DIR.mkdir(exist_ok=True)
    ok = df[df.split_status.isin(OK_STATUSES)]
    review = df[df.split_status.str.startswith("review")]
    nomfr = df[df.split_status == "no_manufacturer"]

    # Summary: NSQ and spurious are counted in SEPARATE columns, never added together
    rows = []
    for key, d in ok.groupby("factory_key_draft"):
        nsq = d[d.category == "NSQ"]
        spu = d[d.category == "Spurious"]
        years = sorted({y for y in nsq["year"] if y}) if "year" in nsq.columns else []
        rows.append({
            "factory_key_draft": key,
            "manufacturer_name_most_common": d.manufacturer_name.mode().iloc[0],
            "place_example": d.address_part.mode().iloc[0][:120],
            "nsq_incidents": len(nsq),
            "spurious_incidents": len(spu),
            "nsq_distinct_years": len(years),
            "first_year": years[0] if years else "",
            "last_year": years[-1] if years else "",
            "nsq_distinct_products": nsq[PRODUCT_COL].str.strip().str.lower().nunique(),
            "name_spellings_seen": d.manufacturer_name.nunique(),
        })
    summary = pd.DataFrame(rows).sort_values("nsq_incidents", ascending=False)

    ok.to_csv(OUT_DIR / "rows_with_manufacturer_split.csv", index=False, encoding="utf-8-sig")
    review.to_csv(OUT_DIR / "manufacturer_needs_review.csv", index=False, encoding="utf-8-sig")
    nomfr.to_csv(OUT_DIR / "rows_no_manufacturer.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "manufacturer_summary.csv", index=False, encoding="utf-8-sig")

    print("\n--- RESULT ---")
    print(df.groupby(["category", "split_status"]).size().to_string())
    print("\nReporting source after cleaning:")
    print(df.source_clean.value_counts().to_string())
    print(f"\nFactories (draft keys): {len(summary)}")
    print(f"Files written to: {OUT_DIR}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    run(ap.parse_args().preview)
