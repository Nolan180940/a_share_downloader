"""Quick test: download two sample stocks and save to data dir."""
import os
import yaml
import pandas as pd
import akshare as ak
from datetime import datetime


cfg = yaml.safe_load(open("config.yaml", "r", encoding="utf-8"))
data_dir = cfg.get("data_dir", "data")
os.makedirs(data_dir, exist_ok=True)

codes = ["000001", "600519"]
start = cfg.get("start_date", "20150101")
end = datetime.now().strftime("%Y%m%d")

for code in codes:
    print(f"Downloading {code} {start} -> {end}")
    df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust=cfg.get("adjust", ""))
    if df is None or df.empty:
        print(f"No data for {code}")
        continue
    df = df.rename(columns={
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "涨跌幅": "pct_change",
        "涨跌额": "change",
        "换手率": "turnover",
    })
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["code"] = code
    out = os.path.join(data_dir, f"{code}.parquet")
    df.to_parquet(out, index=False)
    print("Saved", out)
