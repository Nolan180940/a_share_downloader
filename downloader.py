"""
A-share historical downloader using AkShare.

Usage:
  python downloader.py --mode full   # full historical download since 2015-01-01
  python downloader.py --mode update # incremental update (append newest trading days)

Configuration in `config.yaml`.
"""
import os
import time
import argparse
from datetime import datetime, timedelta
import logging

import pandas as pd
import akshare as ak
import yaml
import pandas as pd

LOG = logging.getLogger("a_share_downloader")


def setup_logging(level=logging.INFO):
    ch = logging.StreamHandler()
    ch.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    ch.setFormatter(fmt)
    LOG.addHandler(ch)
    LOG.setLevel(level)


def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        try:
            cfg = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise RuntimeError(f"Invalid YAML in {path}: {e}") from e
    return cfg


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def resolve_market_suffix(code):
    code = str(code).zfill(6)
    if code.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "SH"
    if code.startswith(("000", "001", "002", "003", "200", "300", "301")):
        return "SZ"
    if code.startswith(("83", "87", "88", "92")):
        return "BJ"
    return "SZ"


def resolve_tushare_code(code):
    code = str(code).zfill(6)
    suffix = resolve_market_suffix(code)
    return f"{code}.{suffix}"


def read_last_date(path):
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path, columns=["date"])
        if df.empty:
            return None
        return pd.to_datetime(df["date"]).max().date()
    except Exception:
        return None


def standardize_df(df, code, name=None):
    # Normalize column names from multiple sources (AkShare Chinese, yfinance English,
    # or flattened multiindex like 'Close_000001.SZ').
    orig_cols = list(df.columns)

    col_map = {}
    for c in orig_cols:
        lc = str(c).lower()
        # chinese names
        if '日期' in str(c) or lc == 'date' or 'index' == lc:
            col_map[c] = 'date'
            continue
        if '开盘' in str(c) or 'open' in lc:
            col_map[c] = 'open'
            continue
        if '收盘' in str(c) or 'close' in lc:
            col_map[c] = 'close'
            continue
        if '最高' in str(c) or 'high' in lc:
            col_map[c] = 'high'
            continue
        if '最低' in str(c) or 'low' in lc:
            col_map[c] = 'low'
            continue
        if '成交量' in str(c) or 'volume' in lc:
            col_map[c] = 'volume'
            continue
        if '成交额' in str(c) or 'amount' in lc or 'turnover' in lc:
            col_map[c] = 'amount'
            continue
        if '涨跌幅' in str(c) or 'pct' in lc or 'percent' in lc:
            col_map[c] = 'pct_change'
            continue
        if '涨跌额' in str(c) or (('change' in lc) and ('pct' not in lc)):
            col_map[c] = 'change'
            continue

        # flattened multiindex like 'Close_000001.SZ' -> detect by prefix
        parts = str(c).split('_')
        if parts:
            p0 = parts[0].lower()
            if p0 in ('open', 'close', 'high', 'low', 'volume', 'adj close', 'adj_close'):
                map_to = p0.replace('adj close', 'close').replace('adj_close', 'close')
                col_map[c] = 'close' if map_to == 'close' and 'adj' in p0 else map_to

    if col_map:
        df = df.rename(columns=col_map)

    # ensure date column
    if 'date' in df.columns:
        try:
            df['date'] = pd.to_datetime(df['date']).dt.date
        except Exception:
            pass

    # add code/name
    df['code'] = code
    if name is not None:
        df['name'] = name

    if 'date' in df.columns:
        df = df.sort_values('date')

    # choose columns to keep and order
    desired = ['code', 'name', 'date', 'open', 'close', 'high', 'low', 'volume', 'amount', 'pct_change', 'change', 'turnover']
    cols = [c for c in desired if c in df.columns]
    # always include code and name
    # ensure final_cols exist in df
    candidate = ['code', 'name'] + [c for c in cols if c not in ('code', 'name')]
    final_cols = [c for c in candidate if c in df.columns]
    return df[final_cols]
    # ensure date column
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    # add code/name
    df["code"] = code
    if name is not None:
        df["name"] = name
    # keep relevant columns and order
    cols = [c for c in ["code", "name", "date", "open", "close", "high", "low", "volume", "amount", "pct_change", "change", "turnover"] if c in df.columns]
    return df[cols]


