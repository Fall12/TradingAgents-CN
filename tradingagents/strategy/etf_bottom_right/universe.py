"""
ETF 候选池构建模块 (§3)

每日交易前重建。须同时满足：
- 上市时间 >= 1 年
- 过去 20 个交易日日均成交额 >= 5000 万元
- 剔除货币/债券/商品/杠杆/反向 ETF
"""
import logging
import pandas as pd
import numpy as np
from typing import List, Set, Dict, Optional
from datetime import datetime, timedelta

from .config import StrategyConfig

logger = logging.getLogger(__name__)


class UniverseBuilder:
    """ETF 候选池构建器"""

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        self.uc = self.config.universe

    def filter_by_list_days(
        self, basic_info: pd.DataFrame, current_date: str
    ) -> pd.DataFrame:
        """
        过滤上市时间 >= 1 年

        Args:
            basic_info: 含 symbol, name, list_date 列
            current_date: 当前日期 YYYY-MM-DD
        """
        if "list_date" not in basic_info.columns:
            logger.warning("basic_info 缺少 list_date 列，跳过上市时间过滤")
            return basic_info

        cutoff = pd.Timestamp(current_date) - pd.Timedelta(days=365)
        # list_date 格式可能是 YYYYMMDD 或 YYYY-MM-DD
        list_dates = pd.to_datetime(
            basic_info["list_date"], format="mixed", errors="coerce"
        )
        mask = list_dates <= cutoff
        filtered = basic_info[mask].copy()
        logger.info(f"上市时间过滤: {len(basic_info)} -> {len(filtered)}")
        return filtered

    def filter_by_turnover(
        self, daily_quotes: Dict[str, pd.DataFrame], current_date: str
    ) -> Set[str]:
        """
        过滤过去 20 日日均成交额 >= 5000 万元

        Args:
            daily_quotes: {symbol: DataFrame} 每只 ETF 的日线数据
            current_date: 当前日期

        Returns:
            符合流动性要求的 symbol 集合
        """
        cutoff = pd.Timestamp(current_date)
        lookback = cutoff - pd.Timedelta(days=40)  # 多取一些日历日确保有 20 个交易日

        qualified = set()
        for symbol, df in daily_quotes.items():
            if df is None or df.empty:
                continue
            df_sorted = df.sort_values("trade_date")
            # 筛选最近 20 个交易日
            recent = df_sorted[df_sorted["trade_date"] <= cutoff].tail(20)
            if len(recent) < 20:
                continue
            avg_amount = recent["amount"].mean()
            if avg_amount >= self.uc.min_turnover_20d:
                qualified.add(symbol)

        logger.info(f"流动性过滤: {len(daily_quotes)} -> {len(qualified)} 只合格")
        return qualified

    def filter_by_type(self, basic_info: pd.DataFrame) -> pd.DataFrame:
        """
        剔除货币/债券/商品/杠杆/反向 ETF

        通过基金简称中的关键字判断。
        """
        if "name" not in basic_info.columns:
            logger.warning("basic_info 缺少 name 列，跳过类型过滤")
            return basic_info

        mask = pd.Series(True, index=basic_info.index)
        for kw in self.uc.exclude_keywords:
            mask &= ~basic_info["name"].str.contains(kw, na=False)

        filtered = basic_info[mask].copy()
        logger.info(f"类型过滤: {len(basic_info)} -> {len(filtered)}")
        return filtered

    def get_theme(self, symbol: str) -> str:
        """获取 ETF 所属主题"""
        return self.config.theme_mapping.get(symbol, "其他")

    def build(
        self,
        basic_info: pd.DataFrame,
        daily_quotes: Dict[str, pd.DataFrame],
        current_date: str,
    ) -> List[str]:
        """
        构建 ETF 候选池

        Args:
            basic_info: ETF 基础信息 (symbol, name, list_date)
            daily_quotes: {symbol: DataFrame} 日线数据
            current_date: 当前日期 YYYY-MM-DD

        Returns:
            合格 ETF 代码列表
        """
        logger.info(f"开始构建候选池，日期={current_date}")

        # 1. 上市时间过滤
        filtered = self.filter_by_list_days(basic_info, current_date)

        # 2. 类型过滤
        filtered = self.filter_by_type(filtered)

        # 3. 流动性过滤
        # 只检查通过前两步的 ETF
        candidate_symbols = set(filtered["symbol"].tolist()) if "symbol" in filtered.columns else set()
        candidate_quotes = {
            s: daily_quotes.get(s) for s in candidate_symbols
            if daily_quotes.get(s) is not None
        }
        liquid_symbols = self.filter_by_turnover(candidate_quotes, current_date)

        # 4. 交集
        final_symbols = [s for s in candidate_symbols if s in liquid_symbols]

        logger.info(f"候选池构建完成: {len(final_symbols)} 只 ETF")
        return sorted(final_symbols)

    def build_daily(
        self,
        all_basic_info: pd.DataFrame,
        all_quotes: Dict[str, pd.DataFrame],
        trade_dates: List[str],
    ) -> Dict[str, List[str]]:
        """
        批量构建每日候选池

        Args:
            all_basic_info: 全部 ETF 基础信息
            all_quotes: {symbol: DataFrame} 全部日线数据
            trade_dates: 交易日列表

        Returns:
            {trade_date: [symbol, ...]} 每日候选池
        """
        result = {}
        for date in trade_dates:
            # 按日期过滤基础信息（只保留该日期前已上市的）
            pool = self.build(all_basic_info, all_quotes, date)
            result[date] = pool
        return result
