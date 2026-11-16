# Data folder

Every file used to build the project, in the order the work was done:

**download → split names → match factories → score and analyse → final workbook** (`../CDSCO_NSQ_Project_Workbook.xlsx`)

All data comes from public CDSCO records. Raw counts only: these are not failure rates. An NSQ result applies to one tested batch, not to a whole product or company.

## 1_downloaded: data as received from CDSCO

| File | What it is |
|---|---|
| `nsq_drugs.csv` | All 6,956 NSQ alert rows from the CDSCO online NSQ portal, Jan 2019 to Sep 2026 (93 months). One row per failed batch. |
| `spurious_drugs.csv` | All 49 spurious-drug rows from the same portal (15 months). |
| `quality_report.json` | Download check: months found, months downloaded, months failed (0) and blank critical fields (0). |
| `alerts_manifest.csv` | List of the 300 documents on the CDSCO drug alerts page: 212 NSQ, spurious and revised alert PDFs that were downloaded, and 88 unrelated documents (circulars, lab lists) that were skipped as "not_needed". Each row has the release date and a SHA-256 fingerprint. The PDFs themselves are not stored here; they can be downloaded from CDSCO. |
| `raw_json/nsq/` and `raw_json/spurious/` | The original monthly responses from the portal, one file per month (93 + 15 files), kept so the CSVs can be checked against the source. In the CSVs, the `raw_file` column still shows the original folder name (`cdsco_nsq_portal\raw\...`); those files are in `raw_json/` here. |

## 2_manufacturer_matching: splitting names and matching factories (version 2)

| File | What it is |
|---|---|
| `rows_with_manufacturer_split.csv` | 6,842 alert rows where the "manufactured by" text was split into company name and address. |
| `manufacturer_needs_review.csv` | 95 rows the rules could not split. Kept, not deleted. |
| `rows_no_manufacturer.csv` | 68 rows with no real manufacturer ("Under Investigation", "Not applicable"). |
| `manufacturer_summary.csv` | First grouping by name and place, before spelling variants were merged (2,588 groups). |
| `factory_table.csv` | Final 2,315 factories under 1,881 companies, after merging spelling variants within the same PIN-code area. |
| `row_to_factory.csv` | Links each alert row to its factory and company id. |
| `match_check_sample.csv` | 100 merged name pairs checked by hand: 97 judged the same factory (97% precision). |

Check: 6,956 NSQ rows + 49 spurious rows = 7,005 rows downloaded = 6,842 split + 95 needs review + 68 no manufacturer.

## 3_analysis_outputs: results of each analysis step

| File | What it is |
|---|---|
| `analysis_results.xlsx` | Risk scores, failure categories, time-until-next-failure (Kaplan-Meier) results and backtest. |
| `score_variant_check.xlsx` | Comparison of risk-score weightings in time-split backtests (used to choose the weights). |
| `count_model_results.xlsx` | Count model (zero-truncated Negative Binomial) estimates. |
| `hotspot_results.xlsx` | District hotspot test (Getis-Ord Gi*) results. |

## archive_version1: first attempt, superseded

The first name-splitting method. It is kept to show how the method improved, and is **not used in any result**.

| | Version 1 | Version 2 (used) |
|---|---|---|
| Rows split into name and address | 3,051 | 6,842 |
| Rows left for manual review | 3,954 | 95 |

Files: `rows_with_manufacturer_split.csv`, `manufacturer_needs_review.csv`, `manufacturer_summary.csv`.

## Not included
- The raw CDSCO alert PDFs (about 60 MB): listed in `1_downloaded/alerts_manifest.csv` and available from CDSCO.
- Downloader progress files (`checkpoint.json`) and saved copies of the CDSCO web pages.
