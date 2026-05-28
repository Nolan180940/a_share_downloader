"""Local, jqdata-free rewrite of the user's RSI + dynamic-threshold strategy.

The original jqdata strategy is preserved as closely as possible, but all
platform-specific calls are routed through a local API object provided by the
backtest engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


def calculate_rsi(close_values: np.ndarray, period: int = 14) -> float:
    """Wilder-style RSI implementation that does not depend on TA-Lib."""
    values = np.asarray(close_values, dtype=float)
    if len(values) < period + 1:
        return float("nan")

    delta = np.diff(values)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)

    avg_gain = np.empty_like(gains)
    avg_loss = np.empty_like(losses)
    avg_gain[: period] = np.nan
    avg_loss[: period] = np.nan

    first_gain = gains[:period].mean()
    first_loss = losses[:period].mean()
    avg_gain[period - 1] = first_gain
    avg_loss[period - 1] = first_loss

    for i in range(period, len(gains)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i]) / period

    rs = np.divide(
        avg_gain,
        avg_loss,
        out=np.full_like(avg_gain, np.inf, dtype=float),
        where=avg_loss != 0,
    )
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(rsi[-1])


@dataclass
class StrategyParams:
    pool_size: int = 50
    position_count: int = 5
    rsi_period: int = 14
    base_threshold: float = 32.0
    bias_factor: float = 50.0
    stop_loss: float = -0.08
    lock_days_param: int = 3
    panic_threshold: int = 150
    index_security: str = "000852.XSHG"


class LocalDynamicRsiStrategy:
    """Local version of the jqdata strategy.

    The strategy keeps the same core parameters and decision flow:
    1. Compute a dynamic RSI threshold from the index bias.
    2. If a market-panic condition is detected, liquidate positions.
    3. Build a candidate pool from fundamentals (or a price-based fallback).
    4. Sell on stop-loss / RSI take-profit / out-of-pool.
    5. Buy low-RSI names up to the target position count.
    """

    def __init__(self, params: StrategyParams | None = None):
        self.params = params or StrategyParams()
        self.hold_days: dict[str, int] = {}

    def initialize(self, context: Any) -> None:
        """Match jqdata's initialize hook."""
        context.strategy_name = "LocalDynamicRsiStrategy"

    def trade_logic(self, context: Any, api: Any) -> None:
        current_data = api.get_current_data(context.current_date)

        # ------------------------------------------------------------
        # 0. Dynamic threshold from the benchmark index bias
        # ------------------------------------------------------------
        idx_data = api.attribute_history(
            self.params.index_security,
            21,
            end_date=context.previous_date,
            fields=["close"],
        )

        if len(idx_data) >= 20:
            curr_idx = float(idx_data["close"].values[-1])
            ma20 = float(idx_data["close"].values[-20:].mean())
            bias = (curr_idx - ma20) / ma20
            dynamic_thresh = self.params.base_threshold + (bias * self.params.bias_factor)
            dynamic_thresh = float(np.clip(dynamic_thresh, 20, 45))
        else:
            dynamic_thresh = self.params.base_threshold

        # ------------------------------------------------------------
        # 1. Liquidity crisis guard
        # ------------------------------------------------------------
        if self.check_market_panic(context, api):
            api.log("【极速避险】检测到流动性危机！今日清仓，暂停买入！")
            self.clean_positions(context, current_data, api)
            return

        # ------------------------------------------------------------
        # 2. Update hold days for existing positions
        # ------------------------------------------------------------
        for stock in list(context.portfolio.positions.keys()):
            self.hold_days[stock] = self.hold_days.get(stock, 0) + 1

        # ------------------------------------------------------------
        # 3. Candidate pool
        # ------------------------------------------------------------
        df = api.get_fundamentals(context.previous_date, limit=300)
        if df is None or len(df) == 0:
            return

        initial_list = list(df["code"])

        target_pool: list[str] = []
        for s in initial_list:
            if s.startswith(("300", "688", "8", "4")):
                continue
            snap = current_data.get(s)
            if snap is None:
                continue
            if snap.paused or snap.is_st:
                continue
            if "ST" in snap.name or "退" in snap.name:
                continue
            target_pool.append(s)
            if len(target_pool) >= self.params.pool_size:
                break

        # ------------------------------------------------------------
        # 4. Sell logic
        # ------------------------------------------------------------
        for stock in list(context.portfolio.positions.keys()):
            snap = current_data.get(stock)
            if snap is None or snap.paused:
                continue

            days = self.hold_days.get(stock, 0)
            is_limit_up = snap.last_price >= snap.high_limit

            position = context.portfolio.positions[stock]
            cost = position.avg_cost
            price = snap.last_price
            if cost > 0 and (price - cost) / cost < self.params.stop_loss:
                api.order_target(context, stock, 0)
                self.hold_days.pop(stock, None)
                continue

            if days < self.params.lock_days_param and not is_limit_up:
                continue

            prices = api.attribute_history(
                stock,
                30,
                end_date=context.previous_date,
                fields=["close"],
            )["close"].values
            if len(prices) < 20:
                continue

            rsi = calculate_rsi(prices, period=self.params.rsi_period)
            if np.isnan(rsi):
                continue

            if rsi > 75 or stock not in target_pool:
                api.order_target(context, stock, 0)
                self.hold_days.pop(stock, None)

        # ------------------------------------------------------------
        # 5. Buy logic
        # ------------------------------------------------------------
        if len(context.portfolio.positions) >= self.params.position_count:
            return

        candidates: list[tuple[str, float]] = []
        for stock in target_pool:
            if stock in context.portfolio.positions:
                continue

            prices = api.attribute_history(
                stock,
                30,
                end_date=context.previous_date,
                fields=["close"],
            )["close"].values
            if len(prices) < 20:
                continue

            rsi = calculate_rsi(prices, period=self.params.rsi_period)
            if np.isnan(rsi):
                continue

            if rsi < dynamic_thresh:
                candidates.append((stock, rsi))

        candidates.sort(key=lambda x: x[1])

        current_pos = len(context.portfolio.positions)
        available_slots = self.params.position_count - current_pos
        if available_slots <= 0:
            return

        cash_per_stock = context.portfolio.available_cash / available_slots

        for stock, _rsi_val in candidates:
            if len(context.portfolio.positions) >= self.params.position_count:
                break
            if cash_per_stock < 1000:
                break
            snap = current_data.get(stock)
            if snap is None or snap.paused:
                continue
            api.order_value(context, stock, cash_per_stock)
            self.hold_days[stock] = 0

    def clean_positions(self, context: Any, current_data: dict[str, Any], api: Any) -> None:
        for stock in list(context.portfolio.positions.keys()):
            snap = current_data.get(stock)
            if snap is None or snap.paused:
                continue
            api.order_target(context, stock, 0)
            self.hold_days.pop(stock, None)

    def check_market_panic(self, context: Any, api: Any) -> bool:
        idx_prices = api.attribute_history(
            self.params.index_security,
            22,
            end_date=context.previous_date,
            fields=["close"],
        )["close"].values

        if len(idx_prices) < 22:
            return False

        curr_idx = float(idx_prices[-1])
        ma20 = float(idx_prices[-20:].mean())
        if curr_idx > ma20:
            return False

        prev_date = context.previous_date
        sample_stocks = api.get_index_stocks(self.params.index_security, date=prev_date)
        if not sample_stocks:
            return False

        df = api.get_price(
            sample_stocks,
            end_date=prev_date,
            fields=["close", "low_limit", "volume"],
            count=1,
        )

        if df is None or len(df) == 0:
            return False

        panic_stocks = df[(df["close"] <= df["low_limit"] + 0.01) & (df["volume"] > 0)]

        # Scale the original threshold (150 / ~1000 CSI-1000 constituents)
        # when we fall back to a price-based proxy universe.
        scaled_threshold = max(1, int(round(self.params.panic_threshold * len(sample_stocks) / 1000)))
        return len(panic_stocks) > scaled_threshold