def fetch_stock_history(code, start_date, end_date, adjust=""):
    # returns DataFrame or None
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=end_date, adjust=adjust)
        return df
    except Exception as e:
        LOG.debug("fetch failed %s %s", code, e)
        return None


def fetch_with_tushare(code, start_date, end_date, adjust=""):
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        LOG.debug("TUSHARE_TOKEN not set")
        return None
    try:
        import tushare as ts
    except Exception:
        LOG.debug("tushare not available")
        return None

    try:
        pro = ts.pro_api(token)
        ts_code = resolve_tushare_code(code)
        df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    except Exception as e:
        LOG.debug("tushare fetch failed %s %s", code, e)
        return None

    if df is None or df.empty:
        return None

    rename_map = {
        "trade_date": "date",
        "open": "open",
        "close": "close",
        "high": "high",
        "low": "low",
        "vol": "volume",
    }
    df = df.rename(columns=rename_map)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["code"] = str(code).zfill(6)
    return df


def fetch_with_akshare(code, start_date, end_date, adjust=""):
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=end_date, adjust=adjust)
        return df
    except Exception as e:
        LOG.debug("akshare fetch failed %s %s", code, e)
        return None


def fetch_best_available(code, start_date, end_date, adjust="", source_priority=None):
    source_priority = source_priority or ["yahoo", "tushare", "akshare"]
    fetchers = {
        "yahoo": fetch_with_yahoo,
        "tushare": fetch_with_tushare,
        "akshare": fetch_with_akshare,
    }

    last_error = None
    for source in source_priority:
        fetcher = fetchers.get(source)
        if fetcher is None:
            continue
        try:
            df = fetcher(code, start_date, end_date, adjust=adjust)
        except TypeError:
            df = fetcher(code, start_date, end_date)
        except Exception as e:
            last_error = e
            df = None
        if df is not None and not df.empty:
            LOG.debug("Fetched %s using %s", code, source)
            return df, source
    if last_error:
        LOG.debug("all sources failed %s %s", code, last_error)
    return None, None


