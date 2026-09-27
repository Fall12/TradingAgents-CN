"""
ETF 评分引擎 (§4-11)

六大模块 + 扣分机制 → FinalScore（满分 100）

模块分值:
  趋势位置 20 | 趋势突破 20 | 相对强度 20 | 量价健康度 15 | 量能结构 15 | 波动风险 10

评分顺序 (§2.5):
  1. 六大模块原始分求和（满分 100）
  2. 应用扣分（累计最多 -20）
  3. FinalScore = max(0, min(100, 原始分 + 扣分))
  4. 一日游：昨日用已落库 FinalScore；今日用当日模块合计（扣分前）
"""
import logging
import numpy as np
import pandas as pd
from typing import Optional

from .config import StrategyConfig
from .indicators import IndicatorCalculator

logger = logging.getLogger(__name__)


class ScoringEngine:
    """ETF 评分引擎"""

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        self.calc = IndicatorCalculator()

    # ==================== §5 趋势位置（20 分）====================

    def _long_period_filter(self, df: pd.DataFrame) -> pd.Series:
        """
        长周期过滤（二选一即通过）

        条件 A: Close > MA250
        条件 B: 过去 10 个交易日，MA60[t] > MA60[t-1] 的天数 >= 8
        """
        cond_a = df["close"] > df["ma250"]

        ma60_diff = (df["ma60"] > df["ma60"].shift(1)).astype(int)
        ma60_up_count = ma60_diff.rolling(window=10, min_periods=10).sum()
        cond_b = ma60_up_count >= 8

        return cond_a | cond_b

    def score_trend_position(self, df: pd.DataFrame) -> pd.DataFrame:
        """趋势位置评分（20 分）"""
        cfg = self.config.position

        # 位置系数
        position = df["position"]

        # 长周期过滤
        long_filter = self._long_period_filter(df)

        # 基础分（按分档）
        base_score = pd.Series(0, index=df.index, dtype=float)
        for lower, upper, score in cfg.position_buckets:
            if upper is None:
                mask = position > lower
            elif lower is None:
                mask = position < upper
            else:
                mask = (position >= lower) & (position < upper)
            base_score[mask] = score

        # 不满足长周期过滤 → floor(基础分 × 0.5)
        adjusted = base_score.copy()
        fail_mask = ~long_filter
        adjusted[fail_mask] = np.floor(base_score[fail_mask] * cfg.long_filter_fail_factor)

        df["score_position"] = adjusted
        df["long_filter_passed"] = long_filter
        return df

    # ==================== §6 趋势突破（20 分）====================

    def score_trend_breakout(self, df: pd.DataFrame) -> pd.DataFrame:
        """趋势突破评分（20 分，取档不叠加）"""
        cfg = self.config.breakout

        # 确认突破
        breakout_20 = df["breakout_20"]  # 20日当日即可
        breakout_60 = self.calc.confirmed_breakout(df, 60, cfg.confirm_days_60)
        breakout_120 = self.calc.confirmed_breakout(df, 120, cfg.confirm_days_120)

        # 取最高档（不累加）
        score = pd.Series(0, index=df.index, dtype=float)

        # 按优先级从高到低
        score[breakout_120.fillna(False)] = cfg.breakout_scores["breakout_120"]
        score[breakout_60.fillna(False)] = np.where(
            breakout_60.fillna(False),
            cfg.breakout_scores["breakout_60"],
            score
        )
        # 已经是 120 的不动
        score_60 = np.where(
            (breakout_60.fillna(False)) & (~breakout_120.fillna(False)),
            cfg.breakout_scores["breakout_60"],
            0
        )
        score_20 = np.where(
            (breakout_20.fillna(False)) & (~breakout_60.fillna(False)) & (~breakout_120.fillna(False)),
            cfg.breakout_scores["breakout_20"],
            0
        )
        # 最终取最高
        final_score = pd.Series(0, index=df.index, dtype=float)
        final_score[breakout_120.fillna(False)] = cfg.breakout_scores["breakout_120"]
        remaining = ~breakout_120.fillna(False)
        final_score[remaining & breakout_60.fillna(False)] = cfg.breakout_scores["breakout_60"]
        remaining = remaining & ~breakout_60.fillna(False)
        final_score[remaining & breakout_20.fillna(False)] = cfg.breakout_scores["breakout_20"]

        # 额外条件 +2（可叠加，封顶 20）
        extra = (df["close"] > df["ma20"]) & (df["ma5"] > df["ma10"])
        final_score = final_score + np.where(extra.fillna(False), cfg.extra_condition_score, 0)
        final_score = final_score.clip(upper=cfg.max_score)

        df["score_breakout"] = final_score
        df["confirmed_breakout_60"] = breakout_60
        df["confirmed_breakout_120"] = breakout_120
        return df

    # ==================== §7 相对强度（20 分）====================

    def score_relative_strength(self, df: pd.DataFrame) -> pd.DataFrame:
        """相对强度评分（20 分）"""
        cfg = self.config.relative_strength

        # 20 日超额收益（最高 16）
        excess = df.get("excess_20", pd.Series(np.nan, index=df.index))
        excess_score = pd.Series(0, index=df.index, dtype=float)

        for lower, upper, score in cfg.excess_buckets:
            if upper is None:
                mask = excess >= lower
            elif lower is None:
                mask = excess < upper
            else:
                mask = (excess >= lower) & (excess < upper)
            excess_score[mask] = score

        # RS 斜率 > 0 → +4
        rs_slope = df.get("rs_slope", pd.Series(np.nan, index=df.index))
        rs_score = np.where(rs_slope > 0, cfg.rs_slope_score, 0)

        df["score_relative"] = excess_score + rs_score
        return df

    # ==================== §8 量价健康度（15 分）====================

    def score_volume_price(self, df: pd.DataFrame) -> pd.DataFrame:
        """量价健康度评分（15 分）"""
        cfg = self.config.volume_price

        # 涨跌量比（10 分）
        ratio = df.get("up_down_vol_ratio", pd.Series(np.nan, index=df.index))
        ratio = ratio.fillna(0)  # 分母为 0 → 0 分
        vol_score = pd.Series(0, index=df.index, dtype=float)

        for lower, upper, score in cfg.up_down_vol_buckets:
            if upper is None:
                mask = ratio >= lower
            elif lower is None:
                mask = ratio < upper
            else:
                mask = (ratio >= lower) & (ratio < upper)
            vol_score[mask] = score

        # 价格效率（5 分）
        eff = df.get("price_efficiency", pd.Series(np.nan, index=df.index))
        eff = eff.fillna(0)
        eff_score = pd.Series(0, index=df.index, dtype=float)

        for lower, upper, score in cfg.efficiency_buckets:
            if upper is None:
                mask = eff > lower
            elif lower is None:
                mask = eff <= upper
            else:
                # (0.3, 0.8] → [0.3, 0.8]
                mask = (eff >= lower) & (eff <= upper)
            eff_score[mask] = score

        df["score_volume_price"] = vol_score + eff_score
        return df

    # ==================== §9 量能结构（15 分）====================

    def score_volume_structure(self, df: pd.DataFrame) -> pd.DataFrame:
        """量能结构评分（15 分）"""
        cfg = self.config.volume_structure

        # 量能趋势（12 分）
        vol_trend = df.get("vol_trend", pd.Series(np.nan, index=df.index))
        vol_trend = vol_trend.fillna(0)
        trend_score = pd.Series(0, index=df.index, dtype=float)

        for lower, upper, score in cfg.vol_trend_buckets:
            if upper is None:
                mask = vol_trend >= lower
            elif lower is None:
                mask = vol_trend < upper
            else:
                mask = (vol_trend >= lower) & (vol_trend < upper)
            trend_score[mask] = score

        # 连续放量（3 分）
        consec = df.get("consecutive_vol_days", pd.Series(0, index=df.index))
        consec_score = consec.clip(upper=cfg.consecutive_vol_max).astype(float)

        df["score_volume_structure"] = trend_score + consec_score
        return df

    # ==================== §10 波动风险（10 分）====================

    def score_volatility_risk(self, df: pd.DataFrame) -> pd.DataFrame:
        """波动风险评分（10 分）"""
        cfg = self.config.volatility

        vol_risk = df.get("vol_risk", pd.Series(np.nan, index=df.index))
        vol_risk = vol_risk.fillna(1.0)  # 缺失按高风险处理
        score = pd.Series(0, index=df.index, dtype=float)

        for lower, upper, sc in cfg.vol_risk_buckets:
            if upper is None:
                mask = vol_risk >= lower
            elif lower is None:
                mask = vol_risk < upper
            else:
                mask = (vol_risk >= lower) & (vol_risk < upper)
            score[mask] = sc

        df["score_volatility"] = score
        return df

    # ==================== §11 扣分机制（最高扣 20）====================

    def compute_penalties(
        self, df: pd.DataFrame, prev_final_score: Optional[pd.Series] = None
    ) -> pd.DataFrame:
        """
        计算扣分项

        Args:
            df: 已计算六大模块得分的 DataFrame
            prev_final_score: 上一交易日的 FinalScore（用于一日游判断）
        """
        cfg = self.config.penalty
        penalty = pd.Series(0, index=df.index, dtype=float)

        # 11.1 高位加速（最多 -8）
        return_20 = df.get("return_20", pd.Series(0, index=df.index))
        high_accel = return_20 > cfg.high_accel_threshold
        if high_accel.any():
            steps = np.floor((return_20 - cfg.high_accel_threshold) / cfg.high_accel_step)
            accel_penalty = (steps * cfg.high_accel_per_step).clip(upper=cfg.high_accel_max)
            penalty -= accel_penalty.where(high_accel, 0)

        # 11.2 爆量滞涨（-6）
        volume_ratio = df.get("volume_ratio", pd.Series(1, index=df.index))
        return_1 = df.get("return_1", pd.Series(0, index=df.index))
        spike = (volume_ratio > cfg.volume_spike_ratio) & (return_1.abs() < cfg.volume_spike_price_threshold)
        penalty -= np.where(spike, cfg.volume_spike_penalty, 0)

        # 11.3 一日游（-6）
        # 昨日 FinalScore >= 70 且 今日模块合计（扣分前） < 50
        module_sum = (
            df["score_position"] + df["score_breakout"] + df["score_relative"] +
            df["score_volume_price"] + df["score_volume_structure"] + df["score_volatility"]
        )
        if prev_final_score is not None:
            # 对齐索引
            prev_aligned = prev_final_score.reindex(df.index)
            one_day = (prev_aligned >= cfg.one_day_wonder_prev_threshold) & (
                module_sum < cfg.one_day_wonder_today_threshold
            )
            penalty -= np.where(one_day.fillna(False), cfg.one_day_wonder_penalty, 0)

        # 截断为最多 -20
        penalty = penalty.clip(lower=-cfg.max_penalty)

        df["penalty"] = penalty
        df["module_sum"] = module_sum
        return df

    # ==================== 综合评分 ====================

    def compute_final_score(
        self, df: pd.DataFrame, prev_final_score: Optional[pd.Series] = None
    ) -> pd.DataFrame:
        """
        计算最终评分

        评分顺序 (§2.5):
        1. 六大模块原始分求和（满分 100）
        2. 应用扣分（累计最多 -20）
        3. FinalScore = max(0, min(100, 原始分 + 扣分))

        Args:
            df: 已计算全部指标的 DataFrame
            prev_final_score: 上一交易日的 FinalScore 序列
        """
        # 六大模块评分
        df = self.score_trend_position(df)
        df = self.score_trend_breakout(df)
        df = self.score_relative_strength(df)
        df = self.score_volume_price(df)
        df = self.score_volume_structure(df)
        df = self.score_volatility_risk(df)

        # 扣分
        df = self.compute_penalties(df, prev_final_score)

        # 最终评分
        df["final_score"] = (df["module_sum"] + df["penalty"]).clip(lower=0, upper=100)

        return df

    def compute_scores_series(
        self, df: pd.DataFrame, hs300_df: Optional[pd.DataFrame] = None
    ) -> pd.DataFrame:
        """
        对单只 ETF 的完整时间序列计算评分

        逐日处理一日游逻辑：昨日用已落库 FinalScore，今日用当日模块合计。

        Args:
            df: ETF 日线数据（含 OHLCV）
            hs300_df: 沪深 300 日线数据

        Returns:
            添加了全部评分列的 DataFrame
        """
        # 先计算全部指标
        df = self.calc.compute_all(df, hs300_df)

        # 逐日计算（因为一日游需要前一日 FinalScore）
        df = df.sort_values("trade_date").reset_index(drop=True)

        # 先批量计算六大模块（不依赖前日）
        df = self.score_trend_position(df)
        df = self.score_trend_breakout(df)
        df = self.score_relative_strength(df)
        df = self.score_volume_price(df)
        df = self.score_volume_structure(df)
        df = self.score_volatility_risk(df)

        module_sum = (
            df["score_position"] + df["score_breakout"] + df["score_relative"] +
            df["score_volume_price"] + df["score_volume_structure"] + df["score_volatility"]
        )
        df["module_sum"] = module_sum

        # 逐日计算扣分（一日游需要前一日 FinalScore）
        prev_final = 0.0
        final_scores = []
        penalties = []

        for i in range(len(df)):
            row = df.iloc[i]

            # 计算当日扣分
            daily_penalty = 0.0

            # 11.1 高位加速
            r20 = row.get("return_20", 0) or 0
            if pd.notna(r20) and r20 > self.config.penalty.high_accel_threshold:
                steps = int(np.floor((r20 - self.config.penalty.high_accel_threshold) / self.config.penalty.high_accel_step))
                daily_penalty -= min(steps * self.config.penalty.high_accel_per_step, self.config.penalty.high_accel_max)

            # 11.2 爆量滞涨
            vr = row.get("volume_ratio", 1) or 1
            r1 = row.get("return_1", 0) or 0
            if pd.notna(vr) and pd.notna(r1) and vr > self.config.penalty.volume_spike_ratio and abs(r1) < self.config.penalty.volume_spike_price_threshold:
                daily_penalty -= self.config.penalty.volume_spike_penalty

            # 11.3 一日游
            if prev_final >= self.config.penalty.one_day_wonder_prev_threshold:
                today_sum = row.get("module_sum", 0) or 0
                if today_sum < self.config.penalty.one_day_wonder_today_threshold:
                    daily_penalty -= self.config.penalty.one_day_wonder_penalty

            daily_penalty = max(daily_penalty, -self.config.penalty.max_penalty)
            penalties.append(daily_penalty)

            final = max(0, min(100, row.get("module_sum", 0) + daily_penalty))
            final_scores.append(final)
            prev_final = final

        df["penalty"] = penalties
        df["final_score"] = final_scores

        return df
