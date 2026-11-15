"""
cdsco_factory_matcher.py   (Step 12 + 13 of the plan)
Takes rows_with_manufacturer_split.csv (made by cdsco_manufacturer_parser_v2.py) and:
  1. groups spelling variants of the same factory under one factory_id
  2. groups factories of the same company under one company_id
  3. builds the factory table (counts, years, first/last failure, etc.)
  4. writes a 100-pair sample for YOU to check by eye (this gives the precision number for your methods note)

One-time install:   py -m pip install rapidfuzz
Run:                py cdsco_factory_matcher.py
Needs these files in the same folder: manufacturer_split.py, cdsco_manufacturer_parser_v2.py
"""
import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

from manufacturer_split import PIN_RE
from cdsco_manufacturer_parser_v2 import normalise_name, OUT_DIR, PRODUCT_COL

IN_FILE = OUT_DIR / "rows_with_manufacturer_split.csv"
FACTORY_THRESHOLD = 90      # name similarity (0-100) needed to call two names the same factory (same PIN area)
COMPANY_THRESHOLD = 94      # stricter, because there is no location to back it up
SERIOUS_RE = re.compile(r"assay|steril|identif|endotoxin|pyrogen|bacterial|microbial|potency|content", re.I)


# ---------------------------------------------------------------- helpers
def name_core(name):
    n = normalise_name(name)
    n = re.sub(r"\b(PVT|LTD|LLP|INC|CO|COMPANY|THE)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip() or "UNKNOWN"


def get_pin(addr):
    for m in PIN_RE.finditer(addr or ""):
        pin = re.sub(r"\s", "", m.group(0))
        if pin[0] in "123456789":
            return pin
    return ""


def city_words(addr):
    """Letters-only word(s) just before the PIN, or the first comma part when there is no PIN."""
    addr = addr or ""
    m = PIN_RE.search(addr)
    seg = addr[:m.start()] if m else addr.split(",")[0]
    seg = seg.split(",")[-1] if m else seg
    seg = re.sub(r"\(.*?\)", " ", seg)
    seg = re.sub(r"[^A-Za-z ]", " ", seg)
    return re.sub(r"\s+", " ", seg).strip().upper()


class UF:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def link(cores, threshold):
    """Return {core: group_representative}. Links names that are similar AND share the first word
    (or are almost identical)."""
    cores = sorted(set(cores))
    uf = UF(cores)
    if len(cores) > 1:
        sc = process.cdist(cores, cores, scorer=fuzz.token_sort_ratio, score_cutoff=threshold, workers=-1)
        for i, j in np.argwhere(np.triu(sc, 1) > 0):
            a, b = cores[i], cores[j]
            if a.split()[0] == b.split()[0] or sc[i, j] >= 96:
                uf.union(a, b)
    return {c: uf.find(c) for c in cores}


# ---------------------------------------------------------------- main
def main():
    if not IN_FILE.exists():
        sys.exit(f"Not found: {IN_FILE}\nRun cdsco_manufacturer_parser_v2.py first.")
    df = pd.read_csv(IN_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df["core"] = df.manufacturer_name.map(name_core)
    df["pin"] = df.address_part.map(get_pin)
    df["pin3"] = df.pin.str[:3]
    df["city"] = df.address_part.map(city_words)

    # 1. learn which city belongs to which PIN area, using the rows that DO have a PIN
    city_pin3 = defaultdict(Counter)
    for c, p in zip(df.city[df.pin != ""], df.pin3[df.pin != ""]):
        if c:
            city_pin3[c][p] += 1
    inferred = {}
    for c, cnt in city_pin3.items():
        p, n = cnt.most_common(1)[0]
        if n >= 2 and n / sum(cnt.values()) >= 0.8:
            inferred[c] = p

    # 2. blocking: only names in the same area are compared with each other
    blocks, notes = [], []
    for pin3, city, addr in zip(df.pin3, df.city, df.address_part):
        if pin3:
            blocks.append("PIN" + pin3); notes.append("")
        elif city and city in inferred:
            blocks.append("PIN" + inferred[city]); notes.append("pin_area_inferred_from_city")
        elif city:
            blocks.append("CITY:" + city); notes.append("no_pin_city_only")
        else:
            blocks.append("NOLOC"); notes.append("no_location")
    df["block"], df["match_note"] = blocks, notes

    # 3. cluster names inside each block  -> factory
    fkey = {}
    for b, grp in df.groupby("block"):
        mapping = link(grp.core.tolist(), FACTORY_THRESHOLD)
        for core, root in mapping.items():
            fkey[(b, core)] = f"{b}#{root}"
    df["factory_key"] = [fkey[(b, c)] for b, c in zip(df.block, df.core)]

    # 4. cluster names across ALL blocks -> company
    cmap = link(df.core.tolist(), COMPANY_THRESHOLD)
    df["company_root"] = df.core.map(cmap)

    # 5. ids, ordered so that F0001 is the factory with the most NSQ incidents
    df["is_nsq"] = (df.category == "NSQ").astype(int)
    order = df.groupby("factory_key").is_nsq.sum().sort_values(ascending=False, kind="stable")
    fid = {k: f"F{i+1:04d}" for i, k in enumerate(order.index)}
    corder = df.groupby("company_root").is_nsq.sum().sort_values(ascending=False, kind="stable")
    cid = {k: f"C{i+1:04d}" for i, k in enumerate(corder.index)}
    df["factory_id"] = df.factory_key.map(fid)
    df["company_id"] = df.company_root.map(cid)

    # 6. factory table
    rows = []
    for f, d in df.groupby("factory_id", sort=True):
        nsq, spu = d[d.category == "NSQ"], d[d.category == "Spurious"]
        years = sorted({y for y in nsq.year if y})
        state_labs = nsq.loc[nsq.source_clean == "State lab", "str_reported_by_lab_or_state"]
        res = nsq["str_nsq_result"] if "str_nsq_result" in nsq.columns else pd.Series([], dtype=str)
        rows.append({
            "factory_id": f,
            "company_id": d.company_id.iloc[0],
            "factory_name": d.manufacturer_name.mode().iloc[0],
            "name_variants_count": d.manufacturer_name.nunique(),
            "name_variants_example": " || ".join(list(dict.fromkeys(d.manufacturer_name))[:4]),
            "location_known": "no" if d.block.iloc[0] in ("NOLOC",) or d.block.iloc[0].startswith("CITY:") else "yes",
            "pin_example": d.pin[d.pin != ""].mode().iloc[0] if (d.pin != "").any() else "",
            "address_example": d.address_part.mode().iloc[0][:150],
            "nsq_incidents": len(nsq),
            "spurious_incidents": len(spu),
            "nsq_distinct_years": len(years),
            "first_year": years[0] if years else "",
            "last_year": years[-1] if years else "",
            "nsq_distinct_products": nsq[PRODUCT_COL].str.strip().str.lower().nunique(),
            "state_lab_reporting_places": state_labs[state_labs != ""].nunique(),
            "cdsco_lab_incidents": int((nsq.source_clean == "CDSCO lab").sum()),
            "serious_keyword_rows_draft": int(res.str.contains(SERIOUS_RE).sum()) if len(res) else 0,
        })
    ft = pd.DataFrame(rows).sort_values(["nsq_incidents", "spurious_incidents"], ascending=False)
    ft.to_csv(OUT_DIR / "factory_table.csv", index=False, encoding="utf-8-sig")

    keep = [c for c in ["factory_id", "company_id", "category", "year", "manufacturer_name", "address_part",
                        PRODUCT_COL, "str_batch_no", "source_clean", "raw_file", "row_index_in_month",
                        "match_note"] if c in df.columns]
    df[keep].to_csv(OUT_DIR / "row_to_factory.csv", index=False, encoding="utf-8-sig")

    # 7. precision check sample (Step 13): pairs of DIFFERENT spellings that were merged
    pairs = []
    for f, d in df.groupby("factory_id"):
        names = list(dict.fromkeys(d.manufacturer_name))
        if len(names) >= 2:
            rep = d.manufacturer_name.mode().iloc[0]
            for other in names:
                if other != rep:
                    a = d[d.manufacturer_name == rep].address_part.iloc[0]
                    b = d[d.manufacturer_name == other].address_part.iloc[0]
                    pairs.append({"factory_id": f, "name_A": rep, "address_A": a[:90], "name_B": other,
                                  "address_B": b[:90],
                                  "similarity": round(fuzz.token_sort_ratio(name_core(rep), name_core(other)), 1),
                                  "your_check_SAME_or_DIFFERENT": ""})
    chk = pd.DataFrame(pairs)
    if len(chk):
        chk = chk.sample(min(100, len(chk)), random_state=11)
    chk.to_csv(OUT_DIR / "match_check_sample.csv", index=False, encoding="utf-8-sig")

    # 8. summary
    n_draft = df.factory_key_draft.nunique() if "factory_key_draft" in df.columns else None
    print("\n--- FACTORY MATCHING RESULT ---")
    print(f"Rows used                       : {len(df)}")
    if n_draft:
        print(f"Draft factory keys (before)     : {n_draft}")
    print(f"Factories after matching        : {df.factory_id.nunique()}")
    print(f"Companies                       : {df.company_id.nunique()}")
    print(f"Factories with >1 name spelling : {int((ft.name_variants_count > 1).sum())}")
    print(f"Factories with known location   : {int((ft.location_known == 'yes').sum())}")
    print(f"Factories with NSQ in 2+ years  : {int((ft.nsq_distinct_years >= 2).sum())}")
    print(f"Factories with 3+ NSQ incidents : {int((ft.nsq_incidents >= 3).sum())}")
    print("\nTop 10 by NSQ incidents:")
    print(ft[["factory_id", "factory_name", "pin_example", "nsq_incidents", "nsq_distinct_years"]].head(10).to_string(index=False))
    print(f"\nFiles written to {OUT_DIR}: factory_table.csv, row_to_factory.csv, match_check_sample.csv")


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    main()
