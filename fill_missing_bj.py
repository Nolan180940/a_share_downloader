"""
Fill missing BJ (Beijing Exchange) A-share parquet files using Tushare.

Usage:
  set TUSHARE_TOKEN=your_token_here
  python fill_missing_bj.py --data-dir data --start-date 20150101

What it does:
  1. Reads all A-share codes from AkShare.
  2. Compares them with existing parquet files in data-dir.
  3. Keeps only missing BJ-prefixed codes (83/87/88/92).
  4. Downloads daily bars from Tushare and writes one parquet per code.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from datetime import datetime

import pandas as pd
import yaml
import akshare as ak


BJ_PREFIXES = ("83", "87", "88", "92")
CANONICAL_COLUMNS = ["code", "name", "date", "open", "close", "high", "low", "volume"]
CORE_COLUMNS = ["date", "open", "close", "high", "low", "volume"]


def load_config(path: str) -> dict:
    if not Path(path).exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_market_suffix(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "SH"
    if code.startswith(("000", "001", "002", "003", "200", "300", "301")):
        return "SZ"
    if code.startswith(BJ_PREFIXES):
        return "BJ"
    return "SZ"


def existing_codes(data_dir: Path) -> set[str]:
    return {p.stem[:6] for p in data_dir.glob("*.parquet") if p.stem[:6].isdigit()}


def all_a_share_codes() -> list[str]:
    code_df = ak.stock_info_a_code_name()
    return [str(x).zfill(6) for x in code_df["code"].tolist()]


def missing_bj_codes(data_dir: Path) -> list[str]:
    existing = existing_codes(data_dir)
    all_codes = all_a_share_codes()
    missing = [c for c in all_codes if c not in existing]
    return [c for c in missing if c.startswith(BJ_PREFIXES)]


def standardize_tushare_df(df: pd.DataFrame, code: str, name: str | None = None) -> pd.DataFrame:
    rename_map = {
        "trade_date": "date",
        "open": "open",
        "close": "close",
        "high": "high",
        "low": "low",
        "vol": "volume",
        "amount": "amount",
    }
    df = df.rename(columns=rename_map)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    for column in ["open", "close", "high", "low", "volume"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df["code"] = code
    if name:
        df["name"] = name
    keep = [c for c in CANONICAL_COLUMNS if c in df.columns]
    df = df[keep]
    if "date" in df.columns:
        df = df.dropna(subset=["date"])
    if set(CORE_COLUMNS).issubset(df.columns):
        df = df.dropna(subset=CORE_COLUMNS)
    return df.sort_values("date")


def fetch_tushare_bars(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
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


def write_parquet_atomic(path: Path, df: pd.DataFrame) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill missing BJ parquet files using Tushare")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    data_dir = Path(args.data_dir or cfg.get("data_dir", "data"))
    start_date = args.start_date or cfg.get("start_date", "20150101")
    end_date = args.end_date or datetime.now().strftime("%Y%m%d")

    data_dir.mkdir(parents=True, exist_ok=True)

    missing = missing_bj_codes(data_dir)
    print(f"missing_bj_count={len(missing)}")
    print(f"missing_bj_sample={missing[:30]}")

    if args.dry_run:
        return

    code_df = ak.stock_info_a_code_name()
    name_map = {str(row["code"]).zfill(6): row["name"] for _, row in code_df.iterrows()}

    done = 0
    skipped = 0
    failed = 0

    for code in missing:
        ts_code = f"{code}.{resolve_market_suffix(code)}"
        name = name_map.get(code)
        out_path = data_dir / f"{code}.parquet"
        try:
            df = fetch_tushare_bars(ts_code, start_date, end_date)
            if df.empty:
                print(f"skip {code} {ts_code}: no data")
                skipped += 1
                continue
            sdf = standardize_tushare_df(df, code, name=name)
            if sdf.empty:
                print(f"skip {code} {ts_code}: empty after normalization")
                skipped += 1
                continue
            write_parquet_atomic(out_path, sdf)
            print(f"saved {code} rows={len(sdf)}")
            done += 1
        except Exception as exc:
            print(f"failed {code} {ts_code}: {exc}")
            failed += 1

    print(f"summary done={done} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
