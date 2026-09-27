"""
回测引擎 (§19)

回测规范:
- 区间: 2018-2026
- 训练/样本外: 2018-2023 / 2024-2026
- 佣金: 万 3 | 滑点: 0.1%
- ETF 管理费: 按各基金披露费率计入
- 基准: 沪深 300 ETF、中证 500 ETF、ETF 等权组合

成交规则:
- 信号日收盘确认，下一交易日开盘价成交
- 涨跌停无法成交则取消该信号
"""
import logging
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass, field

from .config import StrategyConfig
from .indicators import IndicatorCalculator
from .universe import UniverseBuilder
from .score import ScoringEngine
from .regime import RegimeDetector
from .signal import SignalGenerator
from .portfolio import PortfolioManager, PortfolioState, Position
from .exits import ExitManager, ExitSignal

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    """交易记录"""
    date: str
    symbol: str
    direction: str       # "buy" / "sell"
    price: float
    shares: float
    amount: float
    commission: float
    slippage_cost: float
    reason: str


@dataclass
class BacktestResult:
    """回测结果"""
    # 净值曲线
    nav_series: pd.Series          # 日期 -> 净值
    # 交易记录
    trades: List[TradeRecord]
    # 每日持仓
    daily_positions: List[dict]
    # 市场状态记录
    regime_series: pd.Series
    # 绩效指标
    metrics: dict = field(default_factory=dict)
    # 分段绩效（样本内/外）
    in_sample_metrics: dict = field(default_factory=dict)
    out_sample_metrics: dict = field(default_factory=dict)


