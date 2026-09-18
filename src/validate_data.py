"""Validate cleaned SEC financial data."""

from pathlib import Path

import numpy as np
import pandas as pd

CLEAN_PATH = Path("data/processed/companyfacts_clean.csv")
FLAT_PATH = Path("data/processed/companyfacts_flat.csv")
QUARANTINE_DIR = Path("data/quarantine")
VERIFIED_ENTITIES_PATH = Path("config/verified_entities.csv")
OUTPUT_DIR = Path("output")
FILED_BEFORE_END_PATH = OUTPUT_DIR / "filed_before_period_end.csv"
VALIDATION_SUMMARY_PATH = OUTPUT_DIR / "validation_summary.csv"
VALUE_CONFLICT_SUMMARY_PATH = OUTPUT_DIR / "value_conflict_summary.csv"
DATA_QUALITY_REPORT_PATH = OUTPUT_DIR / "data_quality_report.csv"
FINANCIAL_ANOMALY_PATH = OUTPUT_DIR / "financial_anomaly_flags.csv"
HIGH_PRIORITY_ANOMALY_PATH = OUTPUT_DIR / "high_priority_anomaly_review.csv"

FACT_KEY = ["ticker", "concept", "unit", "start", "end"]
CRITICAL_COLS = ["ticker", "concept", "unit", "end", "val", "filed"]
ALLOWED_FORMS = {"10-K", "10-Q"}
HYGIENE_COLS = ["ticker", "concept", "unit", "form"]
DATE_COLS = ["start", "end", "filed"]
ANOMALY_GROUP_COLS = ["ticker", "concept", "unit"]
VALUE_RATIO_HIGH = 100.0
VALUE_RATIO_LOW = 0.01
HIGH_PRIORITY_RATIO_HIGH = 1000.0
HIGH_PRIORITY_RATIO_LOW = 0.001
HIGH_PRIORITY_MIN_GROUP_OBS = 4


def count_datetime_parse_failures(series: pd.Series) -> int:
    """Count non-null values that fail datetime parsing."""
    non_null = series.dropna()
    if non_null.empty:
        return 0
    parsed = pd.to_datetime(non_null, errors="coerce")
    return int(parsed.isna().sum())


def count_invalid_numeric(series: pd.Series) -> int:
    """Count non-null values that cannot be interpreted as numeric."""
    non_null = series.dropna()
    if non_null.empty:
        return 0
    numeric = pd.to_numeric(non_null, errors="coerce")
    return int(numeric.isna().sum())


def string_hygiene_counts(series: pd.Series) -> dict[str, int]:
    """Count nulls, empty strings, and leading/trailing whitespace."""
    null_count = int(series.isna().sum())
    non_null = series.dropna().astype(str)
    empty_string_count = int((non_null == "").sum())
    whitespace_count = int((non_null != non_null.str.strip()).sum())
    return {
        "null": null_count,
        "empty_string": empty_string_count,
        "whitespace": whitespace_count,
    }


def run_format_type_checks(df: pd.DataFrame) -> dict[str, int]:
    """Run date, numeric, and category hygiene checks; return metric dict."""
    print("\n=== Format / type validation ===")

    metrics: dict[str, int] = {}

    print("Datetime parse failures (non-null values that do not parse):")
    for col in DATE_COLS:
        failures = count_datetime_parse_failures(df[col])
        metrics[f"datetime_parse_failures_{col}"] = failures
        print(f"  {col}: {failures}")

    invalid_val = count_invalid_numeric(df["val"])
    metrics["invalid_numeric_val"] = invalid_val
    print(f"\nInvalid / non-numeric val entries: {invalid_val}")

    print("\nString / category hygiene:")
    for col in HYGIENE_COLS:
        counts = string_hygiene_counts(df[col])
        metrics[f"null_{col}"] = counts["null"]
        metrics[f"empty_string_{col}"] = counts["empty_string"]
        metrics[f"whitespace_{col}"] = counts["whitespace"]
        print(
            f"  {col}: null={counts['null']}, "
            f"empty_string={counts['empty_string']}, "
            f"whitespace={counts['whitespace']}"
        )

    return metrics


