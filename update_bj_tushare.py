"""
Incrementally update existing BJ (Beijing Exchange) parquet files using Tushare.

Usage:
    set TUSHARE_TOKEN=your_token_here
    python update_bj_tushare.py --data-dir data --end-date 20260528 --sleep-sec 1.3

What it does:
    1. Scans existing BJ parquet files in the data directory.
    2. Fetches Tushare daily bars after each file's latest local date.
    3. Writes back canonical 8-column parquet files only.
    4. Skips files that are already up to date.

Notes:
    - This updater runs sequentially to stay under Tushare's rate limit.
    - Use a small sleep between requests to avoid 50 calls/minute throttling.
"""

from __future__ import annotations

import argparse
import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import pandas as pd
import pyarrow.parquet as pq


BJ_PREFIXES = ("83", "87", "88", "92")
CANONICAL_COLUMNS = ["code", "name", "date", "open", "close", "high", "low", "volume"]
CORE_COLUMNS = ["date", "open", "close", "high", "low", "volume"]


def parse_yyyymmdd(value: Optional[str]) -> date:
    if not value:
        raise ValueError("date value is required")
    return datetime.strptime(value, "%Y%m%d").date()


def format_yyyymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")


def resolve_market_suffix(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(BJ_PREFIXES):
        return "BJ"
    if code.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "SH"
    return "SZ"


def existing_bj_files(data_dir: Path) -> List[Path]:
    files = [path for path in data_dir.glob("*.parquet") if path.is_file() and path.stem.startswith(BJ_PREFIXES)]
    return sorted(files)


def read_last_date(path: Path) -> Optional[date]:
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path, columns=["date"])
    except Exception:
        return None
    if df.empty or "date" not in df.columns:
        return None
    try:
        return pd.to_datetime(df["date"]).max().date()
    except Exception:
        return None


def load_existing_frame(path: Path) -> tuple[pd.DataFrame, List[str]]:
    if not path.exists():
        return pd.DataFrame(), []

    try:
        schema_names = pq.read_schema(path).names
    except Exception:
        schema_names = []

    read_cols = [col for col in CANONICAL_COLUMNS if col in schema_names]
    if not read_cols:
        return pd.DataFrame(), schema_names

    try:
        frame = pd.read_parquet(path, columns=read_cols)
    except Exception:
        return pd.DataFrame(), schema_names

    return normalize_market_frame(frame), schema_names