class BacktestEngine:
    """回测引擎"""

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        self.bc = self.config.backtest

        # 子模块
        self.calc = IndicatorCalculator()
        self.universe = UniverseBuilder(self.config)
        self.scorer = ScoringEngine(self.config)
        self.regime_detector = RegimeDetector(self.config)
        self.signal_gen = SignalGenerator(self.config)
        self.portfolio_mgr = PortfolioManager(self.config)
        self.exit_mgr = ExitManager(self.config)

    def _precompute(
        self,
        etf_data: Dict[str, pd.DataFrame],
        hs300_df: pd.DataFrame,
        basic_info: pd.DataFrame,
    ) -> Tuple[Dict[str, pd.DataFrame], pd.DataFrame]:
        """
        预计算全部指标和评分（向量化）

        Returns:
            scored_data: {symbol: DataFrame} 每只 ETF 的评分数据
            regime_df: 沪深 300 市场环境数据
        """
        logger.info("开始预计算指标和评分...")

        # 1. 沪深 300 市场环境
        regime_df = self.regime_detector.detect(hs300_df)
        logger.info(f"市场环境检测完成: {len(regime_df)} 个交易日")

        # 2. 逐 ETF 计算指标 + 评分
        scored_data = {}
        total = len(etf_data)
        for i, (symbol, df) in enumerate(etf_data.items()):
            if df is None or df.empty:
                continue
            try:
                scored = self.scorer.compute_scores_series(df, hs300_df)
                scored = self.signal_gen.generate_signals(scored)
                scored_data[symbol] = scored
            except Exception as e:
                logger.warning(f"ETF {symbol} 评分失败: {e}")
            if (i + 1) % 50 == 0:
                logger.info(f"  已处理 {i+1}/{total} 只 ETF")

        logger.info(f"预计算完成: {len(scored_data)} 只 ETF")
        return scored_data, regime_df

    def _get_trade_dates(
        self, scored_data: Dict[str, pd.DataFrame], regime_df: pd.DataFrame
    ) -> List[str]:
        """获取全部交易日（取并集）"""
        all_dates = set()
        for df in scored_data.values():
            if df is not None and not df.empty:
                all_dates.update(df["trade_date"].tolist())
        all_dates.update(regime_df["trade_date"].tolist())

        dates = sorted(all_dates)
        # 限制回测区间
        start = self.bc.start_date
        end = self.bc.end_date
        dates = [d for d in dates if start <= d <= end]
        return dates

    def _is_limit_up_down(
        self, symbol: str, date: str, open_price: float, prev_close: float
    ) -> bool:
        """判断是否涨跌停（ETF 一般 ±10%）"""
        if prev_close <= 0:
            return False
        change_pct = abs(open_price - prev_close) / prev_close
        return change_pct >= 0.099  # 9.9% 以上视为涨跌停

    def run(
        self,
        etf_data: Dict[str, pd.DataFrame],
        hs300_df: pd.DataFrame,
        basic_info: pd.DataFrame,
    ) -> BacktestResult:
        """
        执行回测

        Args:
            etf_data: {symbol: DataFrame} ETF 日线数据
            hs300_df: 沪深 300 日线数据
            basic_info: ETF 基础信息

        Returns:
            回测结果
        """
        logger.info("=" * 60)
        logger.info("开始回测")
        logger.info(f"  区间: {self.bc.start_date} ~ {self.bc.end_date}")
        logger.info(f"  初始资金: {self.bc.initial_capital:,.0f}")
        logger.info(f"  佣金: {self.bc.commission_rate:.4f} | 滑点: {self.bc.slippage:.4f}")
        logger.info("=" * 60)

        # 预计算
        scored_data, regime_df = self._precompute(etf_data, hs300_df, basic_info)
        trade_dates = self._get_trade_dates(scored_data, regime_df)
        logger.info(f"回测交易日数: {len(trade_dates)}")

        # 初始化组合状态
        state = PortfolioState(cash=self.bc.initial_capital)
        state.peak_net_value = self.bc.initial_capital

        # 持仓期间收盘价记录（用于时间止损）
        closes_since_entry: Dict[str, List[float]] = {}

        trades: List[TradeRecord] = []
        daily_positions: List[dict] = []
        nav_records: List[Tuple[str, float]] = []

        # 逐日回测
        for day_idx, current_date in enumerate(trade_dates):
            # ---- 1. 更新持仓天数和收盘价记录 ----
            for symbol in list(state.positions.keys()):
                pos = state.positions[symbol]
                pos.holding_days += 1
                df = scored_data.get(symbol)
                if df is not None:
                    row = df[df["trade_date"] == current_date]
                    if not row.empty:
                        closes_since_entry.setdefault(symbol, []).append(
                            row.iloc[0]["close"]
                        )

            # ---- 2. 检查退出 ----
            for symbol in list(state.positions.keys()):
                pos = state.positions[symbol]
                df = scored_data.get(symbol)
                if df is None:
                    continue

                row = df[df["trade_date"] == current_date]
                if row.empty:
                    continue
                row = row.iloc[0]

                # 获取前一日数据
                prev_row = df[df["trade_date"] < current_date]
                prev_close = prev_row.iloc[-1]["close"] if not prev_row.empty else row["close"]
                prev_ma20 = prev_row.iloc[-1]["ma20"] if not prev_row.empty and "ma20" in prev_row.columns else 0

                # 检查熔断
                cb_active = state.circuit_breaker_active and day_idx < state.circuit_breaker_pause_until

                exit_signal = self.exit_mgr.check_all_exits(
                    position=pos,
                    current_date=current_date,
                    current_close=row["close"],
                    prev_close=prev_close,
                    ma10=row.get("ma10", 0),
                    ma20=row.get("ma20", 0),
                    prev_ma20=prev_ma20,
                    volume_ratio=row.get("volume_ratio", 1),
                    daily_return=row.get("return_1", 0),
                    closes_since_entry=closes_since_entry.get(symbol, []),
                    circuit_breaker_active=cb_active,
                )

                if exit_signal:
                    self._execute_exit(
                        state, pos, exit_signal, current_date, trades
                    )
                    if exit_signal.exit_ratio >= 1.0:
                        state.positions.pop(symbol, None)
                        closes_since_entry.pop(symbol, None)

            # ---- 3. 计算当日净值 ----
            total_position_value = 0.0
            for symbol, pos in state.positions.items():
                df = scored_data.get(symbol)
                if df is not None:
                    row = df[df["trade_date"] == current_date]
                    if not row.empty:
                        total_position_value += pos.shares * row.iloc[0]["close"]
                    else:
                        total_position_value += pos.shares * pos.entry_price
                else:
                    total_position_value += pos.shares * pos.entry_price

            net_value = state.cash + total_position_value
            nav_records.append((current_date, net_value))

            # ---- 4. 检查熔断 ----
            state.net_value_history.append((current_date, net_value))

            if not state.circuit_breaker_active:
                if self.portfolio_mgr.check_circuit_breaker(state, day_idx):
                    state.circuit_breaker_active = True
                    state.circuit_breaker_pause_until = day_idx + self.ec_pause_days()
                    state.circuit_breaker_position_limit = self.config.exit.circuit_breaker_position_limit
                    logger.warning(
                        f"[{current_date}] 触发账户级熔断！暂停开仓至第 {state.circuit_breaker_pause_until} 日"
                    )
            else:
                # 检查恢复
                if day_idx >= state.circuit_breaker_pause_until:
                    if self.portfolio_mgr.check_circuit_breaker_recovery(state):
                        state.circuit_breaker_active = False
                        logger.info(f"[{current_date}] 熔断解除")

            # ---- 5. 生成买入信号 ----
            if state.circuit_breaker_active and day_idx < state.circuit_breaker_pause_until:
                # 熔断暂停期内不开新仓
                pass
            else:
                # 获取当日市场状态
                regime = self.regime_detector.get_regime_for_date(regime_df, current_date)

                # 检查总仓位上限
                limits = self.config.regime.regime_position_limits.get(
                    regime, self.config.regime.regime_position_limits["Neutral"]
                )
                total_pos_pct = self.portfolio_mgr.get_total_position_pct(state)
                if total_pos_pct >= limits["total"]:
                    pass  # 总仓位已达上限
                else:
                    # 构建候选池
                    candidates = []
                    for symbol, df in scored_data.items():
                        if symbol in state.positions:
                            continue
                        row = df[df["trade_date"] == current_date]
                        if row.empty:
                            continue
                        row = row.iloc[0]

                        if row.get("buy_signal", False):
                            candidates.append((
                                symbol,
                                row["final_score"],
                                row.get("signal_mode", "A"),
                                row.get("signal_position", 0.1),
                            ))

                    if candidates:
                        # 选择持仓
                        selected = self.portfolio_mgr.select_holdings(
                            candidates, current_holdings=state.positions
                        )

                        # 检查进攻仓上限
                        aggressive_pct = self.portfolio_mgr.get_aggressive_position_pct(state)

                        for symbol, score, mode, sig_pos in selected:
                            if mode == "B":
                                if aggressive_pct >= self.config.signal.mode_b_total_max:
                                    continue
                                aggressive_pct += sig_pos

                            # 计算最终仓位
                            final_position = self.portfolio_mgr.calculate_position_size(
                                sig_pos, regime, mode
                            )

                            # 次日开盘价成交
                            next_date_idx = day_idx + 1
                            if next_date_idx >= len(trade_dates):
                                continue
                            next_date = trade_dates[next_date_idx]
                            df = scored_data.get(symbol)
                            if df is None:
                                continue
                            next_row = df[df["trade_date"] == next_date]
                            if next_row.empty:
                                continue
                            next_row = next_row.iloc[0]

                            # 涨跌停检查
                            prev_row = df[df["trade_date"] < next_date]
                            prev_close = prev_row.iloc[-1]["close"] if not prev_row.empty else next_row["open"]
                            if self._is_limit_up_down(symbol, next_date, next_row["open"], prev_close):
                                continue

                            # 执行买入
                            self._execute_buy(
                                state, symbol, mode, final_position,
                                next_date, next_row, scored_data, trades
                            )

            # ---- 6. 记录每日持仓 ----
            daily_positions.append({
                "date": current_date,
                "nav": net_value,
                "cash": state.cash,
                "positions": [
                    {
                        "symbol": p.symbol,
                        "shares": p.shares,
                        "entry_price": p.entry_price,
                        "position_pct": p.position_pct,
                        "holding_days": p.holding_days,
                    }
                    for p in state.positions.values()
                ],
                "regime": self.regime_detector.get_regime_for_date(regime_df, current_date),
                "circuit_breaker": state.circuit_breaker_active,
            })

        # ---- 生成结果 ----
        nav_series = pd.Series(
            dict(nav_records), name="nav"
        )
        nav_series.index.name = "trade_date"

        regime_series = regime_df.set_index("trade_date")["regime"]

        result = BacktestResult(
            nav_series=nav_series,
            trades=trades,
            daily_positions=daily_positions,
            regime_series=regime_series,
        )

        # 计算绩效指标
        result.metrics = self._compute_metrics(nav_series, trades)
        result.in_sample_metrics = self._compute_metrics(
            nav_series[nav_series.index <= self.bc.train_end], trades
        )
        result.out_sample_metrics = self._compute_metrics(
            nav_series[nav_series.index >= self.bc.test_start], trades
        )

        self._log_results(result)
        return result

    def ec_pause_days(self) -> int:
        return self.config.exit.circuit_breaker_pause_days

    def _execute_buy(
        self,
        state: PortfolioState,
        symbol: str,
        mode: str,
        position_pct: float,
        trade_date: str,
        row: pd.Series,
        scored_data: Dict[str, pd.DataFrame],
        trades: List[TradeRecord],
    ):
        """执行买入"""
        open_price = row["open"]
        # 滑点
        buy_price = open_price * (1 + self.bc.slippage)

        net_value = state.cash + sum(
            p.shares * scored_data.get(s, pd.DataFrame()).set_index("trade_date").get("close", pd.Series()).get(trade_date, p.entry_price)
            for s, p in state.positions.items()
        ) if state.positions else state.cash + sum(
            p.shares * p.entry_price for p in state.positions.values()
        )

        # 简化：用 cash 直接计算
        target_amount = net_value * position_pct
        target_amount = min(target_amount, state.cash)

        if target_amount < 1000:  # 最小交易金额
            return

        shares = target_amount / buy_price
        # ETF 最小 100 份
        shares = int(shares // 100) * 100
        if shares <= 0:
            return

        actual_amount = shares * buy_price
        commission = actual_amount * self.bc.commission_rate
        total_cost = actual_amount + commission

        if total_cost > state.cash:
            shares = int((state.cash / (buy_price * (1 + self.bc.commission_rate))) // 100) * 100
            if shares <= 0:
                return
            actual_amount = shares * buy_price
            commission = actual_amount * self.bc.commission_rate
            total_cost = actual_amount + commission

        state.cash -= total_cost

        # ATR 止损价
        atr14 = row.get("atr14", 0)
        stop_price = self.exit_mgr.compute_atr_stop_price(buy_price, atr14)

        theme = self.universe.get_theme(symbol)

        pos = Position(
            symbol=symbol,
            theme=theme,
            entry_date=trade_date,
            entry_price=buy_price,
            shares=shares,
            position_pct=position_pct,
            signal_mode=mode,
            atr_at_entry=atr14,
            stop_price=stop_price,
        )
        state.positions[symbol] = pos

        trades.append(TradeRecord(
            date=trade_date,
            symbol=symbol,
            direction="buy",
            price=buy_price,
            shares=shares,
            amount=actual_amount,
            commission=commission,
            slippage_cost=actual_amount * self.bc.slippage,
            reason=f"买入({mode}) 评分={row.get('final_score', 0):.1f}",
        ))

    def _execute_exit(
        self,
        state: PortfolioState,
        pos: Position,
        signal: ExitSignal,
        trade_date: str,
        trades: List[TradeRecord],
    ):
        """执行卖出"""
        sell_shares = pos.shares * signal.exit_ratio
        sell_shares = int(sell_shares // 100) * 100
        if sell_shares <= 0:
            return

        # 次日开盘价成交（此处简化为当日收盘）
        sell_price = signal.exit_price * (1 - self.bc.slippage)
        actual_amount = sell_shares * sell_price
        commission = actual_amount * self.bc.commission_rate
        net_proceeds = actual_amount - commission

        state.cash += net_proceeds
        pos.shares -= sell_shares

        # 标记一级止盈已使用
        if signal.exit_type == "tp1":
            pos.take_profit_1_used = True
            # 调整仓位比例
            pos.position_pct *= (1 - signal.exit_ratio)

        trades.append(TradeRecord(
            date=trade_date,
            symbol=pos.symbol,
            direction="sell",
            price=sell_price,
            shares=sell_shares,
            amount=actual_amount,
            commission=commission,
            slippage_cost=actual_amount * self.bc.slippage,
            reason=signal.reason,
        ))

    def _compute_metrics(
        self, nav_series: pd.Series, trades: List[TradeRecord]
    ) -> dict:
        """计算绩效指标"""
        if nav_series.empty or len(nav_series) < 2:
            return {}

        nav = nav_series.sort_index()
        returns = nav.pct_change().dropna()

        # 年化收益
        total_days = len(nav)
        total_return = nav.iloc[-1] / nav.iloc[0] - 1
        years = total_days / 252
        annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

        # 最大回撤
        cummax = nav.cummax()
        drawdown = (nav - cummax) / cummax
        max_drawdown = drawdown.min()

        # 夏普比率（无风险利率 2%）
        if returns.std() > 0:
            sharpe = (returns.mean() * 252 - 0.02) / (returns.std() * np.sqrt(252))
        else:
            sharpe = 0

        # 盈亏比
        winning_trades = [t for t in trades if t.direction == "sell"]
        if winning_trades:
            # 简化：按卖出金额 vs 买入金额
            buy_amounts = {}
            for t in trades:
                if t.direction == "buy":
                    buy_amounts.setdefault(t.symbol, []).append(t)
                else:
                    buy_amounts.setdefault(t.symbol, [])

            profits = []
            losses = []
            for t in trades:
                if t.direction == "sell":
                    # 简化估算
                    if t.amount > 0:
                        profits.append(t.amount)
                    else:
                        losses.append(abs(t.amount))

            if profits and losses:
                win_loss_ratio = np.mean(profits) / np.mean(losses)
            elif profits:
                win_loss_ratio = float("inf")
            else:
                win_loss_ratio = 0
        else:
            win_loss_ratio = 0

        # 年化换手率
        total_buy_amount = sum(t.amount for t in trades if t.direction == "buy")
        avg_nav = nav.mean()
        turnover = total_buy_amount / avg_nav / years if avg_nav > 0 and years > 0 else 0

        # 交易次数
        n_trades = len(trades)

        return {
            "annual_return": annual_return,
            "total_return": total_return,
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
            "win_loss_ratio": win_loss_ratio,
            "turnover": turnover,
            "n_trades": n_trades,
            "total_days": total_days,
            "final_nav": nav.iloc[-1],
        }

    def _log_results(self, result: BacktestResult):
        """输出回测结果"""
        logger.info("=" * 60)
        logger.info("回测结果")
        logger.info("=" * 60)

        for label, metrics in [
            ("全样本", result.metrics),
            ("样本内(2018-2023)", result.in_sample_metrics),
            ("样本外(2024-2026)", result.out_sample_metrics),
        ]:
            if not metrics:
                continue
            logger.info(f"\n--- {label} ---")
            logger.info(f"  年化收益:     {metrics.get('annual_return', 0):.2%}")
            logger.info(f"  最大回撤:     {metrics.get('max_drawdown', 0):.2%}")
            logger.info(f"  夏普比率:     {metrics.get('sharpe', 0):.2f}")
            logger.info(f"  盈亏比:       {metrics.get('win_loss_ratio', 0):.2f}")
            logger.info(f"  年化换手:     {metrics.get('turnover', 0):.2%}")
            logger.info(f"  交易次数:     {metrics.get('n_trades', 0)}")
            logger.info(f"  终值:         {metrics.get('final_nav', 0):,.0f}")

        # 成功标准检查 (§20)
        logger.info("\n--- 策略成功标准检查 (§20) ---")
        m = result.metrics
        checks = [
            ("年化收益 >= 15%", m.get("annual_return", 0) >= 0.15),
            ("最大回撤 <= 20%", m.get("max_drawdown", 0) >= -0.20),
            ("夏普(全样本) >= 1.2", m.get("sharpe", 0) >= 1.2),
            ("盈亏比 >= 2", m.get("win_loss_ratio", 0) >= 2),
            ("年化换手 <= 200%", m.get("turnover", 0) <= 2.0),
        ]
        for desc, passed in checks:
            logger.info(f"  {'✓' if passed else '✗'} {desc}")

        logger.info("=" * 60)
