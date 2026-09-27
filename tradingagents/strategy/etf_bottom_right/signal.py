"""
买入信号生成 (§12)

模式 A（稳健型·主策略）:
  连续两个交易日 FinalScore >= 75 且通过长周期过滤
  基础仓位 10%

模式 B（进攻型·辅助）:
  FinalScore >= 90 且已确认突破 60 日高点 且 VolTrend > 1.3
  且 Excess_20 > 3% 且 Position <= 0.95
  仓位 = 模式A基础仓位 × 50%，单 ETF <= 5%，进攻仓合计 <= 30%

信号日收盘确认，下一交易日开盘价成交；涨跌停无法成交则取消。
"""
import logging
import pandas as pd
import numpy as np
from typing import Optional, List, Dict

from .config import StrategyConfig

logger = logging.getLogger(__name__)


class SignalGenerator:
    """买入信号生成器"""

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        self.sc = self.config.signal

    def detect_mode_a(self, df: pd.DataFrame) -> pd.Series:
        """
        检测模式 A 信号（稳健型）

        连续两个交易日: FinalScore >= 75 且通过长周期过滤

        Returns:
            bool 序列，True 表示当日产生信号
        """
        score_ok = df["final_score"] >= self.sc.mode_a_score_threshold
        filter_ok = df.get("long_filter_passed", pd.Series(True, index=df.index))

        daily_ok = score_ok & filter_ok

        # 连续两日满足
        confirm = daily_ok & daily_ok.shift(1).fillna(False)
        return confirm

    def detect_mode_b(self, df: pd.DataFrame) -> pd.Series:
        """
        检测模式 B 信号（进攻型）

        同时满足:
        - FinalScore >= 90
        - 已确认突破 60 日高点
        - VolTrend > 1.3
        - Excess_20 > 3%
        - Position <= 0.95
        """
        score_ok = df["final_score"] >= self.sc.mode_b_score_threshold
        breakout_ok = df.get("confirmed_breakout_60", pd.Series(False, index=df.index)).fillna(False)
        vol_ok = df.get("vol_trend", pd.Series(0, index=df.index)) > self.sc.mode_b_vol_trend
        excess_ok = df.get("excess_20", pd.Series(0, index=df.index)) > self.sc.mode_b_excess_20
        position_ok = df.get("position", pd.Series(1, index=df.index)) <= 0.95

        return score_ok & breakout_ok & vol_ok & excess_ok & position_ok

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        为单只 ETF 生成买入信号

        Returns:
            添加 buy_signal / signal_mode / signal_position 列的 DataFrame
        """
        df = df.copy()

        mode_a = self.detect_mode_a(df)
        mode_b = self.detect_mode_b(df)

        # 信号模式（A 优先，B 补充）
        df["signal_mode_a"] = mode_a
        df["signal_mode_b"] = mode_b

        # 综合信号
        df["buy_signal"] = mode_a | mode_b
        df["signal_mode"] = "None"
        df.loc[mode_a, "signal_mode"] = "A"
        df.loc[mode_b & ~mode_a, "signal_mode"] = "B"

        # 信号仓位
        df["signal_position"] = 0.0
        df.loc[mode_a, "signal_position"] = self.sc.mode_a_base_position
        df.loc[mode_b & ~mode_a, "signal_position"] = (
            self.sc.mode_a_base_position * self.sc.mode_b_position_factor
        )

        return df

    def generate_signals_batch(
        self, scored_data: Dict[str, pd.DataFrame]
    ) -> Dict[str, pd.DataFrame]:
        """
        批量生成信号

        Args:
            scored_data: {symbol: DataFrame} 已计算评分的 ETF 数据

        Returns:
            {symbol: DataFrame} 添加了信号列的数据
        """
        result = {}
        for symbol, df in scored_data.items():
            if df is not None and not df.empty:
                result[symbol] = self.generate_signals(df)
            else:
                result[symbol] = df
        return result
