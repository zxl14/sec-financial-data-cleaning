"""Clean and standardize SEC financial data."""

import json
from pathlib import Path

import pandas as pd

RAW_DIR = Path("data/raw")
PROCESSED_DIR = Path("data/processed")
OUTPUT_DIR = Path("output")
OUTPUT_PATH = PROCESSED_DIR / "companyfacts_flat.csv"
CLEAN_OUTPUT_PATH = PROCESSED_DIR / "companyfacts_clean.csv"
DUPLICATE_DIAGNOSTIC_PATH = OUTPUT_DIR / "duplicate_diagnostic.csv"
VALUE_CONFLICT_PATH = OUTPUT_DIR / "value_conflict_diagnostic.csv"
VALUE_CONFLICT_SUMMARY_PATH = OUTPUT_DIR / "value_conflict_summary.csv"

FACT_KEY = ["ticker", "concept", "unit", "start", "end"]
KEEP_FORMS = {"10-K", "10-Q"}


def flatten_company_file(path: Path) -> list[dict]:
    """Flatten us-gaap facts from one Company Facts JSON file into row dicts."""
    ticker = path.stem.removesuffix("_companyfacts")

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    cik = data.get("cik")
    entity_name = data.get("entityName")
    us_gaap = data.get("facts", {}).get("us-gaap", {})

    rows = []
    for concept, concept_data in us_gaap.items():
        label = concept_data.get("label")
        units = concept_data.get("units", {})
        for unit, observations in units.items():
            for obs in observations:
                rows.append(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "entity_name": entity_name,
                        "concept": concept,
                        "label": label,
                        "unit": unit,
                        "start": obs.get("start"),
                        "end": obs.get("end"),
                        "val": obs.get("val"),
                        "accn": obs.get("accn"),
                        "fy": obs.get("fy"),
                        "fp": obs.get("fp"),
                        "form": obs.get("form"),
                        "filed": obs.get("filed"),
                        "frame": obs.get("frame"),
                    }
                )
    return rows