def normalize_market_frame(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column in CANONICAL_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame = frame[CANONICAL_COLUMNS]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for column in ["open", "close", "high", "low", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date"])
    frame = frame.dropna(subset=CORE_COLUMNS)
    frame = frame.sort_values("date")
    return frame


def standardize_tushare_df(df: pd.DataFrame, code: str, name: str | None = None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    frame = df.copy()
    frame = frame.rename(
        columns={
            "trade_date": "date",
            "open": "open",
            "close": "close",
            "high": "high",
            "low": "low",
            "vol": "volume",
        }
    )
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for column in ["open", "close", "high", "low", "volume"]:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame["code"] = str(code).zfill(6)
    if name is not None:
        frame["name"] = name

    keep_columns = [column for column in CANONICAL_COLUMNS if column in frame.columns]
    frame = frame[keep_columns]
    frame = frame.dropna(subset=["date"])
    frame = frame.dropna(subset=CORE_COLUMNS)
    return frame.sort_values("date")


def inherit_existing_name(path: Path, update_df: pd.DataFrame) -> pd.DataFrame:
    if update_df is None or update_df.empty:
        return update_df

    if not path.exists():
        return update_df

    try:
        existing = pd.read_parquet(path, columns=["name"])
    except Exception:
        return update_df

    if "name" not in existing.columns:
        return update_df

    known_names = existing["name"].dropna()
    if known_names.empty:
        return update_df

    stock_name = known_names.iloc[-1]
    frame = update_df.copy()
    if "name" not in frame.columns:
        frame["name"] = stock_name
    else:
        frame["name"] = frame["name"].fillna(stock_name)
    return frame


def fetch_tushare_bars(ts_code: str, start_date: str, end_date: str, sleep_sec: float = 0.0) -> pd.DataFrame:
    if sleep_sec > 0:
        time.sleep(sleep_sec)

    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TUSHARE_TOKEN is not set")

    try:
        import tushare as ts
    except Exception as exc:
        raise RuntimeError("tushare is not installed in this environment") from exc

    pro = ts.pro_api(token)
    df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def atomic_write_parquet(path: Path, df: pd.DataFrame) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def merge_existing_with_update(old_df: pd.DataFrame, update_df: pd.DataFrame) -> pd.DataFrame:
    frames = [frame for frame in (old_df, update_df) if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True, sort=False)
    combined = normalize_market_frame(combined)
    combined = combined.drop_duplicates(subset=["date"], keep="last")
    return normalize_market_frame(combined)


@dataclass
class UpdateResult:
    code: str
    status: str
    last_date: Optional[date]
    target_end_date: date
    rows_added: int = 0
    message: str = ""


def update_one_file(path: Path, target_end_date: date, dry_run: bool = False, sleep_sec: float = 0.0) -> UpdateResult:
    code = path.stem[:6]
    existing_df, schema_names = load_existing_frame(path)
    if existing_df.empty:
        return UpdateResult(code=code, status="skip", last_date=None, target_end_date=target_end_date, message="no readable date column")

    last_date = pd.to_datetime(existing_df["date"]).max().date()
    schema_is_canonical = list(schema_names) == CANONICAL_COLUMNS
    needs_schema_normalization = not schema_is_canonical

    if last_date >= target_end_date:
        if needs_schema_normalization:
            if dry_run:
                return UpdateResult(code=code, status="dry-run", last_date=last_date, target_end_date=target_end_date, message="would normalize schema only")
            atomic_write_parquet(path, existing_df)
            return UpdateResult(code=code, status="normalized", last_date=last_date, target_end_date=target_end_date, message="schema normalized to canonical columns")
        return UpdateResult(code=code, status="up-to-date", last_date=last_date, target_end_date=target_end_date)

    start_date = last_date + timedelta(days=1)
    ts_code = f"{code}.{resolve_market_suffix(code)}"
    update_df = fetch_tushare_bars(ts_code, format_yyyymmdd(start_date), format_yyyymmdd(target_end_date), sleep_sec=sleep_sec)
    if update_df.empty:
        return UpdateResult(code=code, status="no-new-data", last_date=last_date, target_end_date=target_end_date, message=f"no rows from {format_yyyymmdd(start_date)} to {format_yyyymmdd(target_end_date)}")

    update_df = inherit_existing_name(path, update_df)

    if dry_run:
        return UpdateResult(code=code, status="dry-run", last_date=last_date, target_end_date=target_end_date, rows_added=len(update_df), message=f"would add {len(update_df)} rows")

    combined = merge_existing_with_update(existing_df, update_df)
    if combined.empty:
        return UpdateResult(code=code, status="skip", last_date=last_date, target_end_date=target_end_date, message="combined dataframe empty")

    atomic_write_parquet(path, combined)
    return UpdateResult(code=code, status="updated", last_date=last_date, target_end_date=target_end_date, rows_added=len(update_df), message=f"added {len(update_df)} rows")


def chunked(items: Sequence[Path], size: int) -> Iterable[List[Path]]:
    for index in range(0, len(items), size):
        yield list(items[index : index + size])


def run_update(data_dir: Path, target_end_date: date, batch_size: int, dry_run: bool, limit: Optional[int], sleep_sec: float) -> None:
    files = existing_bj_files(data_dir)
    if limit is not None:
        files = files[:limit]

    print(f"Found {len(files)} BJ parquet files")
    if not files:
        return

    total_updated = 0
    total_rows = 0
    stats = Counter()

    for batch_index, batch in enumerate(chunked(files, batch_size), start=1):
        print(f"Processing batch {batch_index} with {len(batch)} files")
        for path in batch:
            try:
                result = update_one_file(path, target_end_date, dry_run, sleep_sec=sleep_sec)
            except Exception as exc:
                stats["failed"] += 1
                print(f"failed {path.name}: {exc}")
                continue

            stats[result.status] += 1
            if result.status == "updated":
                total_updated += 1
                total_rows += result.rows_added
                print(f"{result.code} updated (+{result.rows_added} rows)")
            elif result.status == "normalized":
                total_updated += 1
                print(f"{result.code} normalized schema")
            elif result.status == "no-new-data":
                print(f"{result.code} no new data")
            elif result.status == "up-to-date":
                print(f"{result.code} up to date")
            elif result.status == "dry-run":
                print(f"{result.code} dry-run: {result.message}")
            else:
                print(f"{result.code} skipped: {result.message}")

    print(f"Done. Updated {total_updated} files, added {total_rows} rows total.")
    print(f"Status summary: {dict(stats)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BJ Tushare incremental updater for existing local parquet files")
    parser.add_argument("--data-dir", default="data", help="Directory containing existing stock parquet files")
    parser.add_argument("--end-date", default=None, help="Inclusive end date in YYYYMMDD; defaults to today - update-delay-days")
    parser.add_argument("--update-delay-days", type=int, default=1, help="Delay before the last complete trading day")
    parser.add_argument("--batch-size", type=int, default=50, help="Number of files processed per batch")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N BJ files")
    parser.add_argument("--dry-run", action="store_true", help="Fetch data and report changes without writing files")
    parser.add_argument("--sleep-sec", type=float, default=1.3, help="Sleep before each Tushare request to avoid rate limits")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        raise FileNotFoundError(f"data dir not found: {data_dir}")

    if args.end_date:
        target_end_date = parse_yyyymmdd(args.end_date)
    else:
        target_end_date = datetime.now().date() - timedelta(days=int(args.update_delay_days))

    run_update(
        data_dir=data_dir,
        target_end_date=target_end_date,
        batch_size=max(1, int(args.batch_size)),
        dry_run=bool(args.dry_run),
        limit=args.limit,
        sleep_sec=max(0.0, float(args.sleep_sec)),
    )


if __name__ == "__main__":
    main()