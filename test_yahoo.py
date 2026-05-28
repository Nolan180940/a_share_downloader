"""Test yfinance fallback for sample A-share codes."""
import yaml
from datetime import datetime
import yfinance as yf
import pandas as pd
import os


cfg = yaml.safe_load(open("config.yaml", "r", encoding="utf-8"))
start = cfg.get("start_date", "20150101")
end = datetime.now().strftime("%Y%m%d")

codes = ["000001", "000002", "000004", "600000", "600519"]

def map_ticker(code):
    # Shanghai codes usually start with 6
    suffix = "SS" if str(code).startswith("6") else "SZ"
    return f"{code}.{suffix}"


def fetch(code):
    ticker = map_ticker(code)
    print(f"Testing {code} -> {ticker}")
    # yfinance expects yyyy-mm-dd
    start_s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    end_s = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    try:
        df = yf.download(ticker, start=start_s, end=end_s, progress=False)
    except Exception as e:
        print("yfinance error:", e)
        return None
    print("Returned type/shape:", type(df), getattr(df, 'shape', None))
    try:
        cols = list(df.columns)
    except Exception:
        cols = None
    print("Columns:", cols)
    if df is None or (hasattr(df, 'empty') and df.empty):
        print("No data from yfinance for", ticker)
        return None
    # try to reset index and normalize
    try:
        df = df.reset_index()
        rename_map = {"Date":"date","Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume","Adj Close":"adj_close"}
        # handle if index is already date name
        df = df.rename(columns=rename_map)
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date']).dt.date
        print(df.head(3))
    except Exception as e:
        print('Error normalizing df:', e)
        return df
    return df


if __name__ == '__main__':
    for c in codes:
        try:
            fetch(c)
        except Exception as e:
            print("Exception for", c, e)
