"""
技术指标计算模块

实现策略所需全部技术指标：
MA / ATR / High_N / Position / 量比 / VolTrend / RS / RS斜率 /
收益率 / 涨跌量比 / 突破检测 / 价格效率

所有计算向量化（pandas/numpy），输入 DataFrame 需含:
  trade_date, open, high, low, close, volume, amount
"""
import numpy as np
import pandas as pd
from typing import Optional


class IndicatorCalculator:
    """技术指标计算器"""

    # ---- 均线 ----

    @staticmethod
    def ma(series: pd.Series, window: int) -> pd.Series:
        """简单移动平均"""
        return series.rolling(window=window, min_periods=window).mean()

    @staticmethod
    def ma_ewm(series: pd.Series, span: int) -> pd.Series:
        """指数移动平均（备用）"""
        return series.ewm(span=span, adjust=False).mean()

    def add_moving_averages(self, df: pd.DataFrame) -> pd.DataFrame:
        """添加 MA5 / MA10 / MA20 / MA60 / MA120 / MA250"""
        for w in (5, 10, 20, 60, 120, 250):
            df[f"ma{w}"] = self.ma(df["close"], w)
        return df

    # ---- ATR ----

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        Average True Range (ATR)

        TR = max(high - low, |high - prev_close|, |low - prev_close|)
        ATR = SMA(TR, period)  -- Wilder 法等价于 SMA 首项后 EWM
        """
        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)

        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        # Wilder 平滑：首项 SMA，后续 EWM(alpha=1/period)
        atr_series = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        return atr_series

    def add_atr(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        df[f"atr{period}"] = self.atr(df, period)
        df[f"atr{period}_pct"] = df[f"atr{period}"] / df["close"]
        return df

    # ---- High_N（不含当日）----

    @staticmethod
    def high_n(df: pd.DataFrame, n: int) -> pd.Series:
        """
        High_N(t) = max(Close[t-N : t-1])  # 不包含当天 t

        用 close 的 rolling(n) 再 shift(1) 实现。
        """
        return df["close"].rolling(window=n, min_periods=n).max().shift(1)

    def add_high_n(self, df: pd.DataFrame) -> pd.DataFrame:
        """添加 high_20 / high_60 / high_120"""
        for n in (20, 60, 120):
            df[f"high_{n}"] = self.high_n(df, n)
        return df

    # ---- 位置系数 ----

    @staticmethod
    def position_coefficient(df: pd.DataFrame) -> pd.Series:
        """
        Position(t) = Close[t] / High_120(t)

        创新高时 Position > 1（因 High_120 不含当日）。
        """
        return df["close"] / df["high_120"]

    def add_position(self, df: pd.DataFrame) -> pd.DataFrame:
        df["position"] = self.position_coefficient(df)
        return df

    # ---- 突破检测 ----

    @staticmethod
    def is_breakout(df: pd.DataFrame, n: int) -> pd.Series:
        """
        突破 N 日高点: Close[t] > High_N(t)

        返回 bool 序列。
        """
        return df["close"] > df[f"high_{n}"]

    def add_breakouts(self, df: pd.DataFrame) -> pd.DataFrame:
        """添加 breakout_20 / breakout_60 / breakout_120（bool）"""
        for n in (20, 60, 120):
            df[f"breakout_{n}"] = self.is_breakout(df, n)
        return df

    @staticmethod
    def confirmed_breakout(df: pd.DataFrame, n: int, confirm_days: int = 2) -> pd.Series:
        """
        连续确认突破：最近 confirm_days 日均突破 N 日高点。

        20 日突破当日即可（confirm_days=1）；
        60/120 日需连续两日（confirm_days=2）。
        """
        flag = df[f"breakout_{n}"]
        if confirm_days <= 1:
            return flag
        # 连续 confirm_days 日为 True
        result = flag.copy()
        for i in range(1, confirm_days):
            result = result & flag.shift(i)
        return result

    # ---- 成交量指标 ----

    def add_volume_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """添加成交量相关指标"""
        # 成交量均线
        for w in (5, 20, 60):
            df[f"vol_ma{w}"] = self.ma(df["volume"], w)

        # 量比 = Volume / MA(Volume, 20)
        df["volume_ratio"] = df["volume"] / df["vol_ma20"]

        # 量能趋势 = MA(Volume, 5) / MA(Volume, 60)
        df["vol_trend"] = df["vol_ma5"] / df["vol_ma60"]

        # 连续放量天数：自当日起连续 Volume > MA(Volume, 20) 的天数
        vol_above = (df["volume"] > df["vol_ma20"]).astype(int)
        # 反向累计：从当前位置往前数连续 True 的个数
        df["consecutive_vol_days"] = 0
        cum = 0
        for i in range(len(df)):
            if vol_above.iloc[i] == 1:
                cum += 1
            else:
                cum = 0
            df.iloc[i, df.columns.get_loc("consecutive_vol_days")] = min(cum, 3)

        return df

    # ---- 收益率 ----

    @staticmethod
    def returns(df: pd.DataFrame, days: int) -> pd.Series:
        """N 日收益率: Close[t] / Close[t-days] - 1"""
        return df["close"] / df["close"].shift(days) - 1

    def add_returns(self, df: pd.DataFrame) -> pd.DataFrame:
        """添加 return_1 / return_10 / return_20"""
        for d in (1, 10, 20):
            df[f"return_{d}"] = self.returns(df, d)
        return df

    # ---- 涨跌量比 ----

    @staticmethod
    def up_down_volume_ratio(df: pd.DataFrame, window: int = 10) -> pd.Series:
        """
        涨跌量比（10 日）

        UpDownVolRatio = mean(Vol | 上涨日) / mean(Vol | 下跌日)

        上涨: Close > 昨收；下跌: Close < 昨收；平盘不计。
        分母为 0 → NaN（后续评分取 0）。
        """
        direction = np.sign(df["close"].diff())
        up_mask = direction > 0
        down_mask = direction < 0

        # 滚动窗口内上涨日/下跌日的平均成交量
        up_vol = (df["volume"] * up_mask).rolling(window=window, min_periods=1).sum()
        up_count = up_mask.rolling(window=window, min_periods=1).sum()
        down_vol = (df["volume"] * down_mask).rolling(window=window, min_periods=1).sum()
        down_count = down_mask.rolling(window=window, min_periods=1).sum()

        up_mean = up_vol / up_count.replace(0, np.nan)
        down_mean = down_vol / down_count.replace(0, np.nan)

        ratio = up_mean / down_mean
        return ratio

    def add_up_down_vol_ratio(self, df: pd.DataFrame, window: int = 10) -> pd.DataFrame:
        df["up_down_vol_ratio"] = self.up_down_volume_ratio(df, window)
        return df

    # ---- 价格效率 ----

    @staticmethod
    def price_efficiency(df: pd.DataFrame, window: int = 10, atr_period: int = 14) -> pd.Series:
        """
        价格效率 = Return_10 / ATR%_10

        ATR%_10 = mean(ATR14 / Close) over 10 days
        Return_10 带符号。
        """
        return_10 = df["close"] / df["close"].shift(window) - 1
        atr_pct = df[f"atr{atr_period}"] / df["close"]
        atr_pct_mean = atr_pct.rolling(window=window, min_periods=1).mean()

        efficiency = return_10 / atr_pct_mean.replace(0, np.nan)
        return efficiency

    def add_price_efficiency(self, df: pd.DataFrame) -> pd.DataFrame:
        df["price_efficiency"] = self.price_efficiency(df)
        return df

    # ---- 相对强度 RS ----

    @staticmethod
    def relative_strength(etf_close: pd.Series, hs300_close: pd.Series) -> pd.Series:
        """
        RS[t] = Close_ETF[t] / Close_HS300[t]

        需要两个序列按日期对齐。
        """
        # 对齐索引
        aligned = pd.concat([etf_close, hs300_close], axis=1, join="inner")
        aligned.columns = ["etf", "hs300"]
        return aligned["etf"] / aligned["hs300"]

    @staticmethod
    def rs_slope(rs: pd.Series, lookback: int = 5) -> pd.Series:
        """
        对过去 lookback 日 RS 线性回归，返回斜率。

        斜率 > 0 → +4 分。
        """
        slopes = pd.Series(np.nan, index=rs.index)

        for i in range(lookback - 1, len(rs)):
            window = rs.iloc[i - lookback + 1: i + 1].dropna()
            if len(window) < lookback:
                continue
            x = np.arange(lookback, dtype=float)
            y = window.values
            # 最小二乘斜率: slope = cov(x,y) / var(x)
            x_mean = x.mean()
            y_mean = y.mean()
            cov = np.sum((x - x_mean) * (y - y_mean))
            var = np.sum((x - x_mean) ** 2)
            if var != 0:
                slopes.iloc[i] = cov / var

        return slopes

    def add_relative_strength(
        self, df: pd.DataFrame, hs300_df: pd.DataFrame, lookback: int = 5
    ) -> pd.DataFrame:
        """
        添加 RS 相关指标到 ETF DataFrame。

        需要沪深 300 的收盘价序列（已按 trade_date 排序）。
        """
        # 对齐日期
        hs300_close = hs300_df.set_index("trade_date")["close"]
        etf_close = df.set_index("trade_date")["close"]

        rs = self.relative_strength(etf_close, hs300_close)
        rs = rs.reindex(df.set_index("trade_date").index)

        df["rs"] = rs.values
        df["rs_slope"] = self.rs_slope(rs, lookback).values

        # 20 日超额收益
        etf_ret_20 = etf_close / etf_close.shift(20) - 1
        hs300_ret_20 = hs300_close / hs300_close.shift(20) - 1
        excess_20 = etf_ret_20 - hs300_ret_20
        df["excess_20"] = excess_20.reindex(df.set_index("trade_date").index).values

        return df

    # ---- 波动风险 ----

    @staticmethod
    def volatility_risk(df: pd.DataFrame, atr_period: int = 14) -> pd.Series:
        """VolRisk = ATR14 / Close"""
        return df[f"atr{atr_period}"] / df["close"]

    def add_volatility_risk(self, df: pd.DataFrame) -> pd.DataFrame:
        df["vol_risk"] = self.volatility_risk(df)
        return df

    # ---- 综合计算 ----

    def compute_all(
        self, df: pd.DataFrame, hs300_df: Optional[pd.DataFrame] = None
    ) -> pd.DataFrame:
        """
        一次性计算全部指标。

        Args:
            df: ETF 日线数据 (trade_date, open, high, low, close, volume, amount)
            hs300_df: 沪深 300 日线数据（用于 RS 计算）

        Returns:
            添加了全部指标列的 DataFrame
        """
        df = df.copy()
        df = df.sort_values("trade_date").reset_index(drop=True)

        # 基础指标
        df = self.add_moving_averages(df)
        df = self.add_atr(df, period=14)
        df = self.add_high_n(df)
        df = self.add_position(df)
        df = self.add_breakouts(df)
        df = self.add_volume_indicators(df)
        df = self.add_returns(df)
        df = self.add_up_down_vol_ratio(df, window=10)
        df = self.add_price_efficiency(df)
        df = self.add_volatility_risk(df)

        # 相对强度（需要沪深 300 数据）
        if hs300_df is not None and not hs300_df.empty:
            df = self.add_relative_strength(df, hs300_df, lookback=5)

        return df
