# SEC Financial Data Cleaning & Validation Pipeline

## Overview

This project builds a reproducible Python pipeline for downloading, cleaning, canonicalizing, and validating SEC Company Facts data across 30 public companies. It turns nested XBRL Company Facts JSON into a tabular dataset of standard 10-K and 10-Q financial observations, with diagnostics for entity-resolution problems, repeated facts, value conflicts, format checks, and financial anomaly review flags.

## Data Source

The pipeline uses:

- the [SEC Company Facts API](https://data.sec.gov/api/xbrl/companyfacts/)
- the SEC [company ticker-to-CIK mapping](https://www.sec.gov/files/company_tickers.json)

Requests include a User-Agent header as required by SEC fair-access guidance.

## Company Universe

The project covers 30 companies across technology, financials, energy, consumer, healthcare, industrials, and telecom/media:

```text
AAPL, MSFT, NVDA, GOOGL, META, ORCL,
JPM, GS, BAC, MS, C, BLK,
XOM, CVX, COP,
WMT, COST, HD, MCD, NKE,
JNJ, PFE, MRK, UNH,
CAT, GE, HON, UPS,
VZ, DIS
```

## Pipeline

1. **Ticker-to-CIK resolution**  
   Resolve each ticker through `config/verified_entities.csv` when present; otherwise use the SEC ticker mapping. CIKs are zero-padded to 10 digits for the Company Facts API.

2. **SEC Company Facts download**  
   Download each issuer’s Company Facts JSON and write accepted files to `data/raw/`.

3. **Entity quality checks and quarantine**  
   Before accepting a download, inspect file size, us-gaap concept/observation counts, and 10-K/10-Q depth. Candidates that are unusually sparse relative to the peer universe are quarantined under `data/quarantine/` instead of entering the main raw dataset.

4. **Verified entity mapping**  
   Manually reviewed ticker-to-CIK corrections live in `config/verified_entities.csv` and take priority over automatic mapping on subsequent downloads.

5. **Flatten nested us-gaap facts**  
   Expand `facts["us-gaap"]` concepts, units, and observations into one row per observation.

6. **Keep only 10-K and 10-Q observations**  
   Amended forms such as `10-K/A` and `10-Q/A`, as well as other form types, are excluded from the cleaned dataset.

7. **Canonicalize repeated financial facts**  
   Group rows by `ticker + concept + unit + start + end`, sort by `filed`, and keep the most recently filed observation for each group.

8. **Validation checks**  
   Confirm form coverage, duplicate candidate keys, missing critical fields, datetime/numeric/string hygiene, and related quality metrics on the cleaned table.

9. **Financial anomaly review flags**  
   Compare consecutive observations within `ticker + concept + unit` groups and write broad and high-priority review extracts under `output/`.

10. **Quality reports**  
    Write compact summaries and review extracts under `output/`.

## Data Quality Issues Handled

- **Repeated historical facts** across multiple later filings for the same reporting period
- **Conflicting values** when later filings revise an earlier reported amount for the same fact key
- **Ticker-to-CIK entity mismatch** for XOM, where automatic mapping resolved to a sparse incorrect entity
- **Suspicious sparse entity data** quarantined by cross-sectional quality checks
- **Reviewed mapping** used to restore the correct Exxon Mobil issuer (`0000034088`)
- **`filed < end` rows** retained and flagged as review warnings, because many are legitimate forward-looking or incomplete-period disclosures rather than simple data errors

## Format and Type Validation

After cleaning, the dataset is checked for:

- datetime parsing of `start`, `end`, and `filed`
- numeric validity of `val`
- null values in critical fields
- empty strings
- leading/trailing whitespace in key categorical fields (`ticker`, `concept`, `unit`, `form`)

Final results from the current run:

| Check | Result |
|-------|-------:|
| Datetime parsing failures | 0 |
| Non-numeric `val` entries | 0 |
| Critical null / empty / whitespace issues | 0 |

## Financial Anomaly Detection

Anomaly detection is performed within groups defined by:

```text
ticker + concept + unit
```

Observations are ordered by `end` date and compared with the previous observation in the same group. Absolute value ratios are used when both values are non-zero.

Two review tiers are produced:

**Broad review**

- absolute value ratio `>= 100` or `<= 0.01`

**High priority**

- absolute value ratio `>= 1000` or `<= 0.001`
- and the group must contain at least 4 observations

Anomaly flags are **review signals only**. They are **not** automatically deleted: large financial changes can be economically valid (for example debt issuance/repayment, acquisitions, restructuring, or share-count events).

Results from the current run:

| Metric | Value |
|--------|------:|
| Comparable observations | 430,168 |
| Broad anomaly flags | 2,068 |
| High-priority anomaly flags | 349 |
| Review-tier flags | 1,719 |

## Final Results

Results from the current 30-company run:

| Metric | Value |
|--------|------:|
| Raw flattened rows | 881,417 |
| Rows after 10-K / 10-Q filtering | 848,733 |
| Cleaned rows | 448,690 |
| Rows removed by canonicalization | 400,043 |
| Companies (tickers) | 30 |
| Unique concepts | 3,999 |
| Value conflict groups | 21,831 |
| Conflict rate | 4.87% |
| Duplicate candidate fact keys after cleaning | 0 |
| Missing critical fields | 0 |
| Filed-before-end review warnings | 36 |
| Financial anomaly flags | 2,068 |
| High-priority anomaly flags | 349 |

## Sample Output

The full cleaned dataset (`data/processed/companyfacts_clean.csv`) is approximately **95 MB** and is not stored in Git.

A representative sample is available at:

```text
data/sample/companyfacts_clean_sample.csv
```

The sample contains **600 rows**: up to **20** randomly sampled cleaned observations per ticker (`random_state=1`), preserving all 15 columns from the cleaned dataset.

## Repository Structure

```text
config/           Verified ticker-to-CIK mappings for reviewed entities
data/raw/         Accepted raw Company Facts JSON (local; excluded from Git)
data/processed/   Flattened and cleaned CSV outputs (local; excluded from Git)
data/quarantine/  Suspicious downloads held out of the main pipeline
data/sample/      Small cleaned sample for GitHub inspection
output/           Validation summaries and quality reports
src/              Download, cleaning, and validation scripts
requirements.txt  Python dependencies
```

## Key Output Files

| File | Purpose |
|------|---------|
| `output/data_quality_report.csv` | Project-level metric summary |
| `output/validation_summary.csv` | Compact validation snapshot |
| `output/filed_before_period_end.csv` | Rows flagged for `filed < end` review |
| `output/financial_anomaly_flags.csv` | Broad financial anomaly review flags |
| `output/high_priority_anomaly_review.csv` | High-priority anomaly subset for manual review |
| `config/verified_entities.csv` | Manually verified ticker-to-CIK overrides |

## How to Run

```bash
pip install -r requirements.txt
python src/download_data.py
python src/clean_data.py
python src/validate_data.py
```

These scripts regenerate local raw JSON under `data/raw/`, processed tables under `data/processed/`, and reports under `output/`. Optional download subsetting is available via `--tickers` on the download script.

## Notes

Large raw and processed files are intentionally excluded from Git through `.gitignore`, while directory placeholders (`.gitkeep`), configuration, sample output, and small quality-report artifacts remain in the repository.
