"""
ETF 底部右侧启动趋势策略 V4.3 Final - 配置常量

所有策略参数集中管理，对应文档 docs/strategy/etf-bottom-right-trend-v4.3-final.md
冻结版本，修改须另开版本号。
"""
from dataclasses import dataclass, field
from typing import Dict


@dataclass(frozen=True)
class UniverseConfig:
    """§3 ETF 候选池配置"""
    min_list_days: int = 252          # 上市时间 >= 1 年（约 252 交易日）
    min_turnover_20d: float = 5e7     # 过去 20 日日均成交额 >= 5000 万元
    # 剔除类型关键字（基金简称含以下关键字则剔除）
    exclude_keywords: tuple = (
        "货币", "债券", "债", "商品", "黄金", "原油", "白银",
        "杠杆", "反向", "做空", "增强",  # 增强型也剔除（非纯被动）
    )


@dataclass(frozen=True)
class PositionConfig:
    """§5 趋势位置配置"""
    # 位置系数分档 [a, b) 左闭右开，最高档单独注明
    position_buckets: tuple = (
        # (下界, 上界, 基础分)  上界 None 表示无上限
        (0.70, 0.85, 20),
        (0.85, 0.95, 14),
        (0.95, 1.00, 6),
        (0.00, 0.70, 5),
        (1.00, None, 4),   # > 1.00 创新高
    )
    long_filter_fail_factor: float = 0.5  # 不满足长周期过滤 → floor(基础分 × 0.5)


@dataclass(frozen=True)
class BreakoutConfig:
    """§6 趋势突破配置"""
    # 取档不叠加
    breakout_scores: Dict[str, int] = field(default_factory=lambda: {
        "breakout_20": 8,
        "breakout_60": 16,
        "breakout_120": 20,
    })
    # 额外条件 +2（可叠加，总分封顶 20）
    extra_condition_score: int = 2
    max_score: int = 20
    # 60/120 日突破需连续两日确认
    confirm_days_60: int = 2
    confirm_days_120: int = 2


@dataclass(frozen=True)
class RelativeStrengthConfig:
    """§7 相对强度配置"""
    # 20 日超额收益分档
    excess_buckets: tuple = (
        # (下界, 上界, 分)  上界 None 表示无上限
        (0.06, None, 16),
        (0.03, 0.06, 12),
        (0.00, 0.03, 6),
        (None, 0.00, 0),
    )
    rs_slope_score: int = 4       # RS 斜率 > 0 → +4
    rs_lookback_days: int = 5     # RS 线性回归回看天数


@dataclass(frozen=True)
class VolumePriceConfig:
    """§8 量价健康度配置"""
    # 涨跌量比分档（10 分）
    up_down_vol_buckets: tuple = (
        (1.2, None, 10),
        (1.0, 1.2, 7),
        (0.8, 1.0, 4),
        (None, 0.8, 0),
    )
    # 价格效率分档（5 分）
    efficiency_buckets: tuple = (
        (0.8, None, 5),
        (0.3, 0.8, 3),
        (None, 0.3, 0),
    )


@dataclass(frozen=True)
class VolumeStructureConfig:
    """§9 量能结构配置"""
    # 量能趋势分档（12 分）
    vol_trend_buckets: tuple = (
        (1.3, None, 12),
        (1.15, 1.3, 8),
        (1.0, 1.15, 4),
        (None, 1.0, 0),
    )
    # 连续放量（3 分），每天 +1，最多 3
    consecutive_vol_max: int = 3


@dataclass(frozen=True)
class VolatilityConfig:
    """§10 波动风险配置"""
    vol_risk_buckets: tuple = (
        (None, 0.02, 10),
        (0.02, 0.04, 7),
        (0.04, 0.06, 3),
        (0.06, None, 0),
    )


@dataclass(frozen=True)
class PenaltyConfig:
    """§11 扣分机制配置"""
    max_penalty: int = 20
    # 高位加速
    high_accel_threshold: float = 0.20    # Return_20 > 20%
    high_accel_step: float = 0.03         # 每 3% 扣 2 分
    high_accel_per_step: int = 2
    high_accel_max: int = 8               # 最多扣 8
    # 爆量滞涨
    volume_spike_ratio: float = 2.0        # 量比 > 2
    volume_spike_price_threshold: float = 0.01  # |当日涨跌幅| < 1%
    volume_spike_penalty: int = 6
    # 一日游
    one_day_wonder_prev_threshold: int = 70   # 昨日 FinalScore >= 70
    one_day_wonder_today_threshold: int = 50  # 今日模块合计（扣分前） < 50
    one_day_wonder_penalty: int = 6


@dataclass(frozen=True)
class SignalConfig:
    """§12 买入规则配置"""
    # 模式 A：稳健型
    mode_a_score_threshold: int = 75
    mode_a_confirm_days: int = 2          # 连续两个交易日
    mode_a_base_position: float = 0.10    # 基础仓位 10%
    mode_a_position_range: tuple = (0.08, 0.12)

    # 模式 B：进攻型
    mode_b_score_threshold: int = 90
    mode_b_vol_trend: float = 1.3
    mode_b_excess_20: float = 0.03
    mode_b_position_factor: float = 0.5   # 模式A基础仓位 × 50%
    mode_b_single_max: float = 0.05       # 单 ETF <= 5%
    mode_b_total_max: float = 0.30        # 进攻仓合计 <= 30%

    # 组合约束
    max_holdings: int = 5                 # 最多同时持仓 5 只
    same_theme_max: float = 0.25          # 同一主题合计仓位 <= 25%


