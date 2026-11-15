"""
cdsco_nsq_portal_downloader.py

Step 1b of the Manufacturer Quality Risk Index project.

cdsco_alerts_downloader.py gets the monthly NSQ/spurious-drug PDFs that CDSCO
published on its Alerts page. That page stopped carrying them after June 2025;
since July 2025 the same data lives on a different site, cdscoonline.gov.in,
as structured JSON records (one row per drug sample) instead of PDFs. This
script reads that portal directly - there is no document to download, parse,
or OCR, because each row already *is* the record.

It also backfills everything that portal holds: NSQ drug records since 2019,
spurious-drug records since mid-2025 (the portal has nothing earlier - that
table appears to have started then).

What it does:
  1. Opens the portal once to pick up its session cookie.
  2. Asks the portal which years and months actually have data - it reports
     real coverage, not a fixed calendar, so a year/month with nothing in it
     is simply absent from the list, not an error.
  3. For every (table, year, month) the portal reports, downloads that
     month's full row set as JSON and saves the raw response as evidence.
  4. Builds two flat CSVs (nsq_drugs.csv, spurious_drugs.csv) from everything
     saved so far.
  5. Checkpoints after every month - interrupt-safe and resumable. Finished
     months are skipped on the next run EXCEPT the most recent month for each
     table, which is still open and may still receive new rows, so it is
     always re-checked.

A quirk worth knowing (found while building this): the portal's own dropdowns
send month values in mixed case ("Jun-2025"), and that is also what the
portal's backend expects - sending an upper-cased variant ("JUN-2025") has
been observed to abruptly drop the connection instead of returning an error.
This script always uses the exact month label the portal itself reports, and
never re-cases it, so this should not come up in normal operation. If you see
a connection_failure in the log, that is a sign something upstream changed.

How to run (in the project folder, with the virtual environment active):
  python cdsco_nsq_portal_downloader.py --dry-run       <- show coverage, no downloads
  python cdsco_nsq_portal_downloader.py                 <- fetch everything new
  python cdsco_nsq_portal_downloader.py --refresh-all   <- re-fetch every month
"""

import csv
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Settings (constants). Change these if you need to; nothing else needs editing.
# ---------------------------------------------------------------------------
BASE = "https://cdscoonline.gov.in"
NSQ_PAGE = f"{BASE}/CDSCO/viewPublicNSQDrug"   # loading this gives us a session cookie

BASE_FOLDER = Path("cdsco_nsq_portal")
RAW_FOLDER = BASE_FOLDER / "raw"                # one JSON file per (table, year, month): evidence
SNAPSHOT_FOLDER = BASE_FOLDER / "page_snapshots"
CHECKPOINT_FILE = BASE_FOLDER / "checkpoint.json"
QUALITY_REPORT_FILE = BASE_FOLDER / "quality_report.json"

WAIT_BETWEEN_CALLS = 2.0   # seconds between requests - this portal has shown itself to be fragile
TIMEOUT = 60
MAX_TRIES = 4

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}
AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": NSQ_PAGE,
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# Everything specific to one table (NSQ drugs vs. spurious drugs) lives here,
# so the rest of the script can treat both tables the same way.
TABLES = {
    "nsq": {
        "filtered_endpoint": "/CDSCO/filteredNsqDrugTable",
        "csv_file": BASE_FOLDER / "nsq_drugs.csv",
        # Exact field names the portal uses, confirmed from a real response.
        "fields": [
            "str_product_name", "str_brand_name", "str_batch_no",
            "dt_manufacturing_date", "dt_expiry_date", "str_manufactured_by",
            "str_nsq_result", "str_reporting_source",
            "str_reported_by_lab_or_state", "dt_reporting_month_year",
        ],
        # Fields that should basically always be filled in for a usable record.
        "critical_fields": ["str_product_name", "str_batch_no", "str_manufactured_by"],
    },
    "spurious": {
        "filtered_endpoint": "/CDSCO/filteredSpuriousDrugTable",
        "csv_file": BASE_FOLDER / "spurious_drugs.csv",
        "fields": [
            "product_name_from_dtl", "str_brand_name", "str_batch_no",
            "dt_manufacturing_date", "dt_expiry_date", "str_manufactured_by",
            "str_spurious_manufacturer_name", "str_spurious_manufactured_by",
            "str_nsq_result", "str_nsq_remarks", "str_firm_reply",
            "str_reporting_source", "str_reported_by_lab_or_state",
            "dt_reporting_month_year",
        ],
        "critical_fields": ["product_name_from_dtl", "str_batch_no"],
    },
}

