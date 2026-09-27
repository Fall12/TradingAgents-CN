"""
ETF 动量混合策略 V5.2 Final

核心改进 (vs V4.3):
- 入场: 动量排名替代评分系统 (0.4*ret_20 + 0.3*ret_60 + 0.3*ret_120)
- 过滤: 相对强度(RS)过滤 - 20日超额收益 > 0
- 市场: HS300 > MA250 (慢速过滤, 减少假信号)
- 退出: 保留V4.3退出系统 (ATR止损/MA20止损/MA10减仓/极端止盈/熔断)
- 池: 72只去重代表ETF (按板块选流动性最高1-2只)

回测结果 (2018-2026):
  全样本: 年化4.71% 回撤-17.93% 夏普0.28 Calmar0.26 交易425次
  样本外(2024-2026): 年化10.53% 回撤-11.94% 夏普0.61

vs V4.3原版:
  年化: -0.39% → +4.71% (+5.10%)
  夏普: -0.58 → +0.28 (+0.86)
  交易: 733 → 425 (-42%)
"""
import logging
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .etf_universe_dedup import REPRESENTATIVE_ETFS, SECTOR_MAPPING

logger = logging.getLogger(__name__)

# 低流动性/非股票类ETF排除
EXCLUDE_ETFS = {"511880", "511990", "510990", "515090", "515110", "512650", "159176", "159253"}
ETF_POOL = [s for s in REPRESENTATIVE_ETFS if s not in EXCLUDE_ETFS]


@dataclass(frozen=True)
class MomentumHybridConfig:
    """V5.2 策略配置"""
    # 入场
    top_n: int = 3                      # 持仓数量上限
    rebalance_days: int = 5             # 调仓周期(交易日)
    position_size: float = 0.15         # 单只仓位 15%
    max_position_pct: float = 0.45      # 最大总仓位 45%
    keep_buffer: int = 2                # 持仓保留缓冲(仍在top N+2内则不换)
    min_momentum: float = 0.0           # 最低动量阈值
    min_excess_20: float = 0.0          # 20日超额收益 > 0 (RS过滤)

    # 动量公式权重
    mom_w_20: float = 0.4               # 20日收益权重
    mom_w_60: float = 0.3               # 60日收益权重
    mom_w_120: float = 0.3              # 120日收益权重

    # 市场过滤
    market_filter_ma: str = "ma250"     # HS250 > MA250
    trend_confirm_ma: str = "ma60"      # ETF: close > MA60 & MA20 > MA60

    # 退出 - V4.3系统
    enable_atr_stop: bool = True
    atr_stop_mult: float = 3.5          # ATR 3.5倍止损 (V4.3原2.5, V5.2放宽)
    enable_time_stop: bool = False      # V5.2: 关闭时间止损 (实验证明无效)
    time_stop_days: int = 20
    time_stop_threshold: float = 1.1
    enable_ma_stop: bool = True         # MA20连续两日跌破 → 全平
    enable_tp1: bool = True             # MA10减仓50%
    tp1_reduce: float = 0.5
    enable_extreme_tp: bool = True      # 极端止盈
    extreme_profit: float = 0.30        # 累计收益 > 30%
    extreme_vol_ratio: float = 2.0      # 量比 > 2
    extreme_price_change: float = 0.01  # 当日涨幅 < 1%
    enable_trend_break: bool = True     # 额外: 跌破MA60 → 全平
    trend_break_ma: str = "ma60"
    enable_circuit_breaker: bool = True  # 账户级熔断
    cb_weekly_dd: float = 0.08          # 周回撤 > 8%
    cb_monthly_dd: float = 0.15         # 月回撤 > 15%
    cb_pause_days: int = 10             # 暂停开仓10交易日
    cb_recover_dd: float = 0.05         # 回撤 <= 5% 解除

    # 回测
    initial_capital: float = 1_000_000
    commission: float = 0.0003
    slippage: float = 0.001
    start_date: str = "2018-01-01"
    end_date: str = "2026-12-31"
    train_end: str = "2023-12-31"
    test_start: str = "2024-01-01"
    risk_free_rate: float = 0.02


class MomentumHybridStrategy:
    """ETF 动量混合策略 V5.2"""

    def __init__(self, config: MomentumHybridConfig = None):
        self.config = config or MomentumHybridConfig()

    def compute_indicators(self, df: pd.DataFrame, hs300_df: pd.DataFrame) -> pd.DataFrame:
        """计算技术指标和动量分数"""
        df = df.copy()
        df = df.sort_values("trade_date").reset_index(drop=True)

        # 均线
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()
        df["ma120"] = df["close"].rolling(120).mean()

        # ATR
        df["tr"] = np.maximum(
            df["high"] - df["low"],
            np.maximum(
                abs(df["high"] - df["close"].shift(1)),
                abs(df["low"] - df["close"].shift(1))
            )
        )
        df["atr14"] = df["tr"].rolling(14).mean()

        # 量比
        df["vol_ma5"] = df["volume"].rolling(5).mean()
        df["volume_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)

        # 收益率
        df["return_1"] = df["close"].pct_change(1)
        df["return_20"] = df["close"].pct_change(20)
        df["return_60"] = df["close"].pct_change(60)
        df["return_120"] = df["close"].pct_change(120)

        # 动量综合分
        c = self.config
        df["momentum"] = (
            c.mom_w_20 * df["return_20"] +
            c.mom_w_60 * df["return_60"] +
            c.mom_w_120 * df["return_120"]
        )

        # 超额收益(vs HS300)
        hs300_ret20 = hs300_df.set_index("trade_date")["close"].pct_change(20)
        df["excess_20"] = df["return_20"] - df["trade_date"].map(hs300_ret20).fillna(0)

        # 趋势确认
        trend_ma = c.trend_confirm_ma
        df["trend_ok"] = (df["close"] > df[trend_ma]) & (df["ma20"] > df[trend_ma])

        return df

    def get_entry_candidates(
        self, etf_data: Dict[str, pd.DataFrame], current_date, hs300_df: pd.DataFrame
    ) -> List[Tuple[str, float, float, float]]:
        """
        获取当日入场候选

        Returns:
            [(symbol, momentum, atr14, close), ...] 按动量降序
        """
        c = self.config
        candidates = []

        for symbol, df in etf_data.items():
            row = df[df["trade_date"] == current_date]
            if row.empty:
                continue
            row = row.iloc[0]

            mom = row.get("momentum", -999)
            trend = row.get("trend_ok", False)
            excess = row.get("excess_20", 0)

            if trend and mom > c.min_momentum and excess > c.min_excess_20:
                candidates.append((
                    symbol, mom,
                    row.get("atr14", 0),
                    row.get("close", 0)
                ))

        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates

    def check_market_filter(self, hs300_df: pd.DataFrame, current_date) -> bool:
        """市场过滤: HS300 > MA(market_filter_ma)"""
        c = self.config
        row = hs300_df[hs300_df["trade_date"] == current_date]
        if row.empty:
            return False
        hs300_close = row.iloc[0]["close"]
        market_ma = row.iloc[0].get(c.market_filter_ma, 0)
        return market_ma > 0 and hs300_close > market_ma
