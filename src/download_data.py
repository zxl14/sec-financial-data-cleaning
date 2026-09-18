"""Download SEC financial data."""

import argparse
import json
import statistics
import time
from pathlib import Path

import pandas as pd
import requests

TICKERS = [
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "META",
    "ORCL",
    "JPM",
    "GS",
    "BAC",
    "MS",
    "C",
    "BLK",
    "XOM",
    "CVX",
    "COP",
    "WMT",
    "COST",
    "HD",
    "MCD",
    "NKE",
    "JNJ",
    "PFE",
    "MRK",
    "UNH",
    "CAT",
    "GE",
    "HON",
    "UPS",
    "VZ",
    "DIS",
]

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
HEADERS = {
    "User-Agent": "sec-financial-data-cleaning research@example.com",
}
RAW_DIR = Path("data/raw")
QUARANTINE_DIR = Path("data/quarantine")
OUTPUT_DIR = Path("output")
CONFIG_DIR = Path("config")
VERIFIED_ENTITIES_PATH = CONFIG_DIR / "verified_entities.csv"
ANOMALY_PATH = OUTPUT_DIR / "entity_resolution_anomalies.csv"
REQUEST_DELAY_SECONDS = 0.25

# Cross-sectional sparsity thresholds relative to the universe median.
OBS_RATIO = 0.10
CONCEPT_RATIO = 0.25
SIZE_RATIO = 0.10
FILING_RATIO = 0.10
# Flag when at least this many sparsity signals fire.
MIN_SPARSE_SIGNALS = 2


def load_verified_entities() -> dict[str, str]:
    """Load manually verified ticker -> CIK mappings from config."""
    if not VERIFIED_ENTITIES_PATH.exists():
        return {}

    df = pd.read_csv(VERIFIED_ENTITIES_PATH, dtype={"ticker": str, "cik": str})
    verified = {}
    for _, row in df.iterrows():
        ticker = str(row["ticker"]).strip().upper()
        cik = str(row["cik"]).strip().zfill(10)
        verified[ticker] = cik
    return verified


def load_ticker_to_cik() -> dict[str, str]:
    """Download SEC ticker mapping and return ticker -> 10-digit CIK."""
    response = requests.get(TICKER_MAP_URL, headers=HEADERS)
    response.raise_for_status()
    mapping_data = response.json()

    ticker_to_cik = {}
    for entry in mapping_data.values():
        ticker = str(entry["ticker"]).upper()
        cik = str(entry["cik_str"]).zfill(10)
        ticker_to_cik[ticker] = cik
    return ticker_to_cik


def resolve_cik(
    ticker: str,
    verified_entities: dict[str, str],
    sec_ticker_to_cik: dict[str, str],
) -> tuple[str | None, bool]:
    """
    Resolve CIK with priority:
      1. verified_entities.csv
      2. SEC company_tickers.json
    Returns (cik_or_none, used_verified_mapping).
    """
    if ticker in verified_entities:
        return verified_entities[ticker], True
    return sec_ticker_to_cik.get(ticker), False


def inspect_company_facts(ticker: str, cik: str, content: str) -> dict:
    """Compute source-entity quality metrics from a Company Facts JSON body."""
    data = json.loads(content)
    us_gaap = data.get("facts", {}).get("us-gaap", {})

    n_concepts = 0
    n_obs = 0
    ten_k = 0
    ten_q = 0
    filed_dates: list[str] = []

    for concept_data in us_gaap.values():
        n_concepts += 1
        for observations in concept_data.get("units", {}).values():
            for obs in observations:
                n_obs += 1
                form = obs.get("form")
                if form == "10-K":
                    ten_k += 1
                elif form == "10-Q":
                    ten_q += 1
                filed = obs.get("filed")
                if filed:
                    filed_dates.append(filed)

    return {
        "ticker": ticker,
        "resolved_cik": cik,
        "entity_name": data.get("entityName"),
        "file_size_bytes": len(content.encode("utf-8")),
        "us_gaap_concepts": n_concepts,
        "us_gaap_observations": n_obs,
        "ten_k_observations": ten_k,
        "ten_q_observations": ten_q,
        "earliest_filed": min(filed_dates) if filed_dates else None,
        "latest_filed": max(filed_dates) if filed_dates else None,
        "content": content,
    }