def print_diagnostics(df: pd.DataFrame) -> None:
    """Print basic data quality diagnostics for the flattened DataFrame."""
    print("\n=== Basic summary ===")
    print(f"Total rows: {len(df)}")
    print(f"Total columns: {len(df.columns)}")
    print(f"Unique tickers: {df['ticker'].nunique()}")
    print(f"Unique concepts: {df['concept'].nunique()}")
    print(f"Exact duplicate rows: {df.duplicated().sum()}")

    print("\n=== Missing values (highest to lowest) ===")
    missing = df.isna().sum().sort_values(ascending=False)
    print(missing.to_string())

    print("\n=== Value counts: form ===")
    print(df["form"].value_counts(dropna=False).to_string())

    print("\n=== Value counts: unit ===")
    print(df["unit"].value_counts(dropna=False).to_string())

    print("\n=== Value counts: ticker ===")
    print(df["ticker"].value_counts(dropna=False).to_string())

    print("\n=== Top 20 financial concepts ===")
    print(df["concept"].value_counts().head(20).to_string())

    dup_keys = ["ticker", "concept", "unit", "start", "end", "val", "form", "filed"]
    key_dupes = df.duplicated(subset=dup_keys).sum()
    print("\n=== Key-based duplicate diagnostic ===")
    print(
        "Duplicate rows on "
        f"{' + '.join(dup_keys)}: {key_dupes}"
    )

    dup_groups = df[df.duplicated(subset=dup_keys, keep=False)].copy()
    dup_groups = dup_groups.sort_values(
        by=["ticker", "concept", "end", "filed"]
    ).reset_index(drop=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dup_groups.to_csv(DUPLICATE_DIAGNOSTIC_PATH, index=False)
    print(
        f"Saved {len(dup_groups)} rows from duplicate groups to "
        f"{DUPLICATE_DIAGNOSTIC_PATH}"
    )

    grouped = dup_groups.groupby(dup_keys, dropna=False)
    n_groups = grouped.ngroups
    print(f"\nNumber of duplicate groups: {n_groups}")

    print("\nGroups with more than one distinct non-null value:")
    for col in ["accn", "fy", "fp", "frame"]:
        n_varying = grouped[col].nunique(dropna=True).gt(1).sum()
        print(f"  {col}: {n_varying}")

    display_cols = [
        "ticker",
        "concept",
        "start",
        "end",
        "val",
        "form",
        "filed",
        "accn",
        "fy",
        "fp",
        "frame",
    ]
    first_10_keys = dup_groups[dup_keys].drop_duplicates().head(10)
    first_10_groups = dup_groups.merge(first_10_keys, on=dup_keys, how="inner")
    print("\n=== First 10 duplicate groups ===")
    print(first_10_groups[display_cols].to_string(index=False))

    print("\n=== Missing values (again, highest to lowest) ===")
    print(df.isna().sum().sort_values(ascending=False).to_string())

    print("\n=== Value counts: form (again) ===")
    print(df["form"].value_counts(dropna=False).to_string())

    print("\n=== Value counts: unit (again) ===")
    print(df["unit"].value_counts(dropna=False).to_string())


def clean_companyfacts(df: pd.DataFrame) -> pd.DataFrame:
    """Filter to 10-K/10-Q and keep latest filed row per fact key."""
    rows_before_form = len(df)
    print("\n=== Cleaning stage ===")
    print(f"Rows before form filtering: {rows_before_form}")

    cleaned = df[df["form"].isin(KEEP_FORMS)].copy()
    rows_after_form = len(cleaned)
    print(f"Rows after keeping only 10-K and 10-Q: {rows_after_form}")

    rows_before_dedup = len(cleaned)
    print(f"Rows before canonical deduplication: {rows_before_dedup}")

    grouped = cleaned.groupby(FACT_KEY, dropna=False)
    n_distinct_vals = grouped["val"].nunique(dropna=True)
    n_groups = len(n_distinct_vals)
    n_single_val = int((n_distinct_vals == 1).sum())
    n_conflict = int((n_distinct_vals > 1).sum())
    conflict_pct = (n_conflict / n_groups * 100) if n_groups else 0.0

    print("\n=== Value conflict diagnostic (before canonical dedup) ===")
    print(f"Total candidate fact groups: {n_groups}")
    print(f"Groups with only one distinct val: {n_single_val}")
    print(f"Groups with more than one distinct val: {n_conflict}")
    print(f"Percentage of groups with conflicting values: {conflict_pct:.2f}%")

    has_conflict = grouped["val"].transform(lambda s: s.nunique(dropna=True) > 1)
    conflicts = cleaned.loc[has_conflict].sort_values(
        by=["ticker", "concept", "start", "end", "filed"]
    ).reset_index(drop=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conflicts.to_csv(VALUE_CONFLICT_PATH, index=False)
    print(f"Saved {len(conflicts)} conflicting-group rows to {VALUE_CONFLICT_PATH}")

    conflict_groups = conflicts.groupby(FACT_KEY, dropna=False)

    def _val_at_filed(group: pd.DataFrame, filed_value) -> object:
        matched = group.loc[group["filed"] == filed_value, "val"]
        return matched.iloc[0] if len(matched) else None

    summary_rows = []
    for key, group in conflict_groups:
        earliest_filed = group["filed"].min()
        latest_filed = group["filed"].max()
        summary_rows.append(
            {
                "ticker": key[0],
                "concept": key[1],
                "unit": key[2],
                "start": key[3],
                "end": key[4],
                "n_distinct_val": group["val"].nunique(dropna=True),
                "n_distinct_accn": group["accn"].nunique(dropna=True),
                "n_distinct_filed": group["filed"].nunique(dropna=True),
                "earliest_filed": earliest_filed,
                "latest_filed": latest_filed,
                "earliest_reported_val": _val_at_filed(group, earliest_filed),
                "latest_reported_val": _val_at_filed(group, latest_filed),
            }
        )

    conflict_summary = pd.DataFrame(summary_rows)
    conflict_summary = conflict_summary.sort_values(
        by=["ticker", "concept", "start", "end"]
    ).reset_index(drop=True)
    conflict_summary.to_csv(VALUE_CONFLICT_SUMMARY_PATH, index=False)

    n_multi_accn = int((conflict_summary["n_distinct_accn"] > 1).sum())
    n_multi_filed = int((conflict_summary["n_distinct_filed"] > 1).sum())
    n_same_accn_multi_val = int(
        (
            (conflict_summary["n_distinct_accn"] == 1)
            & (conflict_summary["n_distinct_val"] > 1)
        ).sum()
    )

    print("\n=== Value conflict summary ===")
    print(f"Total conflicting groups: {len(conflict_summary)}")
    print(f"Conflicting groups with multiple accession numbers: {n_multi_accn}")
    print(f"Conflicting groups with multiple filed dates: {n_multi_filed}")
    print(
        "Conflicting groups with same accession number but multiple values: "
        f"{n_same_accn_multi_val}"
    )
    print("\nConflict counts by ticker:")
    print(conflict_summary["ticker"].value_counts().to_string())
    print("\nTop 20 concepts with the most value conflicts:")
    print(conflict_summary["concept"].value_counts().head(20).to_string())
    print(f"Saved conflict summary to {VALUE_CONFLICT_SUMMARY_PATH}")

    cleaned = cleaned.sort_values(by="filed", ascending=True)
    cleaned = cleaned.drop_duplicates(subset=FACT_KEY, keep="last")
    cleaned = cleaned.reset_index(drop=True)

    rows_after_dedup = len(cleaned)
    rows_removed = rows_before_dedup - rows_after_dedup
    print(f"\nRows after canonical deduplication: {rows_after_dedup}")
    print(f"Rows removed by canonical deduplication: {rows_removed}")

    return cleaned


def main() -> None:
    raw_files = sorted(RAW_DIR.glob("*_companyfacts.json"))
    if not raw_files:
        raise FileNotFoundError(f"No companyfacts JSON files found in {RAW_DIR}")

    all_rows = []
    for path in raw_files:
        print(f"Flattening {path.name}...")
        all_rows.extend(flatten_company_file(path))

    df = pd.DataFrame(all_rows)
    print_diagnostics(df)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False)
    print(f"\nSaved {len(df)} rows to {OUTPUT_PATH}")

    cleaned = clean_companyfacts(df)
    cleaned.to_csv(CLEAN_OUTPUT_PATH, index=False)
    print(f"Saved {len(cleaned)} rows to {CLEAN_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