# Columns added to every row to say where it came from and how it was fetched.
PROVENANCE_FIELDS = [
    "table", "year", "month_label", "row_index_in_month",
    "retrieval_timestamp", "source_url", "raw_file",
]


# ---------------------------------------------------------------------------
# Small helper functions (same pattern as cdsco_alerts_downloader.py)
# ---------------------------------------------------------------------------
def log(message):
    """Print a message with the current time in front of it.
    Falls back to a safe rendering if the console can't display a character."""
    line = f"[{datetime.now():%H:%M:%S}] {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(line.encode(encoding, errors="replace").decode(encoding), flush=True)


def save_json_safely(data, path):
    """Write JSON to a temporary file first, then swap it in, so an interrupted
    write can never corrupt the previous good file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    temp_path.replace(path)


def load_checkpoint():
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def hash_rows(rows):
    """A stable fingerprint of a month's data, for change detection on re-fetch."""
    canonical = json.dumps(rows, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Step A: talk to the portal
# ---------------------------------------------------------------------------
def establish_session(session):
    """Load the portal page once so the session picks up its JSESSIONID cookie
    (the same thing a browser does on every visit - no login or credentials
    are involved; the data is public)."""
    response = fetch_with_retries(session, NSQ_PAGE)
    SNAPSHOT_FOLDER.mkdir(parents=True, exist_ok=True)
    snapshot = SNAPSHOT_FOLDER / f"portal_{datetime.now():%Y-%m-%d_%H%M%S}.html"
    snapshot.write_bytes(response.content)
    return snapshot


def fetch_with_retries(session, url, params=None, expect_json=False):
    """GET a URL, retrying plausibly-transient failures with backoff.
    Raises RuntimeError with a classified reason if every attempt fails."""
    last_problem = "unknown error"
    for attempt in range(1, MAX_TRIES + 1):
        try:
            response = session.get(url, params=params, headers=AJAX_HEADERS,
                                     timeout=TIMEOUT)
        except requests.exceptions.ConnectionError as error:
            # Includes the "server dropped the connection" failure mode this
            # portal has shown in practice. Treated as transient and retried.
            last_problem = f"connection_failure: {error}"
            response = None
        except requests.RequestException as error:
            last_problem = f"{type(error).__name__}: {error}"
            response = None

        if response is not None:
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else 2 ** attempt
                log(f"   HTTP 429 (rate limited); waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            if response.status_code == 200:
                if expect_json and "json" not in response.headers.get("Content-Type", "").lower():
                    # Most likely the session lapsed and we got an HTML page
                    # back instead of data. Re-establish it and try again.
                    last_problem = "unexpected_content_type (session may have expired)"
                    establish_session(session)
                else:
                    return response
            else:
                last_problem = f"http_{response.status_code}"
            response.close()

        if attempt < MAX_TRIES:
            wait = 2 ** attempt
            log(f"   try {attempt} failed ({last_problem}); waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError(last_problem)


def get_years(session, table):
    response = fetch_with_retries(session, BASE + "/CDSCO/reportingYears",
                                    params={"tab": table}, expect_json=True)
    return response.json()


def get_months(session, table, year):
    """Mixed-case month labels ('Jan', 'Feb', ...) for which the portal
    actually has data in this (table, year). An empty list means no data,
    not an error - the portal's coverage, not a fixed calendar."""
    response = fetch_with_retries(session, BASE + "/CDSCO/publicReportingMonths",
                                    params={"tab": table, "year": year}, expect_json=True)
    return response.json()


def fetch_month_rows(session, table, year, month_label):
    """Download one (table, year, month)'s full row set.
    IMPORTANT: month_label must be exactly what the portal itself returned
    from get_months() - do not upper()/title() it. An all-caps variant has
    been observed to crash the server's connection instead of erroring cleanly."""
    endpoint = TABLES[table]["filtered_endpoint"]
    params = {"month": f"{month_label}-{year}", "source": "All", "tab": table}
    url = BASE + endpoint
    response = fetch_with_retries(session, url, params=params, expect_json=True)
    data = response.json()
    full_url = response.url
    total = data.get("iTotalRecords")
    rows = data.get("aaData", [])
    if total is not None and total != len(rows):
        log(f"   NOTE: {table} {month_label}-{year} reports iTotalRecords={total} "
            f"but returned {len(rows)} rows - saving what was returned.")
    return rows, full_url


# ---------------------------------------------------------------------------
# Step B: discovery (what months exist) and the checkpoint key for one
# ---------------------------------------------------------------------------
def checkpoint_key(table, year, month_label):
    return f"{table}:{year}:{month_label}"


def discover_work_items(session):
    """Ask the portal for its real coverage: every (table, year, month) that
    has at least been reported as existing. Returns them oldest-first within
    each table, and also says which one is the latest per table (that one
    always gets re-fetched, since it's still an open reporting period)."""
    work_items = []
    latest_key = {}
    for table in TABLES:
        years = sorted(get_years(session, table))
        time.sleep(WAIT_BETWEEN_CALLS)
        table_items = []
        for year in years:
            months = get_months(session, table, year)
            time.sleep(WAIT_BETWEEN_CALLS)
            for month_label in months:
                table_items.append((table, year, month_label))
        work_items.extend(table_items)
        if table_items:
            latest_key[table] = checkpoint_key(*table_items[-1])
    return work_items, latest_key


# ---------------------------------------------------------------------------
# Step C: build the flat CSVs from everything saved so far
# ---------------------------------------------------------------------------
def write_csvs(checkpoint):
    """Rebuild both CSVs from the raw evidence files recorded in the
    checkpoint. Re-reading rather than accumulating in memory across the run
    keeps this correct even after a Ctrl+C partway through, and the row
    volume here (tens of thousands at most) is small enough that re-reading
    everything each time is cheap."""
    for table, config in TABLES.items():
        columns = PROVENANCE_FIELDS + config["fields"]
        csv_path = config["csv_file"]
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = csv_path.with_suffix(".tmp")
        written = 0
        with open(temp_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for key, record in sorted(checkpoint.items()):
                if not key.startswith(f"{table}:") or record.get("status") != "done":
                    continue
                raw_path = Path(record["raw_file"])
                if not raw_path.exists():
                    continue
                with open(raw_path, encoding="utf-8") as rf:
                    rows = json.load(rf)
                for index, row in enumerate(rows):
                    out = dict(row)
                    out.update({
                        "table": table,
                        "year": record["year"],
                        "month_label": record["month_label"],
                        "row_index_in_month": index,
                        "retrieval_timestamp": record["retrieved_at"],
                        "source_url": record["source_url"],
                        "raw_file": record["raw_file"],
                    })
                    writer.writerow(out)
                    written += 1
        temp_path.replace(csv_path)
        log(f"   wrote {written} rows -> {csv_path}")


def compute_quality_report(checkpoint, work_items):
    report = {"generated_at": datetime.now().isoformat(timespec="seconds"), "tables": {}}
    for table, config in TABLES.items():
        table_items = [w for w in work_items if w[0] == table]
        done = [w for w in table_items if checkpoint.get(checkpoint_key(*w), {}).get("status") == "done"]
        failed = [w for w in table_items if checkpoint.get(checkpoint_key(*w), {}).get("status") == "failed"]
        total_rows = sum(checkpoint.get(checkpoint_key(*w), {}).get("row_count", 0) for w in done)
        blank_counts = {field: 0 for field in config["critical_fields"]}
        for w in done:
            rec = checkpoint[checkpoint_key(*w)]
            raw_path = Path(rec["raw_file"])
            if not raw_path.exists():
                continue
            with open(raw_path, encoding="utf-8") as rf:
                rows = json.load(rf)
            for row in rows:
                for field in config["critical_fields"]:
                    if not str(row.get(field, "")).strip():
                        blank_counts[field] += 1
        report["tables"][table] = {
            "months_discovered": len(table_items),
            "months_done": len(done),
            "months_failed": len(failed),
            "failed_months": [f"{y}-{m}" for (_, y, m) in failed],
            "total_rows": total_rows,
            "blank_critical_field_counts": blank_counts,
        }
    return report


# ---------------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv
    refresh_all = "--refresh-all" in sys.argv
    BASE_FOLDER.mkdir(exist_ok=True)
    RAW_FOLDER.mkdir(exist_ok=True)

    session = requests.Session()
    session.headers.update(HEADERS)

    log(f"Opening {NSQ_PAGE}")
    snapshot = establish_session(session)
    log(f"Session established. Page copy saved to {snapshot}")

    log("Asking the portal which years/months actually have data...")
    work_items, latest_key = discover_work_items(session)

    counts = {}
    for table, year, month_label in work_items:
        counts[(table, year)] = counts.get((table, year), 0) + 1
    for table in TABLES:
        total = sum(1 for w in work_items if w[0] == table)
        log(f"   {table:10} {total} months with data "
            f"(latest: {latest_key.get(table, 'none')})")

    if not work_items:
        log("No months reported by the portal. The contract may have changed; "
            "check the page snapshot.")
        return

    checkpoint = load_checkpoint()

    if dry_run:
        save_json_safely(checkpoint, CHECKPOINT_FILE)  # no-op if unchanged; keeps behaviour uniform
        to_fetch = [w for w in work_items
                    if refresh_all
                    or checkpoint.get(checkpoint_key(*w), {}).get("status") != "done"
                    or checkpoint_key(*w) in latest_key.values()]
        log(f"Dry run only. {len(to_fetch)} of {len(work_items)} months would be fetched "
            f"(the rest are already saved). No data downloaded.")
        return

    to_fetch = [w for w in work_items
                if refresh_all
                or checkpoint.get(checkpoint_key(*w), {}).get("status") != "done"
                or checkpoint_key(*w) in latest_key.values()]
    log(f"{len(to_fetch)} of {len(work_items)} months to fetch "
        f"({len(work_items) - len(to_fetch)} already saved and not the latest month).")

    start = time.time()
    try:
        for number, (table, year, month_label) in enumerate(to_fetch, start=1):
            key = checkpoint_key(table, year, month_label)
            label = f"[{number}/{len(to_fetch)}] {table:10} {month_label}-{year}"
            previous = checkpoint.get(key, {})
            try:
                rows, source_url = fetch_month_rows(session, table, year, month_label)
                digest = hash_rows(rows)
                raw_path = RAW_FOLDER / table / str(year) / f"{month_label}-{year}.json"
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                save_json_safely(rows, raw_path)

                changed_note = ""
                if previous.get("status") == "done" and previous.get("sha256") != digest:
                    changed_note = "  (CHANGED since last time it was fetched)"

                checkpoint[key] = {
                    "status": "done",
                    "table": table,
                    "year": year,
                    "month_label": month_label,
                    "row_count": len(rows),
                    "sha256": digest,
                    "raw_file": str(raw_path),
                    "source_url": source_url,
                    "retrieved_at": datetime.now().isoformat(timespec="seconds"),
                }
                log(f"{label}  OK {len(rows)} rows{changed_note}")
            except Exception as error:
                checkpoint[key] = {
                    "status": "failed", "table": table, "year": year,
                    "month_label": month_label, "error": str(error),
                }
                log(f"{label}  FAILED: {error}")

            save_json_safely(checkpoint, CHECKPOINT_FILE)

            elapsed = time.time() - start
            remaining = (elapsed / number) * (len(to_fetch) - number)
            if number % 10 == 0 or number == len(to_fetch):
                log(f"   progress {number}/{len(to_fetch)}, about {remaining / 60:.1f} min left")

            if number < len(to_fetch):
                time.sleep(WAIT_BETWEEN_CALLS)

    except KeyboardInterrupt:
        log("Stopped by Ctrl+C. Progress is saved; run the script again to continue.")

    finally:
        save_json_safely(checkpoint, CHECKPOINT_FILE)
        write_csvs(checkpoint)
        report = compute_quality_report(checkpoint, work_items)
        save_json_safely(report, QUALITY_REPORT_FILE)
        for table, stats in report["tables"].items():
            log(f"   {table}: {stats['months_done']}/{stats['months_discovered']} months, "
                f"{stats['total_rows']} rows, {stats['months_failed']} failed")
        log(f"Quality report: {QUALITY_REPORT_FILE}")

    if any(record.get("status") == "failed" for record in checkpoint.values()):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
