#!/usr/bin/env python3
"""
V11 个股动量+回调策略 — 核心引擎
实现入场/退出/风控逻辑, 可用于实盘信号扫描和回测

使用方式:
  from tradingagents.strategy.stock_momentum.v11_strategy import V11Strategy
  strategy = V11Strategy()
  signals = strategy.scan(current_date)  # 扫描信号
"""
import sys
import os
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

from .v11_config import STRATEGY_CONFIG


class SignalType(Enum):
    BUY = "买入"
    SELL = "卖出"
    HOLD = "持有"
    WAIT = "观望"


@dataclass
class StockSignal:
    """个股信号"""
    symbol: str
    name: str
    signal: SignalType
    close: float
    score: float = 0.0
    reason: str = ""
    atr14: float = 0.0
    trailing_stop: float = 0.0
    hard_stop: float = 0.0
    details: Dict = field(default_factory=dict)


class V11Strategy:
    """V11 个股动量+回调策略"""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or STRATEGY_CONFIG
        self.entry_cfg = self.config["entry"]
        self.exit_cfg = self.config["exit"]
        self.risk_cfg = self.config["risk"]
        self.rebal_cfg = self.config["rebalance"]
        self.score_cfg = self.config["scoring"]
        self.cost_cfg = self.config["costs"]

    # ==================== 指标计算 ====================
    @staticmethod
    def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """计算所有技术指标"""
        df = df.copy()
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()
        df["ma120"] = df["close"].rolling(120).mean()
        df["ma250"] = df["close"].rolling(250).mean()

        # ATR
        df["tr"] = np.maximum(
            df["high"] - df["low"],
            np.maximum(
                abs(df["high"] - df["close"].shift(1)),
                abs(df["low"] - df["close"].shift(1))
            )
        )
        df["atr14"] = df["tr"].rolling(14).mean()
        df["atr_pct"] = df["atr14"] / df["close"]

        # 动量
        df["return_5"] = df["close"].pct_change(5)
        df["return_20"] = df["close"].pct_change(20)
        df["return_60"] = df["close"].pct_change(60)
        df["return_120"] = df["close"].pct_change(120)
        df["mom_fast"] = 0.6 * df["return_20"] + 0.4 * df["return_60"]
        df["mom_mid"] = 0.5 * df["return_60"] + 0.5 * df["return_120"]

        # 量比
        df["vol_ma5"] = df["volume"].rolling(5).mean()
        df["volume_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)

        # 偏离度
        df["dist_ma20"] = df["close"] / df["ma20"] - 1
        df["dist_ma60"] = df["close"] / df["ma60"] - 1

        # 回撤
        df["roll_high_20"] = df["close"].rolling(20).max()
        df["drawdown_20"] = df["close"] / df["roll_high_20"] - 1

        # RSI
        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        df["rsi14"] = 100 - (100 / (1 + rs))

        # 流动性
        df["amount"] = df["amount"].astype(float) if "amount" in df.columns else df["close"] * df["volume"]
        df["avg_amount_20"] = df["amount"].rolling(20).mean()

        return df

    # ==================== 入场逻辑 ====================
    def check_entry(self, r: pd.Series, hs300_ret20: float) -> Tuple[bool, float, str]:
        """
        检查入场条件
        返回: (是否通过, 评分, 原因)
        """
        close = float(r["close"])
        volume = float(r["volume"])
        amount = float(r.get("amount", 0))

        # 基础过滤
        if volume == 0 or amount == 0:
            return False, 0, "停牌"

        # 流动性
        avg_amount_20 = float(r["avg_amount_20"]) if not np.isnan(r["avg_amount_20"]) else 0
        if avg_amount_20 < self.risk_cfg["min_liquidity"]:
            return False, 0, "低流动性"

        # 趋势确认
        ma60 = float(r["ma60"]) if not np.isnan(r["ma60"]) else 0
        ma250 = float(r["ma250"]) if not np.isnan(r["ma250"]) else 0
        if ma60 == 0 or ma250 == 0:
            return False, 0, "数据不足"
        if not (close > ma60 and ma60 > ma250):
            return False, 0, "趋势未确认"

        # 波动率过滤
        atr_pct = float(r["atr_pct"]) if not np.isnan(r["atr_pct"]) else 1
        if atr_pct > self.risk_cfg["atr_pct_max"]:
            return False, 0, "波动率过高"

        atr14 = float(r["atr14"]) if not np.isnan(r["atr14"]) else 0
        if atr14 == 0:
            return False, 0, "ATR为零"

        # 动量
        mom_mid = float(r["mom_mid"]) if not np.isnan(r["mom_mid"]) else 0
        if mom_mid < self.entry_cfg["mom_mid_threshold"]:
            return False, 0, "动量不足"

        # 回调择时
        dist_ma20 = float(r["dist_ma20"]) if not np.isnan(r["dist_ma20"]) else 0
        dist_ma60 = float(r["dist_ma60"]) if not np.isnan(r["dist_ma60"]) else 0
        if dist_ma20 > self.entry_cfg["dist_ma20_max"]:
            return False, 0, "偏离MA20过大"
        if dist_ma60 > self.entry_cfg["dist_ma60_max"]:
            return False, 0, "偏离MA60过大"

        ret_5 = float(r["return_5"]) if not np.isnan(r["return_5"]) else 0
        if ret_5 > self.entry_cfg["ret_5_max"]:
            return False, 0, "5日涨幅过大"
        if ret_5 < self.entry_cfg["ret_5_min"]:
            return False, 0, "5日跌幅过大"

        # RS过滤
        ret_20 = float(r["return_20"]) if not np.isnan(r["return_20"]) else 0
        excess_20 = ret_20 - hs300_ret20
        if self.entry_cfg["excess_20_required"] and excess_20 <= 0:
            return False, 0, "未跑赢大盘"

        # RSI
        rsi14 = float(r["rsi14"]) if not np.isnan(r["rsi14"]) else 50
        if rsi14 > self.entry_cfg["rsi_max"]:
            return False, 0, "RSI超买"

        # 评分
        drawdown_20 = float(r["drawdown_20"]) if not np.isnan(r["drawdown_20"]) else 0
        vol_ratio = float(r["volume_ratio"]) if not np.isnan(r["volume_ratio"]) else 1

        mom_score = min(mom_mid * 100, self.score_cfg["mom_score_cap"])
        pullback_score = max(0, -drawdown_20 * 100)
        vol_score = min(vol_ratio * 10, self.score_cfg["vol_score_cap"])
        score = mom_score + pullback_score + vol_score

        return True, score, "通过"

    # ==================== 退出逻辑 ====================
    def check_exit(self, pos: Dict, r: pd.Series, prev_r: Optional[pd.Series] = None) -> Tuple[bool, str]:
        """
        检查退出条件
        返回: (是否退出, 原因)
        """
        close = float(r["close"])
        volume = float(r["volume"])
        if volume == 0:
            return False, ""

        entry_price = pos["entry_price"]
        cum_ret = (close / entry_price - 1) if entry_price > 0 else 0

        # 1. 硬止损
        if cum_ret <= -self.exit_cfg["hard_stop_loss"]:
            return True, f"硬止损{self.exit_cfg['hard_stop_loss']:.0%}"

        # 2. Trailing Stop
        trailing_stop = pos.get("trailing_stop", 0)
        if trailing_stop > 0 and close <= trailing_stop:
            return True, "Trailing止损"

        # 3. MA120连续跌破
        trend_ma = self.exit_cfg["trend_exit_ma"]
        ma_val = float(r[trend_ma]) if not np.isnan(r[trend_ma]) else 0
        if ma_val > 0 and prev_r is not None:
            prev_ma = float(prev_r[trend_ma]) if not np.isnan(prev_r[trend_ma]) else 0
            if prev_ma > 0:
                prev_close = float(prev_r["close"])
                if close < ma_val and prev_close < prev_ma:
                    return True, f"{trend_ma.upper()}止损"

        # 4. 极端止盈
        vol_ratio = float(r["volume_ratio"]) if not np.isnan(r["volume_ratio"]) else 1
        loc_idx = r.name if hasattr(r, "name") else 0
        daily_ret = 0
        if prev_r is not None:
            prev_close = float(prev_r["close"])
            daily_ret = (close / prev_close - 1) if prev_close > 0 else 0

        if (cum_ret > self.exit_cfg["extreme_profit"] and
            vol_ratio > self.exit_cfg["extreme_vol_ratio"] and
            abs(daily_ret) < 0.01):
            return True, "极端止盈"

        # 5. 趋势破坏: 跌破MA120
        ma120 = float(r["ma120"]) if not np.isnan(r["ma120"]) else 0
        if ma120 > 0 and close < ma120:
            return True, "趋势破坏"

        return False, ""

    # ==================== 信号扫描 ====================
    def scan(self, stock_data: Dict[str, pd.DataFrame], hs300_df: pd.DataFrame,
             current_date: pd.Timestamp, positions: Dict[str, Dict] = None,
             market_ok: bool = True) -> Tuple[List[StockSignal], List[StockSignal]]:
        """
        扫描买入和卖出信号
        返回: (buy_signals, sell_signals)
        """
        positions = positions or {}
        buy_signals = []
        sell_signals = []

        if current_date not in hs300_df.set_index("trade_date").index:
            return buy_signals, sell_signals

        hs300_row = hs300_df[hs300_df["trade_date"] == current_date].iloc[0]
        hs300_ret20 = hs300_df.set_index("trade_date")["close"].pct_change(20).get(current_date, 0)
        if np.isnan(hs300_ret20):
            hs300_ret20 = 0

        # 检查持仓退出
        for sym, pos in positions.items():
            if sym not in stock_data or current_date not in stock_data[sym].index:
                continue
            df = stock_data[sym]
            r = df.loc[current_date]
            loc_idx = df.index.get_loc(current_date)
            prev_r = df.iloc[loc_idx - 1] if loc_idx >= 1 else None

            should_exit, reason = self.check_exit(pos, r, prev_r)
            if should_exit:
                sell_signals.append(StockSignal(
                    symbol=sym, name=pos.get("name", sym),
                    signal=SignalType.SELL, close=float(r["close"]),
                    reason=reason,
                    details={"entry_price": pos["entry_price"], "cum_return": float(r["close"]) / pos["entry_price"] - 1}
                ))

        # 如果市场过滤未通过, 所有持仓发出卖出信号
        if not market_ok:
            for sym, pos in positions.items():
                if sym in stock_data and current_date in stock_data[sym].index:
                    r = stock_data[sym].loc[current_date]
                    sell_signals.append(StockSignal(
                        symbol=sym, name=pos.get("name", sym),
                        signal=SignalType.SELL, close=float(r["close"]),
                        reason="市场清仓"
                    ))
            return buy_signals, sell_signals

        # 扫描买入候选
        candidates = []
        for sym, df in stock_data.items():
            if current_date not in df.index:
                continue
            r = df.loc[current_date]
            passed, score, reason = self.check_entry(r, hs300_ret20)
            if passed:
                atr14 = float(r["atr14"])
                close = float(r["close"])
                candidates.append(StockSignal(
                    symbol=sym,
                    name=df.attrs.get("name", sym),
                    signal=SignalType.BUY,
                    close=close,
                    score=score,
                    reason=reason,
                    atr14=atr14,
                    trailing_stop=close - self.exit_cfg["trailing_atr_mult"] * atr14,
                    hard_stop=close * (1 - self.exit_cfg["hard_stop_loss"]),
                    details={
                        "mom_mid": float(r["mom_mid"]),
                        "ret_5": float(r["return_5"]) if not np.isnan(r["return_5"]) else 0,
                        "ret_20": float(r["return_20"]) if not np.isnan(r["return_20"]) else 0,
                        "excess_20": float(r["return_20"]) - hs300_ret20 if not np.isnan(r["return_20"]) else 0,
                        "dist_ma20": float(r["dist_ma20"]) if not np.isnan(r["dist_ma20"]) else 0,
                        "rsi14": float(r["rsi14"]) if not np.isnan(r["rsi14"]) else 50,
                        "atr_pct": float(r["atr_pct"]) if not np.isnan(r["atr_pct"]) else 0,
                        "vol_ratio": float(r["volume_ratio"]) if not np.isnan(r["volume_ratio"]) else 1,
                    }
                ))

        # 排序, 取Top N
        candidates.sort(key=lambda x: x.score, reverse=True)
        buy_signals = candidates[:self.rebal_cfg["top_n_entry"]]

        return buy_signals, sell_signals

    # ==================== 市场过滤 ====================
    def check_market(self, hs300_df: pd.DataFrame, current_date: pd.Timestamp) -> bool:
        """检查市场过滤条件"""
        row = hs300_df[hs300_df["trade_date"] == current_date]
        if row.empty:
            return False
        market_close = float(row.iloc[0]["close"])
        market_ma = float(row.iloc[0][self.risk_cfg["market_filter_ma"]])
        if np.isnan(market_ma):
            return False
        return market_close > market_ma
