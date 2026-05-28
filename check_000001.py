import pandas as pd
p='D:/Backtest/a_share_downloader/data/000001.csv'
df=pd.read_csv(p, parse_dates=['date'])
print('columns:', df.columns.tolist())
print('rows:', len(df))
print('date range:', df['date'].min().date(), df['date'].max().date())
print('nulls:\n', df.isnull().sum())
print('\nhead:\n', df.head(3).to_string(index=False))
