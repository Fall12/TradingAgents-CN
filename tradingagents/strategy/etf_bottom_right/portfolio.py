"""
仓位管理与组合约束 (§12.3, §14, §18)

最终仓位 = min(基础仓位 × 市场系数, 当前状态单 ETF 上限)

组合约束:
- 最多同时持仓 5 只
- 按 FinalScore 从高到低分配
- 同一主题合计仓位 <= 25%
- 进攻仓合计 <= 30% 净值
- 账户级熔断: 周回撤 > 8% / 月回撤 > 15% → 总仓位降至 20%，暂停新开仓 10 交易日
"""
import logging
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from .config import StrategyConfig
from .universe import UniverseBuilder

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """单笔持仓"""
    symbol: str
    theme: str
    entry_date: str
    entry_price: float
    shares: float
    position_pct: float       # 建仓时占净值比例
    signal_mode: str          # "A" or "B"
    atr_at_entry: float       # 买入日 ATR14
    stop_price: float         # ATR 止损价
    take_profit_1_used: bool = False  # 一级止盈是否已使用
    holding_days: int = 0


@dataclass
class PortfolioState:
    """组合状态"""
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    net_value_history: List[Tuple[str, float]] = field(default_factory=list)
    # 熔断状态
    circuit_breaker_active: bool = False
    circuit_breaker_pause_until: int = 0  # 暂停到第几个交易日
    circuit_breaker_position_limit: float = 1.0  # 熔断时仓位上限
    peak_net_value: float = 0.0


class PortfolioManager:
    """仓位管理器"""

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        self.sc = self.config.signal
        self.ec = self.config.exit
        self.universe = UniverseBuilder(self.config)

    def calculate_position_size(
        self,
        base_position: float,
        regime: str,
        signal_mode: str,
    ) -> float:
        """
        计算最终仓位 (§14)

        最终仓位 = min(基础仓位 × 市场系数, 单 ETF 上限)

        Args:
            base_position: 信号仓位（模式 A: 10%, 模式 B: 5%）
            regime: 市场状态
            signal_mode: "A" or "B"

        Returns:
            仓位占比（相对净值）
        """
        limits = self.config.regime.regime_position_limits.get(
            regime, self.config.regime.regime_position_limits["Neutral"]
        )
        coef = limits["coef"]
        single_max = limits["single"]

        position = min(base_position * coef, single_max)

        # 模式 B 额外限制
        if signal_mode == "B":
            position = min(position, self.sc.mode_b_single_max)

        return position

    def select_holdings(
        self,
        candidates: List[Tuple[str, float, str, str]],
        max_holdings: int = None,
        current_holdings: Dict[str, Position] = None,
    ) -> List[Tuple[str, float, str, str]]:
        """
        选择持仓标的 (§12.3)

        按 FinalScore 从高到低分配，触上限则不再开更低分标的。
        同一主题合计仓位 <= 25%。

        Args:
            candidates: [(symbol, final_score, signal_mode, signal_position), ...]
            max_holdings: 最大持仓数
            current_holdings: 当前持仓

        Returns:
            选中的候选列表
        """
        max_h = max_holdings or self.sc.max_holdings
        current = current_holdings or {}

        # 已持仓数
        available_slots = max_h - len(current)
        if available_slots <= 0:
            return []

        # 按 FinalScore 降序
        sorted_candidates = sorted(candidates, key=lambda x: -x[1])

        # 主题仓位累计
        theme_positions = {}
        for pos in current.values():
            theme_positions[pos.theme] = theme_positions.get(pos.theme, 0) + pos.position_pct

        selected = []
        for symbol, score, mode, sig_pos in sorted_candidates:
            if len(selected) >= available_slots:
                break

            theme = self.universe.get_theme(symbol)
            current_theme_pct = theme_positions.get(theme, 0)

            # 检查主题上限
            if current_theme_pct + sig_pos > self.sc.same_theme_max:
                # 可以部分开仓
                allowed = self.sc.same_theme_max - current_theme_pct
                if allowed <= 0:
                    continue
                sig_pos = allowed

            theme_positions[theme] = current_theme_pct + sig_pos
            selected.append((symbol, score, mode, sig_pos))

        return selected

    def check_circuit_breaker(
        self, state: PortfolioState, current_day: int
    ) -> bool:
        """
        检查账户级熔断 (§18)

        周回撤 > 8% 或 月回撤 > 15% → 触发熔断

        Returns:
            True 表示熔断已触发（应减仓并暂停开仓）
        """
        if not state.net_value_history:
            return False

        # 更新峰值
        latest_nv = state.net_value_history[-1][1]
        if latest_nv > state.peak_net_value:
            state.peak_net_value = latest_nv

        # 周回撤（近 5 日）
        recent_5 = state.net_value_history[-5:] if len(state.net_value_history) >= 5 else state.net_value_history
        if recent_5:
            peak_5d = max(nv for _, nv in recent_5)
            weekly_dd = (peak_5d - latest_nv) / peak_5d if peak_5d > 0 else 0
            if weekly_dd > self.ec.weekly_drawdown_threshold:
                return True

        # 月回撤（近 21 日）
        recent_21 = state.net_value_history[-21:] if len(state.net_value_history) >= 21 else state.net_value_history
        if recent_21:
            peak_21d = max(nv for _, nv in recent_21)
            monthly_dd = (peak_21d - latest_nv) / peak_21d if peak_21d > 0 else 0
            if monthly_dd > self.ec.monthly_drawdown_threshold:
                return True

        return False

    def check_circuit_breaker_recovery(
        self, state: PortfolioState
    ) -> bool:
        """
        检查熔断恢复条件 (§18)

        净值回撤 <= 5%（相对历史高点）后解除。
        """
        if not state.net_value_history:
            return False

        latest_nv = state.net_value_history[-1][1]
        if state.peak_net_value <= 0:
            return False

        drawdown = (state.peak_net_value - latest_nv) / state.peak_net_value
        return drawdown <= self.ec.circuit_breaker_recover_threshold

    def get_total_position_pct(self, state: PortfolioState) -> float:
        """当前总仓位占比"""
        if not state.positions:
            return 0.0
        return sum(p.position_pct for p in state.positions.values())

    def get_aggressive_position_pct(self, state: PortfolioState) -> float:
        """进攻仓合计占比"""
        return sum(
            p.position_pct for p in state.positions.values()
            if p.signal_mode == "B"
        )
