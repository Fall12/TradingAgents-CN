"""
市场环境模型 (§13)

基准：沪深 300。切换须连续 3 日确认。

| 状态 | 条件 | 系数 |
| Bull | Close > MA250 且 MA60 > MA120 且 MA60 向上 | 100% |
| Recovery | Close > MA60 且 Close <= MA250 且 MA60 向上 | 50% |
| Bear | Close < MA250 且 MA60 < MA120 | 20% |
| Neutral | 非上三者，且近均线/均线收敛 | 30% |

优先级 (§2.6): Bull > Bear > Recovery > Neutral
先命中先生效；状态切换另需连续 3 日确认（确认期内沿用旧状态系数）。
"""
import logging
import numpy as np
import pandas as pd
from typing import Optional

from .config import StrategyConfig
from .indicators import IndicatorCalculator

logger = logging.getLogger(__name__)


class RegimeDetector:
    """市场环境检测器"""

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        self.rc = self.config.regime
        self.calc = IndicatorCalculator()

    def _detect_raw_state(self, df: pd.DataFrame) -> pd.Series:
        """
        检测每日原始市场状态（未经确认）

        优先级: Bull > Bear > Recovery > Neutral
        """
        close = df["close"]
        ma60 = df["ma60"]
        ma120 = df["ma120"]
        ma250 = df["ma250"]
        ma60_up = ma60 > ma60.shift(1)  # MA60 向上

        # 各状态条件
        is_bull = (close > ma250) & (ma60 > ma120) & ma60_up
        is_bear = (close < ma250) & (ma60 < ma120)
        is_recovery = (close > ma60) & (close <= ma250) & ma60_up

        # Neutral: 非上三者，且近均线或均线收敛
        near_ma250 = (close - ma250).abs() / ma250 <= self.rc.neutral_near_ma250
        ma_converge = (ma60 - ma120).abs() / ma120 < self.rc.neutral_ma_converge
        is_neutral = (~is_bull) & (~is_bear) & (~is_recovery) & (near_ma250 | ma_converge)

        # 按优先级赋值（先命中先生效）
        state = pd.Series("Unknown", index=df.index)
        state[is_neutral] = "Neutral"
        state[is_recovery] = "Recovery"
        state[is_bear] = "Bear"
        state[is_bull] = "Bull"

        # 未匹配的保持上一状态或 Neutral
        state = state.replace("Unknown", np.nan)
        state = state.fillna(method="ffill").fillna("Neutral")

        return state

    def detect(self, hs300_df: pd.DataFrame) -> pd.DataFrame:
        """
        检测市场环境状态（含 3 日确认）

        Args:
            hs300_df: 沪深 300 日线数据 (trade_date, open, high, low, close, volume, amount)

        Returns:
            添加了 regime / regime_confirmed / regime_coef 列的 DataFrame
        """
        df = hs300_df.copy()
        df = df.sort_values("trade_date").reset_index(drop=True)

        # 计算均线
        df = self.calc.add_moving_averages(df)

        # 原始状态
        raw_state = self._detect_raw_state(df)
        df["regime_raw"] = raw_state

        # 3 日确认逻辑
        confirmed = []
        prev_confirmed = "Neutral"  # 初始状态

        for i in range(len(df)):
            current_raw = raw_state.iloc[i]

            if current_raw == prev_confirmed:
                # 与当前确认状态一致 → 直接确认
                confirmed.append(current_raw)
                pending_state = None
                pending_count = 0
            else:
                # 状态变化，开始确认计数
                if i == 0 or not hasattr(self, '_pending_state'):
                    self._pending_state = None
                    self._pending_count = 0

                if self._pending_state != current_raw:
                    self._pending_state = current_raw
                    self._pending_count = 1
                else:
                    self._pending_count += 1

                if self._pending_count >= self.rc.confirm_days:
                    # 确认完成
                    confirmed.append(current_raw)
                    prev_confirmed = current_raw
                    self._pending_state = None
                    self._pending_count = 0
                else:
                    # 确认期内沿用旧状态
                    confirmed.append(prev_confirmed)

        # 清理临时属性
        if hasattr(self, '_pending_state'):
            del self._pending_state
            del self._pending_count

        df["regime"] = confirmed

        # 系数
        coef_map = self.rc.regime_coefficients
        df["regime_coef"] = df["regime"].map(coef_map).fillna(coef_map["Neutral"])

        return df

    def get_position_limits(self, regime: str) -> dict:
        """
        获取指定市场状态的仓位限制

        Returns:
            {"single": float, "total": float, "coef": float}
        """
        return self.rc.regime_position_limits.get(
            regime, self.rc.regime_position_limits["Neutral"]
        )

    def get_regime_for_date(
        self, regime_df: pd.DataFrame, trade_date: str
    ) -> str:
        """获取指定日期的市场状态"""
        row = regime_df[regime_df["trade_date"] == trade_date]
        if row.empty:
            # 找最近的日期
            mask = regime_df["trade_date"] <= trade_date
            row = regime_df[mask].tail(1)
        if row.empty:
            return "Neutral"
        return row.iloc[0]["regime"]