@dataclass(frozen=True)
class RegimeConfig:
    """§13 市场环境模型配置"""
    confirm_days: int = 3   # 状态切换需连续 3 日确认
    # 各状态系数
    regime_coefficients: Dict[str, float] = field(default_factory=lambda: {
        "Bull": 1.0,
        "Recovery": 0.5,
        "Neutral": 0.3,
        "Bear": 0.2,
    })
    # 各状态仓位上限
    regime_position_limits: Dict[str, dict] = field(default_factory=lambda: {
        "Bull":     {"single": 0.20, "total": 0.80, "coef": 1.0},
        "Recovery": {"single": 0.12, "total": 0.50, "coef": 0.5},
        "Neutral":  {"single": 0.10, "total": 0.40, "coef": 0.3},
        "Bear":     {"single": 0.05, "total": 0.15, "coef": 0.2},
    })
    # Neutral 近均线阈值
    neutral_near_ma250: float = 0.05      # |Close - MA250| / MA250 <= 5%
    neutral_ma_converge: float = 0.02     # |MA60 - MA120| / MA120 < 2%


@dataclass(frozen=True)
class ExitConfig:
    """§15-18 退出系统配置"""
    # 各退出规则开关（用于减法实验）
    enable_atr_stop: bool = True
    enable_ma_stop: bool = True           # 均线止损/二级止盈
    enable_tp1: bool = True               # 一级止盈(MA10减仓)
    enable_time_stop: bool = True         # 时间止损
    enable_extreme_tp: bool = True        # 极端止盈
    enable_circuit_breaker: bool = True   # 账户级熔断
    # 初始 ATR 止损
    atr_stop_multiplier: float = 2.5      # 止损价 = 买入价 - 2.5 × ATR14
    # 均线止损 = 二级止盈
    ma_stop_period: int = 20
    ma_stop_recover_days: int = 1         # 次日未收回 → 全平
    # 一级止盈
    take_profit_1_period: int = 10        # 跌破 MA10
    take_profit_1_reduce: float = 0.5     # 减仓 50%
    # 极端止盈
    extreme_profit_threshold: float = 0.30  # 累计收益 > 30%
    extreme_volume_ratio: float = 2.0      # 量比 > 2
    extreme_price_change: float = 0.01     # 当日涨幅 < 1%
    # 时间止损
    time_stop_days: int = 20              # 第 20 个交易日
    time_stop_threshold: float = 1.1      # 期间所有收盘价 < 买入价 × 1.1
    # 账户级熔断
    weekly_drawdown_threshold: float = 0.08   # 周回撤 > 8%
    monthly_drawdown_threshold: float = 0.15  # 月回撤 > 15%
    circuit_breaker_position: float = 0.20     # 总仓位降至 20%
    circuit_breaker_pause_days: int = 10       # 暂停新开仓 10 交易日
    circuit_breaker_recover_threshold: float = 0.05  # 回撤 <= 5% 解除


@dataclass(frozen=True)
class BacktestConfig:
    """§19 回测规范配置"""
    start_date: str = "2018-01-01"
    end_date: str = "2026-12-31"
    train_end: str = "2023-12-31"         # 训练/样本内
    test_start: str = "2024-01-01"        # 样本外
    commission_rate: float = 0.0003       # 佣金 万 3
    slippage: float = 0.001               # 滑点 0.1%
    initial_capital: float = 1_000_000.0  # 初始资金 100 万

    # 数据拉取起始（比回测早 1 年以确保指标预热）
    data_start_date: str = "2017-01-01"


@dataclass(frozen=True)
class StrategyConfig:
    """策略总配置（V4.3 Final 冻结）"""
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    position: PositionConfig = field(default_factory=PositionConfig)
    breakout: BreakoutConfig = field(default_factory=BreakoutConfig)
    relative_strength: RelativeStrengthConfig = field(default_factory=RelativeStrengthConfig)
    volume_price: VolumePriceConfig = field(default_factory=VolumePriceConfig)
    volume_structure: VolumeStructureConfig = field(default_factory=VolumeStructureConfig)
    volatility: VolatilityConfig = field(default_factory=VolatilityConfig)
    penalty: PenaltyConfig = field(default_factory=PenaltyConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    # 沪深 300 基准代码
    hs300_index_code: str = "000300"      # 指数代码
    hs300_index_ts_code: str = "000300.SH"
    hs300_etf_code: str = "510300"        # 代表性 ETF

    # 主题映射表（人工维护，§3.3）
    theme_mapping: Dict[str, str] = field(default_factory=lambda: {
        # 宽基
        "510300": "宽基沪深300", "510500": "宽基中证500", "510050": "宽基上证50",
        "159915": "宽基创业板", "512100": "宽基中证1000", "588000": "宽基科创50",
        # 行业主题
        "512760": "半导体芯片", "159995": "半导体芯片", "512480": "半导体芯片",
        "512010": "医药健康", "159938": "医药健康", "512170": "医药健康",
        "512660": "军工", "512680": "军工",
        "512200": "房地产", "512150": "房地产",
        "515030": "新能源", "516160": "新能源", "159875": "新能源",
        "512690": "白酒消费", "159928": "白酒消费", "510150": "白酒消费",
        "512800": "银行金融", "512000": "银行金融", "515020": "银行金融",
        "159766": "旅游消费",
        "515170": "食品饮料",
        "512610": "家电",
        "515790": "光伏",
        "515050": "5G通信", "515880": "5G通信",
        "512980": "传媒",
        "516950": "基建",
        "159869": "游戏",
    })