def load_peer_metrics_from_raw(exclude_tickers: set[str]) -> list[dict]:
    """Build QC peer metrics from existing accepted raw files."""
    peers = []
    for path in sorted(RAW_DIR.glob("*_companyfacts.json")):
        ticker = path.stem.removesuffix("_companyfacts")
        if ticker in exclude_tickers:
            continue
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        cik = str(data.get("cik", "")).zfill(10)
        metrics = inspect_company_facts(ticker, cik, content)
        # Drop bulky content for peer-only rows.
        metrics = {k: v for k, v in metrics.items() if k != "content"}
        peers.append(metrics)
    return peers


def detect_sparse_anomalies(
    candidates: list[dict],
    peer_universe: list[dict] | None = None,
) -> dict[str, str]:
    """
    Flag candidates whose Company Facts history is unusually sparse
    relative to the peer universe median.

    A candidate is suspicious when at least MIN_SPARSE_SIGNALS of these
    are true versus the cross-sectional median:
      - us-gaap observations < OBS_RATIO * median
      - us-gaap concepts < CONCEPT_RATIO * median
      - raw file size < SIZE_RATIO * median
      - (10-K + 10-Q) observations < FILING_RATIO * median
    """
    peers = peer_universe if peer_universe is not None else candidates
    if len(peers) < 2:
        return {}

    median_obs = statistics.median(c["us_gaap_observations"] for c in peers)
    median_concepts = statistics.median(c["us_gaap_concepts"] for c in peers)
    median_size = statistics.median(c["file_size_bytes"] for c in peers)
    median_filings = statistics.median(
        c["ten_k_observations"] + c["ten_q_observations"] for c in peers
    )

    print("\n=== Cross-sectional medians ===")
    print(f"Median us-gaap observations: {median_obs}")
    print(f"Median us-gaap concepts: {median_concepts}")
    print(f"Median file size (bytes): {median_size}")
    print(f"Median 10-K+10-Q observations: {median_filings}")

    anomalies: dict[str, str] = {}
    for candidate in candidates:
        signals = []
        obs = candidate["us_gaap_observations"]
        concepts = candidate["us_gaap_concepts"]
        size = candidate["file_size_bytes"]
        filings = candidate["ten_k_observations"] + candidate["ten_q_observations"]

        if median_obs > 0 and obs < OBS_RATIO * median_obs:
            signals.append(
                f"us_gaap_observations={obs} < {OBS_RATIO:.0%} of median ({median_obs})"
            )
        if median_concepts > 0 and concepts < CONCEPT_RATIO * median_concepts:
            signals.append(
                f"us_gaap_concepts={concepts} < {CONCEPT_RATIO:.0%} of median ({median_concepts})"
            )
        if median_size > 0 and size < SIZE_RATIO * median_size:
            signals.append(
                f"file_size_bytes={size} < {SIZE_RATIO:.0%} of median ({median_size})"
            )
        if median_filings > 0 and filings < FILING_RATIO * median_filings:
            signals.append(
                f"ten_k_plus_ten_q={filings} < {FILING_RATIO:.0%} of median ({median_filings})"
            )

        if len(signals) >= MIN_SPARSE_SIGNALS:
            anomalies[candidate["ticker"]] = (
                "Unusually sparse Company Facts history vs peer universe: "
                + "; ".join(signals)
            )

    return anomalies


