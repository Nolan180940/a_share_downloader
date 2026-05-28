# A 股数据下载器（多源回退）

这个项目用于下载 A 股日线历史数据（2015-01-01 至今），采用以下回退顺序：

1. Yahoo / yfinance
2. Tushare Pro
3. AkShare

优先使用 Yahoo，因为它通常更稳定；如果 Yahoo 覆盖不到，再用 Tushare 补缺；最后才用 AkShare。

## 快速开始

1. 创建 Python 环境并安装依赖：

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

如果你要启用 Tushare 回退，请先设置 Token：

```bash
set TUSHARE_TOKEN=your_token_here
```

2. 下载全量历史数据：

```bash
python downloader.py --mode full --config config.yaml
```

3. 增量更新最新数据：

```bash
python downloader.py --mode update --config config.yaml
```

4. 只补北交所缺失股票：

```bash
set TUSHARE_TOKEN=your_token_here
python fill_missing_bj.py --dry-run
python fill_missing_bj.py
```

5. 每日定时增量更新：

```bash
set TUSHARE_TOKEN=your_token_here
python run_update.py
```

6. 运行本地回测：

```bash
python backtest_local.py --data-dir data
```

常用参数示例：

```bash
python backtest_local.py --data-dir data --fundamentals-file fundamentals.parquet --output-dir results --max-codes 100
```

## 参数说明

- `--fundamentals-file`：可选的基本面文件，字段至少要有 `code`、`announce_date`，最好再带 `market_cap`、`net_profit`。
- 如果不提供基本面文件，回测会退回到价格/流动性代理标的池，方便先用现有 parquet 数据做研究。
- 每次回测都会自动生成独立结果目录，不会覆盖上一次结果。
- 回测结果会同时保存 `equity_curve.csv`、`trades.csv`、`summary.json` 和 `equity_curve.png`。

## 目录说明

- `data/`：每只股票一个 Parquet 文件的历史行情数据目录。
- `results/`：回测输出目录，默认会按时间戳自动创建子目录。
- `config.yaml`：下载器配置文件，包含数据源优先级和输出格式。

## 重要说明

- `source_priority` 决定下载回退顺序。
- `output_format` 建议保持为 `parquet`。
- 如果只想补北交所缺失数据，请先设置 `TUSHARE_TOKEN`，再运行 `fill_missing_bj.py`。
- 如果你要做每日自动更新，建议用 Windows 任务计划程序定时执行 `run_update.py`。
- 本地回测是基于 parquet 历史数据重建的，不依赖 jqdata。

## 本地回测说明

原始的 jqdata 策略已经改写为适用于本地 parquet 数据的版本。

```bash
python backtest_local.py --data-dir data --start-date 2015-01-01 --end-date 2026-05-28
```

可选参数：

- `--fundamentals-file`：传入包含 `code`、`announce_date`、`market_cap`、`net_profit` 的 parquet 或 csv 文件，可以更接近原始策略的市值 / 利润筛选逻辑。
- 如果没有基本面文件，回测器会使用价格 / 流动性代理标的池，这样即使只有行情数据也能跑起来。
- 每次运行都会写入独立的结果目录，避免覆盖历史结果。

## 常见问题

- 如果下载某些股票失败，先确认网络是否稳定，再检查对应数据源是否可用。
- 如果 Tushare 回退不可用，检查 `TUSHARE_TOKEN` 是否已设置。
- 如果本地回测没有生成图表，请确认 `matplotlib` 已安装在当前虚拟环境中。
