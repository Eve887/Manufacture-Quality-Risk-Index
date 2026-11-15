"""
cdsco_alerts_downloader.py

Step 1 of the Manufacturer Quality Risk Index project.

What it does:
  1. Opens the CDSCO Alerts page and saves a dated copy of it (evidence).
  2. Reads every row of the alerts table: title, release date, file size, download link.
  3. Sorts each row into a category: central_nsq, state_nsq, spurious, revised,
     combined, or other.
  4. Downloads the files in the categories we need into dated folders.
  5. Saves progress after every file (checkpoint), so if it stops or you press
     Ctrl+C, the next run continues where it left off.
  6. Writes a manifest (a CSV list of every row, its category and what happened to it).

How to run (in the project folder, with the virtual environment active):
  python cdsco_alerts_downloader.py --dry-run     <- only list and sort, no downloads
  python cdsco_alerts_downloader.py               <- list, sort and download
"""

import base64
import csv
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, quote

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Settings (constants). Change these if you need to; nothing else needs editing.
# ---------------------------------------------------------------------------
ALERTS_URL = "https://cdsco.gov.in/opencms/opencms/en/Alerts/"

BASE_FOLDER = Path("cdsco_alerts")              # everything is saved under this folder
PDF_FOLDER = BASE_FOLDER / "files"              # the downloaded files
SNAPSHOT_FOLDER = BASE_FOLDER / "page_snapshots"  # dated copies of the Alerts page
CHECKPOINT_FILE = BASE_FOLDER / "checkpoint.json"
MANIFEST_FILE = BASE_FOLDER / "manifest.csv"

# Which categories to download. "other" (medical device alerts, guidelines,
# circulars and so on) is listed in the manifest but not downloaded.
DOWNLOAD_CATEGORIES = {"central_nsq", "state_nsq", "spurious", "revised", "combined"}

WAIT_BETWEEN_FILES = 1.5   # seconds to wait between downloads (be polite to the server)
TIMEOUT = 60               # seconds to wait for the server before giving up on one try
MAX_TRIES = 4              # how many times to try each file before marking it failed