def detect_financial_anomalies(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """
    Flag unusually large consecutive value changes within
    ticker + concept + unit groups, with review tiers.

    Returns (all_flagged_rows, high_priority_rows, rows_evaluated_for_comparison).
    """
    work = df.copy()
    work["end_dt"] = pd.to_datetime(work["end"], errors="coerce")
    work["val_num"] = pd.to_numeric(work["val"], errors="coerce")
    work = work[work["end_dt"].notna() & work["val_num"].notna()].copy()
    work = work.sort_values(ANOMALY_GROUP_COLS + ["end_dt"]).reset_index(drop=True)

    work["group_observation_count"] = work.groupby(
        ANOMALY_GROUP_COLS, dropna=False
    )["val_num"].transform("size")

    grouped = work.groupby(ANOMALY_GROUP_COLS, dropna=False)["val_num"]
    work["previous_val"] = grouped.shift(1)
    work["absolute_change"] = work["val_num"] - work["previous_val"]

    prev = work["previous_val"]
    work["percentage_change"] = np.where(
        prev.notna() & (prev != 0),
        work["absolute_change"] / prev,
        np.nan,
    )
    work["value_ratio"] = np.where(
        prev.notna() & (prev != 0) & (work["val_num"] != 0),
        work["val_num"].abs() / prev.abs(),
        np.nan,
    )

    comparable = work["previous_val"].notna()
    rows_evaluated = int(comparable.sum())

    ratio = work["value_ratio"]
    broad_mask = comparable & ratio.notna() & (
        (ratio >= VALUE_RATIO_HIGH) | (ratio <= VALUE_RATIO_LOW)
    )
    flagged = work.loc[broad_mask].copy()

    high_priority_mask = (
        (flagged["value_ratio"] >= HIGH_PRIORITY_RATIO_HIGH)
        | (flagged["value_ratio"] <= HIGH_PRIORITY_RATIO_LOW)
    ) & (flagged["group_observation_count"] >= HIGH_PRIORITY_MIN_GROUP_OBS)
    flagged["anomaly_tier"] = np.where(high_priority_mask, "high_priority", "review")

    output_cols = [
        "ticker",
        "concept",
        "label",
        "unit",
        "start",
        "end",
        "val",
        "previous_val",
        "absolute_change",
        "percentage_change",
        "value_ratio",
        "form",
        "filed",
        "group_observation_count",
        "anomaly_tier",
    ]
    flagged["val"] = flagged["val_num"]
    flagged["end"] = flagged["end_dt"]
    flagged = flagged[output_cols].reset_index(drop=True)

    high_priority = flagged[flagged["anomaly_tier"] == "high_priority"].copy()
    review_count = int((flagged["anomaly_tier"] == "review").sum())

    print("\n=== Financial anomaly detection ===")
    print(f"Total rows evaluated (with a previous observation): {rows_evaluated}")
    print(f"Total broad anomaly flags: {len(flagged)}")
    print(f"High-priority anomaly count: {len(high_priority)}")
    print(f"Review-tier count: {review_count}")
    if len(high_priority):
        print("\nHigh-priority counts by ticker:")
        print(high_priority["ticker"].value_counts().to_string())
        print("\nTop 20 high-priority concepts:")
        print(high_priority["concept"].value_counts().head(20).to_string())
    else:
        print("No high-priority anomaly flags detected.")

    return flagged, high_priority, rows_evaluated


def write_data_quality_report(
    *,
    cleaned_rows: int,
    unique_tickers: int,
    unique_concepts: int,
    duplicate_fact_key_rows: int,
    missing_critical_fields: int,
    filed_before_end_warning_rows: int,
    format_metrics: dict[str, int],
    financial_anomaly_flags: int,
    high_priority_anomaly_flags: int,
) -> pd.DataFrame:
    """Build a project-level metric,value quality report from pipeline outputs."""
    flat = pd.read_csv(FLAT_PATH, usecols=["form"])
    raw_flattened_rows = len(flat)
    rows_after_form_filter = int(flat["form"].isin(ALLOWED_FORMS).sum())
    rows_removed_by_canonicalization = rows_after_form_filter - cleaned_rows

    if VALUE_CONFLICT_SUMMARY_PATH.exists():
        value_conflict_groups = len(pd.read_csv(VALUE_CONFLICT_SUMMARY_PATH))
    else:
        value_conflict_groups = 0

    # Candidate fact groups after form filter equal cleaned rows after
    # canonical deduplication (one retained row per fact key).
    value_conflict_rate = (
        round(value_conflict_groups / cleaned_rows, 4) if cleaned_rows else 0.0
    )

    quarantined_tickers = sorted(
        path.stem.removesuffix("_companyfacts")
        for path in QUARANTINE_DIR.glob("*_companyfacts.json")
    )
    quarantined_entities_detected = len(quarantined_tickers)
    quarantined_entity_ticker = "|".join(quarantined_tickers) if quarantined_tickers else ""

    if VERIFIED_ENTITIES_PATH.exists():
        verified_entity_resolution_used = len(pd.read_csv(VERIFIED_ENTITIES_PATH))
    else:
        verified_entity_resolution_used = 0

    rows = [
        {"metric": "raw_flattened_rows", "value": raw_flattened_rows},
        {"metric": "rows_after_form_filter", "value": rows_after_form_filter},
        {"metric": "cleaned_rows", "value": cleaned_rows},
        {
            "metric": "rows_removed_by_canonicalization",
            "value": rows_removed_by_canonicalization,
        },
        {"metric": "unique_tickers", "value": unique_tickers},
        {"metric": "unique_concepts", "value": unique_concepts},
        {"metric": "value_conflict_groups", "value": value_conflict_groups},
        {"metric": "value_conflict_rate", "value": value_conflict_rate},
        {"metric": "duplicate_fact_key_rows", "value": duplicate_fact_key_rows},
        {"metric": "missing_critical_fields", "value": missing_critical_fields},
        {
            "metric": "filed_before_end_warning_rows",
            "value": filed_before_end_warning_rows,
        },
        {
            "metric": "quarantined_entities_detected",
            "value": quarantined_entities_detected,
        },
        {"metric": "quarantined_entity_ticker", "value": quarantined_entity_ticker},
        {
            "metric": "verified_entity_resolution_used",
            "value": verified_entity_resolution_used,
        },
        {
            "metric": "financial_anomaly_flags",
            "value": financial_anomaly_flags,
        },
        {
            "metric": "high_priority_anomaly_flags",
            "value": high_priority_anomaly_flags,
        },
    ]
    for name, value in format_metrics.items():
        rows.append({"metric": name, "value": value})

    report = pd.DataFrame(rows)
    report.to_csv(DATA_QUALITY_REPORT_PATH, index=False)
    print(f"\nSaved data quality report to {DATA_QUALITY_REPORT_PATH}")
    return report


def main() -> None:
    df = pd.read_csv(CLEAN_PATH)

    total_rows = len(df)
    total_columns = len(df.columns)
    unique_tickers = df["ticker"].nunique()
    unique_concepts = df["concept"].nunique()

    print("=== Cleaned dataset summary ===")
    print(f"Total rows: {total_rows}")
    print(f"Total columns: {total_columns}")
    print(f"Unique tickers: {unique_tickers}")
    print(f"Unique concepts: {unique_concepts}")

    duplicate_fact_key_rows = int(df.duplicated(subset=FACT_KEY).sum())
    print("\n=== Duplicate candidate fact keys ===")
    print(f"Duplicate rows remaining: {duplicate_fact_key_rows}")

    print("\n=== Missing values in critical columns ===")
    missing = {col: int(df[col].isna().sum()) for col in CRITICAL_COLS}
    for col, count in missing.items():
        print(f"  {col}: {count}")

    format_metrics = run_format_type_checks(df)

    df["end"] = pd.to_datetime(df["end"], errors="coerce")
    df["filed"] = pd.to_datetime(df["filed"], errors="coerce")

    filed_before_end = df[df["filed"] < df["end"]].copy()
    filed_before_end_warning_rows = len(filed_before_end)
    print("\n=== Review warning: filed < end ===")
    print(f"Rows flagged for review where filed < end: {filed_before_end_warning_rows}")
    print(
        "Warning: these rows may represent legitimate forward-looking or "
        "incomplete-period disclosures (for example future amortization, "
        "lease receivables, credit facilities, or repurchase authorizations). "
        "They require review and should not be deleted automatically."
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filed_before_end.to_csv(FILED_BEFORE_END_PATH, index=False)
    print(f"Saved review rows to {FILED_BEFORE_END_PATH}")

    print("\n=== Row counts by ticker ===")
    print(df["ticker"].value_counts(dropna=False).to_string())

    print("\n=== Unique concept counts by ticker ===")
    print(df.groupby("ticker")["concept"].nunique().sort_values(ascending=False).to_string())

    print("\n=== Form value counts ===")
    form_counts = df["form"].value_counts(dropna=False)
    print(form_counts.to_string())
    remaining_forms = set(form_counts.index.dropna().astype(str))
    if remaining_forms <= ALLOWED_FORMS:
        print("Confirmed: only 10-K and 10-Q remain.")
    else:
        unexpected = remaining_forms - ALLOWED_FORMS
        print(f"Unexpected forms remaining: {sorted(unexpected)}")

    flagged, high_priority, _rows_evaluated = detect_financial_anomalies(df)
    financial_anomaly_flags = len(flagged)
    high_priority_anomaly_flags = len(high_priority)
    flagged.to_csv(FINANCIAL_ANOMALY_PATH, index=False)
    high_priority.to_csv(HIGH_PRIORITY_ANOMALY_PATH, index=False)
    print(f"Saved anomaly flags to {FINANCIAL_ANOMALY_PATH}")
    print(f"Saved high-priority anomalies to {HIGH_PRIORITY_ANOMALY_PATH}")

    missing_critical_fields = sum(missing.values())

    summary = pd.DataFrame(
        [
            {
                "total_rows": total_rows,
                "total_columns": total_columns,
                "unique_tickers": unique_tickers,
                "unique_concepts": unique_concepts,
                "duplicate_fact_key_rows": duplicate_fact_key_rows,
                "filed_before_end_warning_rows": filed_before_end_warning_rows,
                "missing_ticker": missing["ticker"],
                "missing_concept": missing["concept"],
                "missing_unit": missing["unit"],
                "missing_end": missing["end"],
                "missing_val": missing["val"],
                "missing_filed": missing["filed"],
                "financial_anomaly_flags": financial_anomaly_flags,
                "high_priority_anomaly_flags": high_priority_anomaly_flags,
                **format_metrics,
            }
        ]
    )
    summary.to_csv(VALIDATION_SUMMARY_PATH, index=False)
    print(f"\nSaved validation summary to {VALIDATION_SUMMARY_PATH}")

    write_data_quality_report(
        cleaned_rows=total_rows,
        unique_tickers=unique_tickers,
        unique_concepts=unique_concepts,
        duplicate_fact_key_rows=duplicate_fact_key_rows,
        missing_critical_fields=missing_critical_fields,
        filed_before_end_warning_rows=filed_before_end_warning_rows,
        format_metrics=format_metrics,
        financial_anomaly_flags=financial_anomaly_flags,
        high_priority_anomaly_flags=high_priority_anomaly_flags,
    )


if __name__ == "__main__":
    main()
