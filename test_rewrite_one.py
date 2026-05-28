from downloader import fetch_with_yahoo, standardize_df, atomic_write
import os

code = '000001'
start = '20150101'
end = '20260528'
data_dir = 'D:/Backtest/a_share_downloader/data'

df = fetch_with_yahoo(code, start, end)
if df is None:
    print('fetch failed')
else:
    sdf = standardize_df(df, code, name=None)
    out = os.path.join(data_dir, f"{code}.csv")
    atomic_write(out, sdf, fmt='csv')
    print('wrote', out)