def atomic_write(path, df, fmt="parquet"):
    tmp = path + ".tmp"
    if fmt == "csv":
        df.to_csv(tmp, index=False, encoding="utf-8")
    else:
        df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def fetch_with_yahoo(code, start_date, end_date):
    try:
        import yfinance as yf
    except Exception:
        LOG.debug("yfinance not available")
        return None

    market = resolve_market_suffix(code)
    if market == "BJ":
        LOG.debug("yahoo does not support BJ code %s", code)
        return None
    suffix = 'SS' if market == 'SH' else 'SZ'
    ticker = f"{code}.{suffix}"
    start_s = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
    end_s = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
    try:
        df = yf.download(ticker, start=start_s, end=end_s, progress=False)
    except Exception as e:
        LOG.debug("yfinance download failed %s %s", ticker, e)
        return None

    if df is None or (hasattr(df, 'empty') and df.empty):
        LOG.debug("yfinance empty for %s", ticker)
        return None

    # flatten multiindex columns if present
    try:
        if hasattr(df.columns, 'levels') and len(getattr(df.columns, 'levels', [])) > 1:
            df.columns = [('_'.join([str(x) for x in col]).strip()) for col in df.columns.values]
    except Exception:
        pass

    df = df.reset_index()
    # normalize typical column names
    rename_map = {"Date": "date", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume", "Adj Close": "adj_close"}
    df = df.rename(columns=rename_map)
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date']).dt.date
    df['code'] = code
    return df


def download_all(cfg, mode="full"):
    data_dir = cfg.get("data_dir", "data")
    ensure_dir(data_dir)

    # get stock list
    LOG.info("Fetching A-share code list")
    codes_df = ak.stock_info_a_code_name()
    codes = codes_df.to_dict("records")

    start_date_cfg = cfg.get("start_date", "20150101")
    update_delay_days = int(cfg.get("update_delay_days", 1))
    today = datetime.now().date()
    target_end_date = today - timedelta(days=update_delay_days)
    fetch_end_date = (target_end_date + timedelta(days=1)).strftime("%Y%m%d")
    sleep_sec = cfg.get("sleep_sec", 3.0)
    retry_times = cfg.get("retry_times", 5)
    output_format = cfg.get("output_format", "csv")
    merge_to_one = cfg.get("merge_to_one", False)
    source_priority = cfg.get("source_priority", ["yahoo", "tushare", "akshare"])

    total = len(codes)
    LOG.info("Found %d codes", total)

    processed = 0
    master_path = os.path.join(data_dir, f"all_a_share_{start_date_cfg}_{fetch_end_date}.csv") if output_format == "csv" and merge_to_one else None
    for rec in codes:
        code = str(rec.get("code")).zfill(6)
        name = rec.get("name")
        out_path = os.path.join(data_dir, f"{code}.{output_format}")

        if mode == "update":
            last_date = read_last_date(out_path) if output_format == "parquet" else None
            if last_date is None:
                sd = start_date_cfg
            else:
                sd_date = last_date + timedelta(days=1)
                sd = sd_date.strftime("%Y%m%d")
        else:
            sd = start_date_cfg

        if sd is None:
            sd = start_date_cfg

        if mode == "update" and sd > fetch_end_date:
            LOG.info("%s up-to-date", code)
            continue

        success = False
        for attempt in range(retry_times):
            ydf, used_source = fetch_best_available(code, sd, fetch_end_date, adjust=cfg.get("adjust", ""), source_priority=source_priority)

            if ydf is None or (hasattr(ydf, 'empty') and ydf.empty):
                LOG.debug("No data for %s (attempt %d)", code, attempt + 1)
                backoff = sleep_sec * (2 ** attempt)
                LOG.debug("Sleeping %.1fs before retry", backoff)
                time.sleep(backoff)
                continue

            try:
                sdf = standardize_df(ydf, code, name=name)
                if used_source:
                    sdf["source"] = used_source
                # save according to format
                if output_format == "csv":
                    # if update mode and csv exists, append new rows
                    if mode == "update" and os.path.exists(out_path):
                        try:
                            old = pd.read_csv(out_path, parse_dates=["date"], encoding="utf-8")
                            combined = pd.concat([old, sdf], ignore_index=True)
                            combined = combined.drop_duplicates(subset=["date"], keep="last")
                            combined.to_csv(out_path + ".tmp", index=False, encoding="utf-8")
                            os.replace(out_path + ".tmp", out_path)
                        except Exception:
                            atomic_write(out_path, sdf, fmt="csv")
                    else:
                        atomic_write(out_path, sdf, fmt="csv")
                else:
                    # parquet: if updating, merge with existing parquet file to avoid losing history
                    if mode == "update" and os.path.exists(out_path):
                        try:
                            old = pd.read_parquet(out_path)
                            combined = pd.concat([old, sdf], ignore_index=True)
                            if "date" in combined.columns:
                                combined = combined.drop_duplicates(subset=["date"], keep="last")
                            combined = combined.sort_values(by=[c for c in ("date",) if c in combined.columns])
                            atomic_write(out_path, combined, fmt="parquet")
                        except Exception:
                            atomic_write(out_path, sdf, fmt="parquet")
                    else:
                        atomic_write(out_path, sdf, fmt="parquet")

                # optionally append to master CSV
                if master_path:
                    header = not os.path.exists(master_path)
                    sdf.to_csv(master_path, mode='a', index=False, header=header, encoding='utf-8')

                success = True
                break
            except Exception as e:
                LOG.debug("save failed %s %s", code, e)
                time.sleep(sleep_sec)

        if not success:
            LOG.warning("Giving up on %s for now", code)

        processed += 1
        batch_n = cfg.get("batch_pause_count", 200)
        if batch_n and processed % batch_n == 0:
            pause = cfg.get("batch_pause_sec", 60)
            LOG.info("Processed %d stocks, pausing %.0fs to avoid rate limits", processed, pause)
            time.sleep(pause)
        else:
            time.sleep(sleep_sec)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["full", "update"], default="full")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config(args.config)

    if args.mode == "full":
        LOG.info("Starting full historical download")
    else:
        LOG.info("Starting incremental update")

    download_all(cfg, mode=args.mode)


if __name__ == "__main__":
    main()
