# ETF 底部右侧启动趋势策略 V4.3 Final - 使用指南

## 概述

本策略基于 `docs/strategy/etf-bottom-right-trend-v4.3-final.md` 实现，包含完整的八层策略架构：候选池筛选、技术指标计算、六模块评分、市场环境检测、买卖信号生成、仓位管理、退出规则、回测引擎。

策略代码位于 `tradingagents/strategy/etf_bottom_right/`，提供 CLI 命令行工具和 REST API 两种使用方式。

## 架构

```
tradingagents/strategy/etf_bottom_right/
├── __init__.py          # 包入口
├── config.py            # 策略配置常量（V4.3 Final 冻结）
├── indicators.py        # 技术指标计算（MA/ATR/High_N/Position/RS等）
├── universe.py          # ETF 候选池过滤 + 主题映射
├── score.py             # 六大评分模块 + 扣分机制
├── regime.py            # 市场环境模型（牛市/恢复/熊市/中性）
├── signal.py            # 买入信号（模式A稳健 + 模式B进攻）
├── portfolio.py         # 仓位管理 + 熔断机制
├── exits.py             # 多级退出规则（ATR止损/极端止盈/时间止损等）
├── backtest.py          # 回测引擎（成本/成交/样本内外报告）
└── data_loader.py       # 数据管道（清库/全量拉取/增量更新）
```

## 快速开始

### 1. 命令行工具

CLI 脚本位于 `scripts/run_etf_strategy.py`，提供以下子命令：

```bash
# 查看数据状态
python scripts/run_etf_strategy.py status

# 一键初始化（清库 + 全量拉取，从 2017 起）
python scripts/run_etf_strategy.py init -y

# 全量拉取（指定日期范围）
python scripts/run_etf_strategy.py pull --start 2017-01-01

# 增量更新（最近 10 个交易日）
python scripts/run_etf_strategy.py update --days 10

# 运行回测
python scripts/run_etf_strategy.py backtest

# 全流程（清库 → 拉取 → 回测）
python scripts/run_etf_strategy.py all -y

# 清库（仅 ETF 相关，保留股票数据）
python scripts/run_etf_strategy.py clear -y

# 详细日志
python scripts/run_etf_strategy.py -v pull --start 2017-01-01
```

### 2. 数据流程

```
清库（仅ETF行情） → 全量拉取（2017起） → 增量更新（10日） → 回测
       ↓                    ↓                   ↓              ↓
   clear_market_data    full_pull       incremental_update   backtest.run
```

**清库范围**：
- `stock_daily_quotes`：仅删除 ETF 代码前缀（51/15/56/58）的数据
- `market_quotes`：同上
- `stock_basic_info`：同上
- 沪深300指数（000300）一并清除
- 股票数据不受影响

**数据源**：Tushare 优先（需有 token），失败自动回退 AKShare。

### 3. 定时任务

通过 API 注册定时任务，默认交易日收盘后 16:30 执行增量更新：

```bash
# 启用定时任务
curl -X POST http://localhost:8000/api/etf-strategy/schedule/update \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "cron": "30 16 * * 1-5"}'

# 查看状态
curl http://localhost:8000/api/etf-strategy/schedule/status \
  -H "Authorization: Bearer <token>"

# 禁用定时任务
curl -X POST http://localhost:8000/api/etf-strategy/schedule/update \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

CRON 表达式 `30 16 * * 1-5` 含义：分30 时16 每日 每月 周一至周五。

## REST API

所有接口前缀：`/api/etf-strategy`

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | `/status` | 数据状态（ETF数量、日期范围等） | 登录 |
| POST | `/data/clear` | 清库（仅ETF或全部行情） | 管理员 |
| POST | `/data/full-pull` | 全量拉取（后台执行） | 管理员 |
| POST | `/data/update` | 增量更新（后台执行） | 登录 |
| POST | `/backtest` | 运行回测（后台执行） | 登录 |
| GET | `/backtest/results` | 历史回测结果列表 | 登录 |
| GET | `/config` | 策略配置参数 | 登录 |
| POST | `/schedule/update` | 管理定时更新任务 | 管理员 |
| GET | `/schedule/status` | 定时任务状态 | 登录 |

### API 调用示例

```bash
# 数据状态
curl http://localhost:8000/api/etf-strategy/status \
  -H "Authorization: Bearer <token>"

