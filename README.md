# A 股数据操作手册

这个仓库的日常使用只需要记住三步：先下载全量数据，再分别做 Yahoo 增量更新和北交所增量更新，最后运行本地回测。

## Quick Start

在 Windows PowerShell 里先进入项目并激活虚拟环境：

```powershell
Set-Location D:\Backtest\a_share_downloader
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& .\.venv\Scripts\Activate.ps1
```

如果还没安装依赖，先执行：

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 1. 从零开始下载全量数据

第一次使用时，直接下全量历史数据到 `data/`：

```powershell
Set-Location D:\Backtest\a_share_downloader
.\.venv\Scripts\python.exe downloader.py --mode full --config config.yaml
```

这一步会建立每只股票一个 parquet 文件。后续日常更新都基于这个目录。

## 2. Yahoo 快速增量更新

这是日常最常用、也最快的更新方式。它只更新本地已经存在的沪深股票文件，并自动把数据整理成统一的 8 列：`code, name, date, open, close, high, low, volume`。

```powershell
Set-Location D:\Backtest\a_share_downloader
.\.venv\Scripts\python.exe update_local_yahoo.py --data-dir data --update-delay-days 1 --max-workers 4 --batch-size 100
```

如果你想固定更新到某一天，可以改成：

```powershell
Set-Location D:\Backtest\a_share_downloader
.\.venv\Scripts\python.exe update_local_yahoo.py --data-dir data --end-date 20260528 --max-workers 4 --batch-size 100
```

说明：`update_local_yahoo.py` 只负责 Yahoo 覆盖得到的股票，北交所不走这条路。

## 3. 北交所单独更新

北交所文件要单独走 Tushare 更新。运行前先设置 `TUSHARE_TOKEN`：

```powershell
Set-Location D:\Backtest\a_share_downloader
$env:TUSHARE_TOKEN = "your_token_here"
.\.venv\Scripts\python.exe update_bj_tushare.py --data-dir data --sleep-sec 1.3
```

如果你要指定更新截止日期，可以加上 `--end-date`：

```powershell
Set-Location D:\Backtest\a_share_downloader
$env:TUSHARE_TOKEN = "your_token_here"
.\.venv\Scripts\python.exe update_bj_tushare.py --data-dir data --end-date 20260528 --sleep-sec 1.3
```

说明：`update_bj_tushare.py` 只更新已经存在的 92 开头 BJ 文件，脚本是顺序执行的，默认会加一点等待，避免 Tushare 频率限制。

如果你的 BJ 文件本身缺失，需要先补建文件，再跑：

```powershell
Set-Location D:\Backtest\a_share_downloader
$env:TUSHARE_TOKEN = "your_token_here"
.\.venv\Scripts\python.exe fill_missing_bj.py --dry-run
.\.venv\Scripts\python.exe fill_missing_bj.py
```

## 4. 本地回测

回测基于本地 parquet 历史数据运行。最常用的命令如下：

```powershell
Set-Location D:\Backtest\a_share_downloader
.\.venv\Scripts\python.exe backtest_local.py --data-dir data --fundamentals-file fundamentals/daily_basic.parquet --start-date 2024-01-01 --end-date 2026-05-28 --max-codes 200 --output-dir results_test
```

如果你已经在当前环境里激活了虚拟环境，也可以直接跑：

```powershell
python backtest_local.py --data-dir data --fundamentals-file fundamentals/daily_basic.parquet
```

`--fundamentals-file` 可以是 `parquet` 或 `csv`。如果不传这个参数，回测会退回到基于价格和流动性的代理标的池。

## 5. 基本面数据抓取

如果你还没准备基本面文件，可以先抓：

```powershell
Set-Location D:\Backtest\a_share_downloader
$env:TUSHARE_TOKEN = "your_token_here"
.\.venv\Scripts\python.exe fetch_fundamentals.py --out-dir fundamentals --start-date 20150101 --end-date 20260528
```

常见产物是：

```text
fundamentals/stock_basic.parquet
fundamentals/daily_basic.parquet
```

## 日常顺序

1. 第一次运行先用 `downloader.py --mode full` 建 `data/`。
2. 每天先跑 `update_local_yahoo.py`。
3. 再跑 `update_bj_tushare.py`。
4. 最后跑 `backtest_local.py`。

## 常见问题

- 如果 Yahoo 更新没有变化，先确认当前文件本身是否已经到最新交易日。
- 如果 BJ 更新报 Tushare 限流，保持 `--sleep-sec 1.3` 或更大。
- 如果 Tushare 没数据，先确认 `TUSHARE_TOKEN` 是否已经设置在当前 PowerShell 会话里。
- 如果回测没有生成图表，确认当前虚拟环境里已经安装 `matplotlib`。
