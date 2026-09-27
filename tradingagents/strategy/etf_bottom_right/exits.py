"""
退出系统 (§15-17, §2.7)

退出规则优先级（同日多条件时）:
1. 账户级熔断减仓
2. 初始 ATR 止损（全平）
3. 极端止盈（全平）
4. 时间止损（全平）
5. 二级止盈 / 均线止损（跌破 MA20 且次日未收回 → 全平）
6. 一级止盈（跌破 MA10 → 减仓 50%）
"""
import logging
import pandas as pd
import numpy as np
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass

from .config import StrategyConfig
from .portfolio import Position, PortfolioState

logger = logging.getLogger(__name__)


@dataclass
class ExitSignal:
    """退出信号"""
    symbol: str
    exit_date: str
    exit_type: str       # atr_stop / extreme_tp / time_stop / ma_stop / tp1
    exit_price: float
    exit_ratio: float    # 1.0 = 全平, 0.5 = 减仓 50%
    reason: str


class ExitManager:
    """退出管理器"""

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        self.ec = self.config.exit

    def check_atr_stop(
        self, position: Position, current_close: float, current_date: str
    ) -> Optional[ExitSignal]:
        """
        §15.1 初始 ATR 止损（固定）

        收盘价 ≤ 止损价 → 下一交易日开盘卖出（此处先标记信号）
        """
        if current_close <= position.stop_price:
            return ExitSignal(
                symbol=position.symbol,
                exit_date=current_date,
                exit_type="atr_stop",
                exit_price=current_close,
                exit_ratio=1.0,
                reason=f"ATR止损: close={current_close:.4f} <= stop={position.stop_price:.4f}"
            )
        return None

    def check_extreme_take_profit(
        self, position: Position, current_close: float,
        volume_ratio: float, daily_return: float, current_date: str
    ) -> Optional[ExitSignal]:
        """
        §16 极端止盈

        累计收益 > 30% 且 量比 > 2 且 当日涨幅 < 1% → 全平
        """
        cum_return = (current_close - position.entry_price) / position.entry_price

        if (
            cum_return > self.ec.extreme_profit_threshold
            and volume_ratio > self.ec.extreme_volume_ratio
            and abs(daily_return) < self.ec.extreme_price_change
        ):
            return ExitSignal(
                symbol=position.symbol,
                exit_date=current_date,
                exit_type="extreme_tp",
                exit_price=current_close,
                exit_ratio=1.0,
                reason=f"极端止盈: cum_ret={cum_return:.2%}, vol_ratio={volume_ratio:.2f}"
            )
        return None

    def check_time_stop(
        self, position: Position, closes_since_entry: List[float], current_date: str
    ) -> Optional[ExitSignal]:
        """
        §17 时间止损

        第 20 个交易日收盘后检查：若期间所有收盘价均 < 买入价 × 1.1 → 次日全平
        """
        if position.holding_days < self.ec.time_stop_days:
            return None

        threshold = position.entry_price * self.ec.time_stop_threshold
        all_below = all(c < threshold for c in closes_since_entry if c > 0)

        if all_below:
            return ExitSignal(
                symbol=position.symbol,
                exit_date=current_date,
                exit_type="time_stop",
                exit_price=closes_since_entry[-1] if closes_since_entry else position.entry_price,
                exit_ratio=1.0,
                reason=f"时间止损: {position.holding_days}日未达{self.ec.time_stop_threshold}倍"
            )
        return None

    def check_ma_stop(
        self, position: Position, current_close: float, ma20: float,
        prev_close: float, prev_ma20: float, current_date: str
    ) -> Optional[ExitSignal]:
        """
        §15.2 均线止损 = 二级止盈

        收盘跌破 MA20，且下一交易日收盘仍未收回 → 全部卖出。

        此处检查"次日未收回"：当前日 close < MA20 且 前日 close < MA20
        """
        below_today = current_close < ma20
        below_prev = prev_close < prev_ma20 if prev_ma20 > 0 else False

        if below_today and below_prev:
            return ExitSignal(
                symbol=position.symbol,
                exit_date=current_date,
                exit_type="ma_stop",
                exit_price=current_close,
                exit_ratio=1.0,
                reason=f"均线止损: close={current_close:.4f} < MA20={ma20:.4f}（连续两日）"
            )
        return None

    def check_take_profit_1(
        self, position: Position, current_close: float, ma10: float, current_date: str
    ) -> Optional[ExitSignal]:
        """
        §16 一级止盈

        收盘跌破 MA10 → 卖出剩余仓位 50%（每笔持仓仅一次）
        """
        if position.take_profit_1_used:
            return None

        if current_close < ma10:
            return ExitSignal(
                symbol=position.symbol,
                exit_date=current_date,
                exit_type="tp1",
                exit_price=current_close,
                exit_ratio=self.ec.take_profit_1_reduce,
                reason=f"一级止盈: close={current_close:.4f} < MA10={ma10:.4f}"
            )
        return None

    def check_all_exits(
        self,
        position: Position,
        current_date: str,
        current_close: float,
        prev_close: float,
        ma10: float,
        ma20: float,
        prev_ma20: float,
        volume_ratio: float,
        daily_return: float,
        closes_since_entry: List[float],
        circuit_breaker_active: bool = False,
    ) -> Optional[ExitSignal]:
        """
        按优先级检查全部退出条件 (§2.7)

        优先级:
        1. 账户级熔断减仓
        2. 初始 ATR 止损（全平）
        3. 极端止盈（全平）
        4. 时间止损（全平）
        5. 二级止盈 / 均线止损（全平）
        6. 一级止盈（减仓 50%）
        """
        # 1. 账户级熔断
        if circuit_breaker_active and self.ec.enable_circuit_breaker:
            return ExitSignal(
                symbol=position.symbol,
                exit_date=current_date,
                exit_type="circuit_breaker",
                exit_price=current_close,
                exit_ratio=1.0,
                reason="账户级熔断: 总仓位降至20%"
            )

        # 2. ATR 止损
        if self.ec.enable_atr_stop:
            signal = self.check_atr_stop(position, current_close, current_date)
            if signal:
                return signal

        # 3. 极端止盈
        if self.ec.enable_extreme_tp:
            signal = self.check_extreme_take_profit(
                position, current_close, volume_ratio, daily_return, current_date
            )
            if signal:
                return signal

        # 4. 时间止损
        if self.ec.enable_time_stop:
            signal = self.check_time_stop(position, closes_since_entry, current_date)
            if signal:
                return signal

        # 5. 均线止损 / 二级止盈
        if self.ec.enable_ma_stop:
            signal = self.check_ma_stop(
                position, current_close, ma20, prev_close, prev_ma20, current_date
            )
            if signal:
                return signal

        # 6. 一级止盈
        if self.ec.enable_tp1:
            signal = self.check_take_profit_1(position, current_close, ma10, current_date)
            if signal:
                return signal

        return None

    def compute_atr_stop_price(self, entry_price: float, atr14: float) -> float:
        """计算 ATR 止损价"""
        return entry_price - self.ec.atr_stop_multiplier * atr14
