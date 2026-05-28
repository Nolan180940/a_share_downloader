"""Local daily backtest engine for the refactored RSI strategy.

This runner uses the parquet files already downloaded into `data/` and does
not depend on jqdata. It supports:
  - order_value / order_target execution at daily open
  - stop-loss / lock-days / RSI take-profit
  - optional fundamentals parquet/csv for the original market-cap/net-profit filter
  - price-only fallback universe when no fundamentals file is provided

Outputs:
  - results/equity_curve.csv
  - results/trades.csv
  - results/summary.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from strategy_local import LocalDynamicRsiStrategy, StrategyParams


@dataclass
class Snapshot:
    code: str
    name: str
    last_price: float
    high_limit: float
    low_limit: float
    paused: bool
    is_st: bool


@dataclass
class Position:
    code: str
    amount: int
    avg_cost: float
    last_price: float = 0.0

    @property
    def market_value(self) -> float:
        price = self.last_price
        if price is None or pd.isna(price) or not np.isfinite(price):
            price = self.avg_cost
        if price is None or pd.isna(price) or not np.isfinite(price):
            price = 0.0
        return float(self.amount) * float(price)


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    @property
    def available_cash(self) -> float:
        return self.cash

    @property
    def total_value(self) -> float:
        cash = self.cash
        if cash is None or pd.isna(cash) or not np.isfinite(cash):
            cash = 0.0
        return float(cash) + sum(p.market_value for p in self.positions.values())


@dataclass
class Context:
    current_date: pd.Timestamp
    previous_date: pd.Timestamp
    portfolio: Portfolio
    strategy_name: str = ""


class DataStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.files = sorted(self.data_dir.glob("*.parquet"))
        self.codes = sorted({p.stem[:6] for p in self.files if p.stem[:6].isdigit()})
        self._data_cache: dict[str, pd.DataFrame] = {}
        self._liquidity_cache: dict[str, pd.DataFrame] = {}
        self._dates_cache: list[pd.Timestamp] | None = None

    def load(self, code: str) -> pd.DataFrame:
        code = str(code).zfill(6)
        if code not in self._data_cache:
            path = self.data_dir / f"{code}.parquet"
            if not path.exists():
                self._data_cache[code] = pd.DataFrame()
            else:
                df = pd.read_parquet(path)
                if "date" in df.columns:
                    df["date"] = pd.to_datetime(df["date"])
                    df = df.sort_values("date").reset_index(drop=True)
                if "close" in df.columns and "volume" in df.columns:
                    df["turnover"] = pd.to_numeric(df["close"], errors="coerce") * pd.to_numeric(df["volume"], errors="coerce")
                if "close" in df.columns:
                    df["prev_close"] = df["close"].shift(1)
                    df["high_limit"] = df["prev_close"] * 1.10
                    df["low_limit"] = df["prev_close"] * 0.90
                self._data_cache[code] = df
        return self._data_cache[code]

    def load_liquidity(self, code: str) -> pd.DataFrame:
        code = str(code).zfill(6)
        if code not in self._liquidity_cache:
            df = self.load(code)
            if df.empty:
                self._liquidity_cache[code] = pd.DataFrame(columns=["date", "turnover_20", "turnover_60"])
            else:
                out = df[["date", "turnover"]].copy()
                out["turnover_20"] = out["turnover"].rolling(20, min_periods=20).mean()
                out["turnover_60"] = out["turnover"].rolling(60, min_periods=60).mean()
                self._liquidity_cache[code] = out[["date", "turnover_20", "turnover_60"]]
        return self._liquidity_cache[code]

    def benchmark_dates(self, benchmark_code: str = "000852") -> list[pd.Timestamp]:
        if self._dates_cache is not None:
            return self._dates_cache
        bench = self.load(benchmark_code)
        if bench.empty:
            # fallback to the first available symbol
            if not self.codes:
                self._dates_cache = []
            else:
                self._dates_cache = list(self.load(self.codes[0])["date"].dropna().drop_duplicates())
        else:
            self._dates_cache = list(bench["date"].dropna().drop_duplicates())
        self._dates_cache = sorted(pd.to_datetime(self._dates_cache).tolist())
        return self._dates_cache

    def history(self, code: str, end_date: pd.Timestamp, count: int, fields: list[str]) -> pd.DataFrame:
        df = self.load(code)
        if df.empty:
            return pd.DataFrame(columns=fields)
        sub = df[df["date"] <= end_date]
        if count:
            sub = sub.tail(count)
        cols = [c for c in fields if c in sub.columns]
        return sub[cols].copy()

    def snapshot(self, code: str, date: pd.Timestamp) -> Snapshot | None:
        df = self.load(code)
        if df.empty:
            return None
        row = df[df["date"] == date]
        if row.empty:
            return None
        r = row.iloc[-1]
        name = str(r.get("name", code))
        last_price = float(r.get("open", r.get("close", np.nan)))
        if not np.isfinite(last_price):
            last_price = float(r.get("close", np.nan))
        prev_close = float(r.get("prev_close", np.nan))
        high_limit = float(r.get("high_limit", np.nan))
        low_limit = float(r.get("low_limit", np.nan))
        paused = bool(pd.isna(last_price) or pd.isna(r.get("volume", np.nan)) or float(r.get("volume", 0.0)) <= 0)
        if pd.isna(high_limit) and not pd.isna(prev_close):
            high_limit = prev_close * 1.10
        if pd.isna(low_limit) and not pd.isna(prev_close):
            low_limit = prev_close * 0.90
        return Snapshot(
            code=code,
            name=name,
            last_price=last_price,
            high_limit=high_limit,
            low_limit=low_limit,
            paused=paused,
            is_st=("ST" in name) or ("退" in name),
        )


class FundamentalsStore:
    def __init__(self, file_path: str | Path | None, data_store: DataStore):
        self.file_path = Path(file_path) if file_path else None
        self.data_store = data_store
        self._df: pd.DataFrame | None = None

    @property
    def available(self) -> bool:
        return self.file_path is not None and self.file_path.exists()

    def _load(self) -> pd.DataFrame:
        if self._df is not None:
            return self._df
        if not self.available:
            self._df = pd.DataFrame()
            return self._df
        if self.file_path.suffix.lower() == ".parquet":
            df = pd.read_parquet(self.file_path)
        else:
            df = pd.read_csv(self.file_path)
        # Normalize common column names from Tushare / other sources
        if "announce_date" in df.columns:
            df["announce_date"] = pd.to_datetime(df["announce_date"])
        elif "date" in df.columns:
            df["announce_date"] = pd.to_datetime(df["date"])
        elif "trade_date" in df.columns:
            df["announce_date"] = pd.to_datetime(df["trade_date"])

        # Support Tushare's ts_code column -> normalized 6-digit `code`
        if "code" in df.columns:
            df["code"] = df["code"].astype(str).str.zfill(6)
        elif "ts_code" in df.columns:
            # ts_code like '000001.SZ' -> code '000001'
            df["code"] = df["ts_code"].astype(str).str[:6]
        self._df = df
        return self._df

    def select(self, date: pd.Timestamp, limit: int = 300) -> pd.DataFrame:
        df = self._load()
        if df.empty:
            return self._proxy_select(date, limit=limit)

        cut = pd.Timestamp(date)
        if "announce_date" not in df.columns:
            return self._proxy_select(date, limit=limit)

        visible = df[df["announce_date"] <= cut].copy()
        if visible.empty:
            return self._proxy_select(date, limit=limit)

        sort_cols = [c for c in ["code", "announce_date"] if c in visible.columns]
        visible = visible.sort_values(sort_cols)
        latest = visible.groupby("code", as_index=False).tail(1).copy()

        if "market_cap" in latest.columns and "net_profit" in latest.columns:
            latest = latest[(latest["market_cap"] > 0) & (latest["net_profit"] > 0)]

        if "market_cap" in latest.columns:
            latest = latest.sort_values("market_cap", ascending=True)
        else:
            latest = latest.sort_values("code")

        return latest[[c for c in ["code", "market_cap", "net_profit"] if c in latest.columns]].head(limit).reset_index(drop=True)

    def _proxy_select(self, date: pd.Timestamp, limit: int = 300) -> pd.DataFrame:
        rows = []
        cut = pd.Timestamp(date)
        for code in self.data_store.codes:
            if code.startswith(("300", "688", "8", "4")):
                continue
            liq = self.data_store.load_liquidity(code)
            if liq.empty:
                continue
            row = liq[liq["date"] <= cut].tail(1)
            if row.empty:
                continue
            turnover_60 = row.iloc[-1]["turnover_60"]
            turnover_20 = row.iloc[-1]["turnover_20"]
            if pd.isna(turnover_60) or turnover_60 <= 0:
                continue
            rows.append((code, float(turnover_60), float(turnover_20) if not pd.isna(turnover_20) else float(turnover_60)))

        if not rows:
            return pd.DataFrame(columns=["code"])

        out = pd.DataFrame(rows, columns=["code", "market_cap", "net_profit"])
        # Smaller average turnover first acts as a rough small-cap proxy.
        return out.sort_values("market_cap", ascending=True).head(limit).reset_index(drop=True)


class LocalMarketAPI:
    def __init__(self, data_store: DataStore, fundamentals_store: FundamentalsStore, benchmark_code: str = "000852"):
        self.data_store = data_store
        self.fundamentals_store = fundamentals_store
        self.benchmark_code = benchmark_code
        self._index_cache: dict[tuple[str, str], list[str]] = {}

    def log(self, message: str) -> None:
        print(message)

    def get_current_data(self, date: pd.Timestamp) -> dict[str, Snapshot]:
        out: dict[str, Snapshot] = {}
        for code in self.data_store.codes:
            snap = self.data_store.snapshot(code, date)
            if snap is not None:
                out[code] = snap
        return out

    def attribute_history(self, code: str, count: int, end_date: pd.Timestamp, fields: list[str]) -> pd.DataFrame:
        code = str(code).zfill(6)
        return self.data_store.history(code, end_date=end_date, count=count, fields=fields)

    def get_price(
        self,
        codes: list[str],
        end_date: pd.Timestamp,
        fields: list[str],
        count: int = 1,
    ) -> pd.DataFrame:
        rows = []
        for code in codes:
            df = self.data_store.load(code)
            if df.empty:
                continue
            sub = df[df["date"] <= end_date].tail(count)
            if sub.empty:
                continue
            r = sub.iloc[-1]
            row = {"code": code}
            for field in fields:
                if field == "low_limit":
                    row[field] = float(r.get("low_limit", np.nan))
                elif field == "high_limit":
                    row[field] = float(r.get("high_limit", np.nan))
                elif field in sub.columns:
                    row[field] = r[field]
                else:
                    row[field] = np.nan
            rows.append(row)
        return pd.DataFrame(rows)

    def get_index_stocks(self, index_code: str, date: pd.Timestamp) -> list[str]:
        cache_key = (index_code, str(pd.Timestamp(date).date()))
        if cache_key in self._index_cache:
            return self._index_cache[cache_key]

        # Prefer an index-members file if you have one in the future.
        # For now we use a proxy universe: the 1000 most liquid non-BJ / non-STAR / non-ChiNext names.
        ranks = []
        for code in self.data_store.codes:
            if code.startswith(("300", "688", "8", "4")):
                continue
            liq = self.data_store.load_liquidity(code)
            if liq.empty:
                continue
            row = liq[liq["date"] <= pd.Timestamp(date)].tail(1)
            if row.empty:
                continue
            turnover = row.iloc[-1]["turnover_60"]
            if pd.isna(turnover) or turnover <= 0:
                continue
            ranks.append((code, float(turnover)))

        ranks.sort(key=lambda x: x[1], reverse=True)
        stocks = [x[0] for x in ranks[:1000]]
        self._index_cache[cache_key] = stocks
        return stocks

    def get_fundamentals(self, date: pd.Timestamp, limit: int = 300) -> pd.DataFrame:
        return self.fundamentals_store.select(date, limit=limit)

    def order_target(self, context: Context, stock: str, target_amount: int) -> None:
        stock = str(stock).zfill(6)
        snap = self.get_current_data(context.current_date).get(stock)
        if snap is None:
            return

        current_pos = context.portfolio.positions.get(stock)
        if target_amount <= 0:
            if current_pos is None or current_pos.amount <= 0:
                return
            sell_amount = current_pos.amount
            self._execute_sell(context, stock, sell_amount, snap.last_price)
            return

    def order_value(self, context: Context, stock: str, cash_value: float) -> None:
        stock = str(stock).zfill(6)
        snap = self.get_current_data(context.current_date).get(stock)
        if snap is None or snap.paused:
            return
        if snap.last_price <= 0 or snap.last_price >= snap.high_limit:
            return

        lot_size = 100
        price = snap.last_price
        max_lots = int(cash_value // (price * lot_size))
        amount = max_lots * lot_size
        if amount <= 0:
            return

        cost = amount * price
        commission = max(5.0, cost * 0.0003)
        total = cost + commission
        if total > context.portfolio.cash:
            max_lots = int((context.portfolio.cash - 5.0) // (price * lot_size * (1.0 + 0.0003)))
            amount = max_lots * lot_size
            if amount <= 0:
                return
            cost = amount * price
            commission = max(5.0, cost * 0.0003)
            total = cost + commission

        position = context.portfolio.positions.get(stock)
        if position is None:
            context.portfolio.positions[stock] = Position(code=stock, amount=amount, avg_cost=price, last_price=price)
        else:
            new_amount = position.amount + amount
            new_avg = (position.avg_cost * position.amount + price * amount) / new_amount
            position.amount = new_amount
            position.avg_cost = new_avg
            position.last_price = price

        context.portfolio.cash -= total
        self._record_trade(context, stock, "BUY", amount, price, commission=commission, tax=0.0)

    def _execute_sell(self, context: Context, stock: str, amount: int, price: float) -> None:
        position = context.portfolio.positions.get(stock)
        if position is None or amount <= 0:
            return
        amount = min(amount, position.amount)
        proceeds = amount * price
        commission = max(5.0, proceeds * 0.0003)
        tax = proceeds * 0.001
        net = proceeds - commission - tax
        context.portfolio.cash += net
        position.amount -= amount
        position.last_price = price
        if position.amount <= 0:
            context.portfolio.positions.pop(stock, None)
        self._record_trade(context, stock, "SELL", amount, price, commission=commission, tax=tax)

    def _record_trade(self, context: Context, stock: str, side: str, amount: int, price: float, commission: float, tax: float) -> None:
        if not hasattr(self, "_trade_log"):
            self._trade_log: list[dict[str, Any]] = []
        self._trade_log.append(
            {
                "date": context.current_date,
                "code": stock,
                "side": side,
                "amount": int(amount),
                "price": float(price),
                "commission": float(commission),
                "tax": float(tax),
                "cash_after": float(context.portfolio.cash),
            }
        )


class BacktestRunner:
    def __init__(
        self,
        data_store: DataStore,
        api: LocalMarketAPI,
        strategy: LocalDynamicRsiStrategy,
        initial_cash: float = 1_000_000.0,
    ):
        self.data_store = data_store
        self.api = api
        self.strategy = strategy
        self.initial_cash = initial_cash
        self.trades: list[dict[str, Any]] = []
        self.equity_curve: list[dict[str, Any]] = []

    def run(self, start_date: str, end_date: str, max_codes: int | None = None) -> dict[str, Any]:
        self.strategy.initialize(Context(pd.Timestamp(start_date), pd.Timestamp(start_date), Portfolio(self.initial_cash)))
        self.api._trade_log = []

        dates = [d for d in self.data_store.benchmark_dates(self.strategy.params.index_security[:6]) if pd.Timestamp(start_date) <= d <= pd.Timestamp(end_date)]
        if not dates:
            raise RuntimeError("No trading dates found in the benchmark data.")

        context = Context(
            current_date=dates[0],
            previous_date=dates[0],
            portfolio=Portfolio(self.initial_cash),
        )

        if max_codes is not None:
            self.data_store.codes = self.data_store.codes[:max_codes]

        for i, date in enumerate(dates):
            prev_date = dates[i - 1] if i > 0 else date
            context.current_date = date
            context.previous_date = prev_date

            # Update last prices to today's close for mark-to-market.
            self._mark_to_market(context, date)

            # Let strategy generate / execute orders using today's open.
            self.strategy.trade_logic(context, self.api)

            # Record end-of-day equity after orders.
            self._mark_to_market(context, date)
            self.equity_curve.append(
                {
                    "date": date,
                    "cash": context.portfolio.cash,
                    "equity": context.portfolio.total_value,
                    "positions": len(context.portfolio.positions),
                }
            )

        self.trades = list(getattr(self.api, "_trade_log", []))

        return self._summarize(context)

    def _mark_to_market(self, context: Context, date: pd.Timestamp) -> None:
        for code, pos in list(context.portfolio.positions.items()):
            df = self.data_store.load(code)
            if df.empty:
                continue
            row = df[df["date"] == date]
            if row.empty:
                continue
            close = float(row.iloc[-1].get("close", np.nan))
            if np.isfinite(close):
                pos.last_price = close

    def _summarize(self, context: Context) -> dict[str, Any]:
        equity_df = pd.DataFrame(self.equity_curve)
        if equity_df.empty:
            raise RuntimeError("Backtest produced no equity curve.")

        equity_df["return"] = equity_df["equity"].pct_change().fillna(0.0)
        equity_df["cummax"] = equity_df["equity"].cummax()
        equity_df["drawdown"] = equity_df["equity"] / equity_df["cummax"] - 1.0

        total_return = equity_df["equity"].iloc[-1] / self.initial_cash - 1.0
        days = max(1, len(equity_df))
        annualized = (1.0 + total_return) ** (252.0 / days) - 1.0
        max_drawdown = float(equity_df["drawdown"].min())
        volatility = float(equity_df["return"].std(ddof=0) * math.sqrt(252))
        sharpe = float((equity_df["return"].mean() / (equity_df["return"].std(ddof=0) + 1e-12)) * math.sqrt(252))

        return {
            "final_equity": float(equity_df["equity"].iloc[-1]),
            "total_return": float(total_return),
            "annualized_return": float(annualized),
            "max_drawdown": max_drawdown,
            "volatility": volatility,
            "sharpe": sharpe,
            "equity_curve": equity_df,
            "trades": pd.DataFrame(self.trades),
        }


def save_outputs(results: dict[str, Any], output_dir: str | Path) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results["equity_curve"].to_csv(output_dir / "equity_curve.csv", index=False)
    results["trades"].to_csv(output_dir / "trades.csv", index=False)

    summary = {
        k: v
        for k, v in results.items()
        if k not in {"equity_curve", "trades"}
    }
    with open(output_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    save_equity_chart(results["equity_curve"], output_dir / "equity_curve.png")


def save_equity_chart(equity_df: pd.DataFrame, output_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(12, 6), dpi=140)
    plot_df = equity_df.copy()
    plot_df["date"] = pd.to_datetime(plot_df["date"])
    plot_df = plot_df.sort_values("date")

    ax.plot(plot_df["date"], plot_df["equity"], label="Strategy Equity", color="#1f77b4", linewidth=1.8)

    if "cash" in plot_df.columns:
        ax.plot(plot_df["date"], plot_df["cash"], label="Cash", color="#ff7f0e", linewidth=1.0, alpha=0.8)

    if "cummax" in plot_df.columns:
        ax.plot(plot_df["date"], plot_df["cummax"], label="Equity Peak", color="#2ca02c", linewidth=1.0, alpha=0.7, linestyle="--")

    ax.set_title("Local Backtest Equity Curve")
    ax.set_xlabel("Date")
    ax.set_ylabel("Value")
    ax.grid(True, linestyle="--", alpha=0.25)
    ax.legend(loc="best")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--fundamentals-file", default="")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--run-name", default="")
    parser.add_argument("--start-date", default="2015-01-01")
    parser.add_argument("--end-date", default=str(pd.Timestamp.now().date()))
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--max-codes", type=int, default=None)
    parser.add_argument("--benchmark-code", default="000852")
    args = parser.parse_args()

    data_store = DataStore(args.data_dir)
    fundamentals_file = args.fundamentals_file.strip() or None
    fundamentals_store = FundamentalsStore(fundamentals_file, data_store)
    api = LocalMarketAPI(data_store, fundamentals_store, benchmark_code=args.benchmark_code)
    strategy = LocalDynamicRsiStrategy(StrategyParams(index_security=args.benchmark_code))

    runner = BacktestRunner(
        data_store=data_store,
        api=api,
        strategy=strategy,
        initial_cash=args.initial_cash,
    )

    results = runner.run(args.start_date, args.end_date, max_codes=args.max_codes)

    run_dir = build_run_directory(args.output_dir, args.run_name)
    save_outputs(results, run_dir)

    print("final_equity:", results["final_equity"])
    print("total_return:", results["total_return"])
    print("annualized_return:", results["annualized_return"])
    print("max_drawdown:", results["max_drawdown"])
    print("sharpe:", results["sharpe"])
    print(f"saved to: {Path(run_dir).resolve()}")


def build_run_directory(base_output_dir: str | Path, run_name: str = "") -> Path:
    base = Path(base_output_dir)
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    name = f"{stamp}_{run_name.strip()}" if run_name.strip() else stamp
    candidate = base / name
    suffix = 1
    while candidate.exists():
        candidate = base / f"{name}_{suffix:02d}"
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


if __name__ == "__main__":
    main()