# 全量拉取
curl -X POST http://localhost:8000/api/etf-strategy/data/full-pull \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"start_date": "2017-01-01", "batch_delay": 0.3}'

# 增量更新
curl -X POST http://localhost:8000/api/etf-strategy/data/update \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"days": 10}'

# 运行回测
curl -X POST http://localhost:8000/api/etf-strategy/backtest \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{}'

# 查看回测结果
curl http://localhost:8000/api/etf-strategy/backtest/results?limit=10 \
  -H "Authorization: Bearer <token>"

# 策略配置
curl http://localhost:8000/api/etf-strategy/config \
  -H "Authorization: Bearer <token>"
```

## 回测结果

回测结果保存在 `logs/etf_backtest/` 目录下，包含三类文件：

| 文件 | 说明 |
|------|------|
| `nav_<timestamp>.csv` | 净值曲线（日期 + 净值） |
| `trades_<timestamp>.csv` | 交易记录（买卖日期/价格/数量/手续费等） |
| `summary_<timestamp>.json` | 绩效摘要（年化收益/最大回撤/夏普比率等） |

### 绩效指标

回测输出三段绩效：

- **全样本**（2018-2026）
- **样本内**（2018-2023，训练期）
- **样本外**（2024-2026，测试期）

指标包括：年化收益、总收益、最大回撤、夏普比率、盈亏比、年化换手率、交易次数、终值。

## 策略参数

策略配置定义在 `config.py` 中（V4.3 Final 冻结版本），主要参数：

| 模块 | 关键参数 | 默认值 |
|------|----------|--------|
| 候选池 | 上市天数 | 252 交易日 |
| 候选池 | 20日均成交额 | 5000 万 |
| 评分 | 模式A分数阈值 | 60 |
| 评分 | 模式B分数阈值 | 68 |
| 信号 | 模式B总仓位上限 | 20% |
| 仓位 | 单只上限（牛市） | 20% |
| 仓位 | 总仓位上限（牛市） | 80% |
| 退出 | ATR止损倍数 | 1.8 |
| 退出 | 一级止盈 | +15% |
| 退出 | 极端止盈 | +30% |
| 退出 | 时间止损 | 20 日 |
| 熔断 | 触发回撤 | 15% |
| 熔断 | 暂停天数 | 3 日 |
| 回测 | 初始资金 | 100 万 |
| 回测 | 佣金 | 万 3 |
| 回测 | 滑点 | 0.1% |

## Python 代码调用

```python
from tradingagents.strategy.etf_bottom_right import (
    StrategyConfig, ETFDataLoader, BacktestEngine
)

# 1. 加载配置
config = StrategyConfig()

# 2. 加载数据
loader = ETFDataLoader(config)
etf_data, hs300_df = loader.load_from_mongodb(start_date="2017-01-01")
basic_info = loader.load_etf_basic_info()

# 3. 运行回测
engine = BacktestEngine(config)
result = engine.run(etf_data, hs300_df, basic_info)

# 4. 查看结果
print(f"年化收益: {result.metrics['annual_return']:.2%}")
print(f"最大回撤: {result.metrics['max_drawdown']:.2%}")
print(f"夏普比率: {result.metrics['sharpe']:.2f}")
```

## 常见问题

### Q: 数据源选哪个？

系统自动选择：Tushare 优先（需在环境变量或系统配置中设置 token），失败回退 AKShare（无需配置）。两个数据源均不可用时会报错。

### Q: 全量拉取需要多久？

取决于 ETF 数量和 API 限速。约 800 只 ETF，每只间隔 0.3 秒，约需 4-5 分钟（Tushare）。AKShare 可能更慢。

### Q: 清库会影响股票数据吗？

默认只清 ETF 相关数据（代码前缀 51/15/56/58 + 沪深300指数 000300），股票数据完全不受影响。

### Q: 增量更新会覆盖旧数据吗？

是的，使用 upsert 模式（`ReplaceOne + upsert=True`），同一日期的数据会被覆盖修正，适合处理交易所盘后数据修正。

### Q: 定时任务在什么时间运行？

默认 CRON `30 16 * * 1-5`，即工作日 16:30（北京时间）。可通过 API 修改 CRON 表达式。
