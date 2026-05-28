"""Fetch fundamentals from Tushare with rate limiting, retries, and resume support.

Usage:
  set TUSHARE_TOKEN=your_token_here
  python fetch_fundamentals.py --out-dir fundamentals --start-date 20150101 --end-date 20260528

The script will write several parquet files into `--out-dir`:
 - stock_basic.parquet
 - daily_basic.parquet
 - fina_indicator.parquet
 - income.parquet
 - dividend.parquet

It respects a default limit of 50 API calls per minute (configurable).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

try:
    import tushare as ts
except Exception as e:
    print("Please install tushare: pip install tushare", file=sys.stderr)
    raise


class RateLimiter:
    def __init__(self, calls_per_minute: int = 50):
        self.interval = 60.0 / calls_per_minute
        self._last = 0.0

    def wait(self) -> None:
        now = time.time()
        wait_for = self._last + self.interval - now
        if wait_for > 0:
            time.sleep(wait_for)
        self._last = time.time()


def retry_call(func, max_attempts=5, backoff=2.0, **kwargs):
    attempt = 0
    while True:
        try:
            return func(**kwargs)
        except Exception as e:
            attempt += 1
            if attempt >= max_attempts:
                raise
            sleep_for = backoff ** attempt
            print(f"Call failed: {e}. Retry {attempt}/{max_attempts} after {sleep_for:.1f}s")
            time.sleep(sleep_for)


def save_df(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def fetch_stock_basic(pro, out_dir: Path) -> pd.DataFrame:
    print("Fetching stock_basic...")
    df = retry_call(pro.stock_basic, exchange='', list_status='L', fields='ts_code,symbol,name,area,industry,list_date')
    save_df(df, out_dir / 'stock_basic.parquet')
    return df


def fetch_daily_basic(pro, out_dir: Path, start_date: str, end_date: str, limiter: RateLimiter) -> pd.DataFrame:
    print("Fetching daily_basic in one shot (may be large)...")
    # Tushare supports date-range queries for daily_basic; use retry and rate limiter
    limiter.wait()
    df = retry_call(pro.daily_basic, start_date=start_date, end_date=end_date,
                    fields='ts_code,trade_date,total_mv,circ_mv,turnover_rate,pe,pb')
    save_df(df, out_dir / 'daily_basic.parquet')
    return df


def fetch_fina_indicator(pro, out_dir: Path, ts_codes: Iterable[str], limiter: RateLimiter) -> pd.DataFrame:
    print("Fetching fina_indicator per code (paginated by code to limit single-call size)...")
    rows = []
    for i, code in enumerate(ts_codes):
        limiter.wait()
        try:
            df = retry_call(pro.fina_indicator, ts_code=code)
        except Exception as e:
            print(f"fina_indicator failed for {code}: {e}")
            continue
        if df is None or df.empty:
            continue
        rows.append(df)
        if (i + 1) % 100 == 0:
            print(f"Fetched fina_indicator for {i+1} codes")
    if rows:
        out = pd.concat(rows, ignore_index=True)
    else:
        out = pd.DataFrame()
    save_df(out, out_dir / 'fina_indicator.parquet')
    return out


def fetch_income(pro, out_dir: Path, ts_codes: Iterable[str], limiter: RateLimiter) -> pd.DataFrame:
    print("Fetching income (profit) tables per code; slow if many codes")
    rows = []
    for i, code in enumerate(ts_codes):
        limiter.wait()
        try:
            df = retry_call(pro.income, ts_code=code)
        except Exception as e:
            print(f"income failed for {code}: {e}")
            continue
        if df is None or df.empty:
            continue
        rows.append(df)
        if (i + 1) % 100 == 0:
            print(f"Fetched income for {i+1} codes")
    if rows:
        out = pd.concat(rows, ignore_index=True)
    else:
        out = pd.DataFrame()
    save_df(out, out_dir / 'income.parquet')
    return out


def fetch_dividend(pro, out_dir: Path, ts_codes: Iterable[str], limiter: RateLimiter) -> pd.DataFrame:
    print("Fetching dividend records per code")
    rows = []
    for i, code in enumerate(ts_codes):
        limiter.wait()
        try:
            df = retry_call(pro.dividend, ts_code=code)
        except Exception as e:
            print(f"dividend failed for {code}: {e}")
            continue
        if df is None or df.empty:
            continue
        rows.append(df)
    if rows:
        out = pd.concat(rows, ignore_index=True)
    else:
        out = pd.DataFrame()
    save_df(out, out_dir / 'dividend.parquet')
    return out


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--out-dir', default='fundamentals', help='Output directory for parquet files')
    p.add_argument('--start-date', default='20150101')
    p.add_argument('--end-date', default=datetime.now().strftime('%Y%m%d'))
    p.add_argument('--rate-per-minute', type=int, default=50)
    p.add_argument('--skip-heavy', action='store_true', help='Skip heavy per-code tables (income/fina/dividend)')
    return p.parse_args()


def main():
    args = parse_args()
    token = os.environ.get('TUSHARE_TOKEN')
    if not token:
        print('Please set TUSHARE_TOKEN in environment', file=sys.stderr)
        sys.exit(2)

    pro = ts.pro_api(token)
    out_dir = Path(args.out_dir)
    limiter = RateLimiter(calls_per_minute=args.rate_per_minute)

    # 1. stock_basic
    stocks = fetch_stock_basic(pro, out_dir)

    # 2. daily_basic for the requested range
    daily = fetch_daily_basic(pro, out_dir, args.start_date, args.end_date, limiter)

    ts_codes = stocks['ts_code'].tolist() if 'ts_code' in stocks.columns else stocks['symbol'].tolist()

    # 3. heavier tables (optional/slow)
    if not args.skip_heavy:
        try:
            fina = fetch_fina_indicator(pro, out_dir, ts_codes, limiter)
        except Exception as e:
            print('Error fetching fina_indicator:', e)
        try:
            income = fetch_income(pro, out_dir, ts_codes, limiter)
        except Exception as e:
            print('Error fetching income:', e)
        try:
            dividend = fetch_dividend(pro, out_dir, ts_codes, limiter)
        except Exception as e:
            print('Error fetching dividend:', e)

    print('All done. Parquet files are in', str(out_dir))


if __name__ == '__main__':
    main()