HEADERS = {
    # Some government servers refuse requests that do not look like a browser.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Titles containing any of these words are never monthly NSQ/spurious lists.
EXCLUDE_WORDS = [
    "guideline", "guidance", "reward", "survey", "recall", "theft",
    "falsified", "faisified", "circular", "ba-be", "ba/be", "new link",
]

# Device-only alerts. BUT the monthly lists are titled "List of Drugs, Medical
# Devices, Vaccine and Cosmetics declared as ...", so these words only exclude
# a title when it does NOT also contain "declared".
DEVICE_WORDS = ["medical device", "ivd", "in-vitro", "invitro", "noc "]

DATE_PATTERN = re.compile(r"\b\d{4}-[A-Za-z]{3}-\d{1,2}\b")   # e.g. 2025-Jul-18
SIZE_PATTERN = re.compile(r"^[\d,\.]+\s*kb$", re.IGNORECASE)  # e.g. 185 KB, 516KB, 1,655kb

# The download link doesn't serve the file itself. It serves a small HTML page
# containing this, pointing at the real file:
#   <iframe src='/opencms/resources/.../some file.pdf' ...></iframe>
IFRAME_SRC_PATTERN = re.compile(r"""<iframe[^>]+src\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small helper functions
# ---------------------------------------------------------------------------
def log(message):
    """Print a message with the current time in front of it.
    Some alert titles contain characters the Windows console/redirected-output
    encoding can't display (seen in practice: a mis-encoded en-dash). Logging
    must never be what crashes the crawler, so fall back to a safe rendering
    instead of letting print() raise."""
    line = f"[{datetime.now():%H:%M:%S}] {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(line.encode(encoding, errors="replace").decode(encoding), flush=True)


def save_json_safely(data, path):
    """Write JSON to a temporary file first, then swap it in.
    If the computer stops halfway through writing, the old file is still intact."""
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    temp_path.replace(path)


def load_checkpoint():
    """Read the checkpoint file if it exists; otherwise start with an empty dictionary."""
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def decode_file_id(num_id):
    """CDSCO links end in ?num_id=MTI5Mjc= . That text is base64 for the number 12927.
    We decode it to get a short, permanent ID for each file."""
    try:
        padded = num_id + "=" * (-len(num_id) % 4)   # base64 needs a length divisible by 4
        return base64.b64decode(padded).decode("ascii")
    except Exception:
        return num_id   # if decoding fails, keep the original text as the ID


def find_alert_month(title):
    """Find the month and year an alert refers to, from its title.
    'NSQ ALERT FOR THE MONTH OF MAY-2025' -> '2025-05'. Returns '' if not found."""
    text = title.lower()
    year_match = re.search(r"\b(20\d{2})\b", text)
    if not year_match:
        return ""
    month_number = None
    for word in re.findall(r"[a-z]+", text):
        if word[:3] in MONTHS and (len(word) == 3 or word.startswith(
                ("janu", "febr", "marc", "apri", "may", "june", "july",
                 "augu", "sept", "octo", "nove", "dece"))):
            month_number = MONTHS[word[:3]]
            break
    if month_number is None:
        return ""
    return f"{year_match.group(1)}-{month_number:02d}"


def classify_title(title):
    """Sort one alert title into a category. The order of the checks matters."""
    text = title.lower()

    if any(word in text for word in EXCLUDE_WORDS):
        return "other"
    if "declared" not in text and any(word in text for word in DEVICE_WORDS):
        return "other"
    if "samples declared nsq" in text or "2017-2023" in text:
        return "combined"
    if re.search(r"\brevis", text):          # 'Revise list ...', '(Revised)'
        return "revised"
    if re.search(r"\bstate\b", text):        # 'STATE NSQ ALERT', 'State Drug Alert', 'State Labs'
        return "state_nsq"
    if "spurious" in text and "not of standard quality" not in text:
        return "spurious"
    if re.search(r"\bnsq\b|not of standard|sub standard|drugs? alerts?", text):
        return "central_nsq"
    return "other"


def parse_size_kb(size_text):
    """'1,655kb' -> 1655.0 ; '' -> None"""
    digits = re.sub(r"[^\d\.]", "", size_text)
    return float(digits) if digits else None


# ---------------------------------------------------------------------------
# Step A: read the Alerts page
# ---------------------------------------------------------------------------
def fetch_with_retries(session, url, stream=False):
    """Ask the server for a URL. If it fails, wait and try again (2, 4, 8 seconds)."""
    for attempt in range(1, MAX_TRIES + 1):
        try:
            response = session.get(url, timeout=TIMEOUT, stream=stream)
            if response.status_code == 200:
                return response
            last_problem = f"HTTP status {response.status_code}"
            response.close()
        except requests.RequestException as error:
            last_problem = f"{type(error).__name__}: {error}"
        if attempt < MAX_TRIES:
            wait = 2 ** attempt
            log(f"   try {attempt} failed ({last_problem}); waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError(last_problem)


def read_alert_rows(html):
    """Find every download link on the page and read the row it sits in."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    seen_ids = set()

    for link in soup.find_all("a", href=True):
        if "download_file_division.jsp" not in link["href"]:
            continue

        full_url = urljoin(ALERTS_URL, link["href"])
        num_id = parse_qs(urlparse(full_url).query).get("num_id", [""])[0]
        file_id = decode_file_id(num_id)
        if file_id in seen_ids:
            continue          # same file linked twice: keep only the first
        seen_ids.add(file_id)

        # Climb up from the link to the table row (<tr>) that contains it.
        row = link.find_parent("tr")
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")] if row else []

        date_text, size_text, title = "", "", ""
        link_text = link.get_text(" ", strip=True)
        for cell in cells:
            if not date_text and DATE_PATTERN.fullmatch(cell):
                date_text = cell
            elif not size_text and SIZE_PATTERN.match(cell):
                size_text = cell
            elif cell.isdigit() or cell == link_text or "download_file_division" in cell:
                continue      # serial number or the link cell
            elif len(cell) > len(title):
                title = cell  # the longest remaining cell is the title
        if not title:
            title = link_text

        rows.append({
            "file_id": file_id,
            "title": title,
            "release_date": date_text,
            "size_listed": size_text,
            "size_kb_listed": parse_size_kb(size_text),
            "category": classify_title(title),
            "alert_month": find_alert_month(title),
            "url": full_url,
        })
    return rows


# ---------------------------------------------------------------------------
# Step B: download one file
# ---------------------------------------------------------------------------
def guess_extension(first_bytes):
    """Work out the real file type from its first bytes (its 'signature')."""
    if first_bytes.startswith(b"%PDF"):
        return ".pdf"
    if first_bytes.startswith(b"PK"):
        return ".zip"        # also .xlsx/.docx; rename by hand if needed
    if first_bytes.startswith(b"\xd0\xcf\x11\xe0"):
        return ".doc"        # old Word or Excel format
    if first_bytes.lstrip()[:1] == b"<":
        return ".html"       # usually an error page, not a real file
    return ".bin"


def target_path(row, extension):
    """files/<category>/<year>/<alert_month>_<category>_id<file_id><ext>"""
    month = row["alert_month"] or "unknown-month"
    year = month[:4] if row["alert_month"] else "unknown-year"
    name = f"{month}_{row['category']}_id{row['file_id']}{extension}"
    return PDF_FOLDER / row["category"] / year / name


def resolve_download_url(session, jsp_url):
    """The CDSCO download link doesn't serve the file itself. It serves a small
    HTML wrapper page with an <iframe src="..."> pointing at the real file, and
    that real path often has raw spaces in it (e.g. 'some file.pdf').

    Fetches the wrapper and returns (real_url, wrapper_body):
      - if an iframe src is found: (fully-qualified, percent-encoded real URL, None)
        -- None tells the caller to fetch real_url fresh.
      - if no iframe is found (the link served something else - the file directly,
        or an unrecognized error page): (jsp_url, the bytes already fetched)
        -- the caller reuses those bytes instead of fetching twice.
    """
    response = fetch_with_retries(session, jsp_url, stream=False)
    body = response.content
    response.close()

    match = IFRAME_SRC_PATTERN.search(body.decode("utf-8", errors="replace"))
    if not match:
        return jsp_url, body

    raw_src = match.group(1).strip()
    parsed = urlparse(raw_src)
    # Percent-encode each path segment (handles raw spaces/unsafe characters)
    # without touching the "/" separators.
    safe_path = "/".join(quote(segment) for segment in parsed.path.split("/"))
    real_url = urljoin(jsp_url, parsed._replace(path=safe_path).geturl())
    return real_url, None


def download_one(session, row):
    """Download one file safely. Returns a dictionary describing the result."""
    real_url, wrapper_body = resolve_download_url(session, row["url"])

    temp_path = PDF_FOLDER / f"_partial_{row['file_id']}.part"
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256()
    total_bytes = 0
    first_bytes = b""

    try:
        if wrapper_body is not None:
            # The link served real content directly (no iframe to follow) - reuse
            # the bytes we already have instead of fetching the same URL twice.
            with open(temp_path, "wb") as f:
                f.write(wrapper_body)
            sha.update(wrapper_body)
            total_bytes = len(wrapper_body)
            first_bytes = wrapper_body[:8]
        else:
            response = fetch_with_retries(session, real_url, stream=True)
            with open(temp_path, "wb") as f:          # "wb" = write binary (bytes, not text)
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if len(first_bytes) < 8:
                        first_bytes += chunk[:8]
                    f.write(chunk)
                    sha.update(chunk)
                    total_bytes += len(chunk)
            response.close()

        extension = guess_extension(first_bytes)
        final_path = target_path(row, extension)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.replace(final_path)   # only now does the file get its real name
    except Exception:
        temp_path.unlink(missing_ok=True)   # don't leave a half-written file behind
        raise

    status = "done" if extension != ".html" else "suspect_html"
    return {
        "status": status,
        "path": str(final_path),
        "bytes": total_bytes,
        "sha256": sha.hexdigest(),
        "file_type": extension,
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        "resolved_url": real_url,
    }


# ---------------------------------------------------------------------------
# Step C: write the manifest
# ---------------------------------------------------------------------------
MANIFEST_COLUMNS = [
    "file_id", "category", "alert_month", "title", "release_date", "size_listed",
    "status", "path", "bytes", "file_type", "sha256", "downloaded_at", "resolved_url",
    "error", "url",
]


def write_manifest(rows, checkpoint):
    with open(MANIFEST_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            record = dict(row)
            record.update(checkpoint.get(row["file_id"], {}))
            if "status" not in record:
                record["status"] = ("not_needed" if row["category"] not in DOWNLOAD_CATEGORIES
                                    else "pending")
            writer.writerow(record)


# ---------------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv
    BASE_FOLDER.mkdir(exist_ok=True)
    SNAPSHOT_FOLDER.mkdir(exist_ok=True)

    session = requests.Session()        # reuses one connection for every request
    session.headers.update(HEADERS)

    # A. Read the Alerts page and keep a dated copy as evidence.
    log(f"Opening {ALERTS_URL}")
    page = fetch_with_retries(session, ALERTS_URL)
    snapshot = SNAPSHOT_FOLDER / f"alerts_{datetime.now():%Y-%m-%d_%H%M%S}.html"
    snapshot.write_bytes(page.content)
    rows = read_alert_rows(page.text)
    log(f"Found {len(rows)} files on the page. Page copy saved to {snapshot}")

    if not rows:
        log("No download links found. The page layout may have changed; open the "
            "snapshot file in a browser and check.")
        return

    counts = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    for category, number in sorted(counts.items()):
        log(f"   {category:12} {number}")

    checkpoint = load_checkpoint()

    if dry_run:
        write_manifest(rows, checkpoint)
        log(f"Dry run only. Check the categories in {MANIFEST_FILE}, then run without --dry-run.")
        return

    # B. Decide which files still need downloading.
    to_download = []
    for row in rows:
        if row["category"] not in DOWNLOAD_CATEGORIES:
            continue
        saved = checkpoint.get(row["file_id"])
        # Only "done" is a finished result. "suspect_html" means we got something
        # that isn't the real file - always worth retrying on the next run rather
        # than treating it as permanently settled.
        if saved and saved.get("status") == "done" and Path(saved["path"]).exists():
            continue    # already finished in an earlier run
        to_download.append(row)

    log(f"{len(to_download)} files to download "
        f"({sum(1 for r in rows if r['category'] in DOWNLOAD_CATEGORIES) - len(to_download)} "
        f"already done).")

    # C. Download, saving progress after every file.
    start = time.time()
    try:
        for number, row in enumerate(to_download, start=1):
            label = f"[{number}/{len(to_download)}] {row['category']:11} {row['title'][:60]}"
            try:
                result = download_one(session, row)
                checkpoint[row["file_id"]] = result
                log(f"{label}  OK {result['bytes'] // 1024} KB"
                    + ("  (looks like a web page, not a file - check it)"
                       if result["status"] == "suspect_html" else ""))
            except Exception as error:
                checkpoint[row["file_id"]] = {"status": "failed", "error": str(error)}
                log(f"{label}  FAILED: {error}")

            save_json_safely(checkpoint, CHECKPOINT_FILE)

            elapsed = time.time() - start
            remaining = (elapsed / number) * (len(to_download) - number)
            if number % 10 == 0 or number == len(to_download):
                log(f"   progress {number}/{len(to_download)}, about {remaining / 60:.1f} min left")

            if number < len(to_download):
                time.sleep(WAIT_BETWEEN_FILES)

    except KeyboardInterrupt:
        log("Stopped by Ctrl+C. Progress is saved; run the script again to continue.")

    finally:
        # This block always runs: after success, after an error, and after Ctrl+C.
        save_json_safely(checkpoint, CHECKPOINT_FILE)
        write_manifest(rows, checkpoint)
        statuses = {}
        for record in checkpoint.values():
            statuses[record["status"]] = statuses.get(record["status"], 0) + 1
        log(f"Summary: {statuses}. Manifest: {MANIFEST_FILE}")


if __name__ == "__main__":
    main()
