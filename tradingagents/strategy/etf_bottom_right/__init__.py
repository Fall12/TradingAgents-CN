"""
ETF 底部右侧启动趋势策略 V4.3 Final

基于 docs/strategy/etf-bottom-right-trend-v4.3-final.md 实现
八大模块: universe / indicators / score / regime / signal / portfolio / exits / backtest
"""

from .config import StrategyConfig
from .indicators import IndicatorCalculator
from .universe import UniverseBuilder
from .score import ScoringEngine
from .regime import RegimeDetector
from .signal import SignalGenerator
from .portfolio import PortfolioManager
from .exits import ExitManager
from .backtest import BacktestEngine
from .data_loader import ETFDataLoader

__all__ = [
    "StrategyConfig",
    "IndicatorCalculator",
    "UniverseBuilder",
    "ScoringEngine",
    "RegimeDetector",
    "SignalGenerator",
    "PortfolioManager",
    "ExitManager",
    "BacktestEngine",
    "ETFDataLoader",
]