def main(tickers: list[str] | None = None) -> None:
    selected_tickers = tickers if tickers is not None else TICKERS

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading verified entity mappings...")
    verified_entities = load_verified_entities()
    print(f"Verified mappings loaded: {len(verified_entities)}")

    print("Downloading SEC ticker mapping...")
    sec_ticker_to_cik = load_ticker_to_cik()

    candidates: list[dict] = []
    failed: list[str] = []
    total = len(selected_tickers)

    for i, ticker in enumerate(selected_tickers, start=1):
        print(f"[{i}/{total}] Downloading {ticker}...")

        cik, used_verified = resolve_cik(ticker, verified_entities, sec_ticker_to_cik)
        if cik is None:
            print(f"Warning: ticker {ticker} not found in SEC mapping; skipping.")
            failed.append(ticker)
            continue

        if used_verified:
            print(f"Using verified entity mapping for {ticker}: {cik}")

        try:
            url = COMPANY_FACTS_URL.format(cik=cik)
            response = requests.get(url, headers=HEADERS)
            response.raise_for_status()
            metrics = inspect_company_facts(ticker, cik, response.text)
            candidates.append(metrics)
            print(
                f"Inspected {ticker}: "
                f"{metrics['us_gaap_concepts']} concepts, "
                f"{metrics['us_gaap_observations']} observations"
            )
        except (requests.RequestException, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(f"Warning: failed to download/inspect {ticker} (CIK {cik}): {exc}")
            failed.append(ticker)

        time.sleep(REQUEST_DELAY_SECONDS)

    # Use current downloads plus existing accepted raw peers for cross-sectional QC.
    existing_peers = load_peer_metrics_from_raw({c["ticker"] for c in candidates})
    peer_universe = [{k: v for k, v in c.items() if k != "content"} for c in candidates]
    peer_universe.extend(existing_peers)
    anomaly_reasons = detect_sparse_anomalies(candidates, peer_universe=peer_universe)

    accepted: list[str] = []
    quarantined: list[str] = []
    anomaly_rows: list[dict] = []

    for candidate in candidates:
        ticker = candidate["ticker"]
        content = candidate["content"]
        reason = anomaly_reasons.get(ticker)

        if reason:
            # Quarantine only; do not overwrite an existing raw file.
            quarantine_path = QUARANTINE_DIR / f"{ticker}_companyfacts.json"
            quarantine_path.write_text(content, encoding="utf-8")
            quarantined.append(ticker)
            anomaly_rows.append(
                {
                    "ticker": ticker,
                    "resolved_cik": candidate["resolved_cik"],
                    "entity_name": candidate["entity_name"],
                    "file_size_bytes": candidate["file_size_bytes"],
                    "us_gaap_concepts": candidate["us_gaap_concepts"],
                    "us_gaap_observations": candidate["us_gaap_observations"],
                    "ten_k_observations": candidate["ten_k_observations"],
                    "ten_q_observations": candidate["ten_q_observations"],
                    "earliest_filed": candidate["earliest_filed"],
                    "latest_filed": candidate["latest_filed"],
                    "anomaly_reason": reason,
                }
            )
            print(f"Quarantined {ticker}: {reason}")
        else:
            raw_path = RAW_DIR / f"{ticker}_companyfacts.json"
            raw_path.write_text(content, encoding="utf-8")
            accepted.append(ticker)
            print(f"Accepted {ticker}")

    anomaly_columns = [
        "ticker",
        "resolved_cik",
        "entity_name",
        "file_size_bytes",
        "us_gaap_concepts",
        "us_gaap_observations",
        "ten_k_observations",
        "ten_q_observations",
        "earliest_filed",
        "latest_filed",
        "anomaly_reason",
    ]
    pd.DataFrame(anomaly_rows, columns=anomaly_columns).to_csv(ANOMALY_PATH, index=False)
    print(f"\nSaved anomaly report to {ANOMALY_PATH}")

    print("\n=== Download summary ===")
    print(f"Requested tickers: {total}")
    print(f"Accepted tickers: {len(accepted)} -> {accepted}")
    print(f"Quarantined tickers: {len(quarantined)} -> {quarantined}")
    print(f"Failed tickers: {failed if failed else []}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download SEC Company Facts data.")
    parser.add_argument(
        "--tickers",
        nargs="+",
        help="Optional subset of tickers to download (default: full universe).",
    )
    args = parser.parse_args()
    main(tickers=args.tickers)
