#!/usr/bin/env python3
"""
ETF 底部右侧启动趋势策略 V4.3 Final - 命令行工具

用法:
    # 清库（仅清 ETF 行情，保留股票数据）
    python scripts/run_etf_strategy.py clear

    # 全量拉取 ETF + 沪深300（自 2017-01-01）
    python scripts/run_etf_strategy.py pull --start 2017-01-01

    # 增量更新最近 10 个交易日
    python scripts/run_etf_strategy.py update --days 10

    # 运行回测
    python scripts/run_etf_strategy.py backtest

    # 全流程：清库 → 全量拉取 → 回测
    python scripts/run_etf_strategy.py all

    # 查看数据状态
    python scripts/run_etf_strategy.py status

    # 一键初始化（清库 + 全量拉取）
    python scripts/run_etf_strategy.py init
"""
import argparse
import json
import logging
import sys
import os
from datetime import datetime
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 清除代理环境变量（避免 AKShare 等数据源因代理不可用而失败）
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_key, None)
os.environ.setdefault("NO_PROXY", "*")

from tradingagents.strategy.etf_bottom_right.config import StrategyConfig
from tradingagents.strategy.etf_bottom_right.data_loader import ETFDataLoader
from tradingagents.strategy.etf_bottom_right.backtest import BacktestEngine


def setup_logging(verbose: bool = False):
    """配置日志"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_clear(args):
    """清库"""
    print("\n" + "=" * 60)
    print("  ETF 策略 - 清库操作")
    print("=" * 60)

    if not args.yes:
        confirm = input(
            "\n⚠️  即将清除行情数据 "
            f"({'仅ETF相关' if args.etf_only else '全部行情'})\n"
            "  影响集合: stock_daily_quotes, market_quotes, stock_basic_info\n"
            "  此操作不可逆！确认继续？(yes/no): "
        )
        if confirm.lower() != "yes":
            print("已取消")
            return

    loader = ETFDataLoader()
    loader.clear_market_data(clear_etf_only=args.etf_only)
    print("\n✅ 清库完成")


def cmd_pull(args):
    """全量拉取"""
    print("\n" + "=" * 60)
    print("  ETF 策略 - 全量拉取（并行模式）")
    print(f"  日期范围: {args.start} ~ {args.end or '今天'}")
    print(f"  并行线程数: {args.workers}")
    print("=" * 60)

    loader = ETFDataLoader()
    stats = loader.full_pull(
        start_date=args.start,
        end_date=args.end,
        include_hs300=True,
        batch_delay=args.delay,
        max_workers=args.workers,
    )

    print("\n" + "=" * 60)
    print("  拉取结果:")
    print(f"    ETF 成功/总数: {stats['success_etfs']}/{stats['total_etfs']}")
    print(f"    跳过(已存在):  {stats.get('skipped_etfs', 0)}")
    print(f"    ETF 数据条数:  {stats['total_records']}")
    print(f"    沪深300条数:   {stats['hs300_records']}")
    print(f"    失败数:         {stats['failed_etfs']}")
    if stats["errors"]:
        print(f"    错误（前5条）:")
        for err in stats["errors"][:5]:
            print(f"      - {err}")
    print("=" * 60)

    # 保存统计结果
    report_path = PROJECT_ROOT / "logs" / f"etf_pull_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n统计报告已保存: {report_path}")


def cmd_update(args):
    """增量更新"""
    print("\n" + "=" * 60)
    print(f"  ETF 策略 - 增量更新（最近 {args.days} 个交易日）")
    print("=" * 60)

    loader = ETFDataLoader()
    stats = loader.incremental_update(days=args.days, max_workers=args.workers)

    print("\n" + "=" * 60)
    print("  更新结果:")
    print(f"    更新 ETF 数:   {stats['updated_etfs']}")
    print(f"    更新数据条数:  {stats['total_records']}")
    print(f"    沪深300已更新: {stats['hs300_updated']}")
    if stats["errors"]:
        print(f"    错误（前5条）:")
        for err in stats["errors"][:5]:
            print(f"      - {err}")
    print("=" * 60)


def cmd_backtest(args):
    """运行回测"""
    print("\n" + "=" * 60)
    print("  ETF 策略 - 回测")
    print("=" * 60)

    config = StrategyConfig()
    loader = ETFDataLoader(config)

    # 加载数据
    print("\n[1/3] 加载数据...")
    etf_data, hs300_df = loader.load_from_mongodb(
        start_date=config.backtest.data_start_date,
        end_date=args.end or config.backtest.end_date,
    )
    basic_info = loader.load_etf_basic_info()

    print(f"  ETF 数量: {len(etf_data)}")
    print(f"  沪深300:  {len(hs300_df)} 条")
    print(f"  基础信息: {len(basic_info)} 条")

    if not etf_data:
        print("\n❌ 无 ETF 数据，请先执行: python scripts/run_etf_strategy.py pull")
        return

    if hs300_df.empty:
        print("\n❌ 无沪深300数据，请先执行全量拉取")
        return

    # 运行回测
    print("\n[2/3] 执行回测...")
    engine = BacktestEngine(config)
    result = engine.run(etf_data, hs300_df, basic_info)

    # 输出结果
    print("\n[3/3] 回测结果:")
    print("=" * 60)

    for label, metrics in [
        ("全样本", result.metrics),
        ("样本内 (2018-2023)", result.in_sample_metrics),
        ("样本外 (2024-2026)", result.out_sample_metrics),
    ]:
        if not metrics:
            continue
        print(f"\n  --- {label} ---")
        print(f"    年化收益:  {metrics.get('annual_return', 0):.2%}")
        print(f"    总收益:    {metrics.get('total_return', 0):.2%}")
        print(f"    最大回撤:  {metrics.get('max_drawdown', 0):.2%}")
        print(f"    夏普比率:  {metrics.get('sharpe', 0):.2f}")
        print(f"    盈亏比:    {metrics.get('win_loss_ratio', 0):.2f}")
        print(f"    年化换手:  {metrics.get('turnover', 0):.2%}")
        print(f"    交易次数:  {metrics.get('n_trades', 0)}")
        print(f"    终值:      {metrics.get('final_nav', 0):,.0f}")

    print("\n" + "=" * 60)

    # 保存详细结果
    report_dir = PROJECT_ROOT / "logs" / "etf_backtest"
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 净值曲线
    nav_path = report_dir / f"nav_{timestamp}.csv"
    result.nav_series.to_csv(nav_path, header=["nav"])
    print(f"\n净值曲线已保存: {nav_path}")

    # 交易记录
    trades_path = report_dir / f"trades_{timestamp}.csv"
    trades_data = [
        {
            "date": t.date,
            "symbol": t.symbol,
            "direction": t.direction,
            "price": t.price,
            "shares": t.shares,
            "amount": t.amount,
            "commission": t.commission,
            "slippage_cost": t.slippage_cost,
            "reason": t.reason,
        }
        for t in result.trades
    ]
    import pandas as pd
    pd.DataFrame(trades_data).to_csv(trades_path, index=False)
    print(f"交易记录已保存: {trades_path}")

    # 绩效摘要
    summary_path = report_dir / f"summary_{timestamp}.json"
    summary = {
        "timestamp": timestamp,
        "metrics": result.metrics,
        "in_sample_metrics": result.in_sample_metrics,
        "out_sample_metrics": result.out_sample_metrics,
        "n_trades": len(result.trades),
        "n_trading_days": len(result.nav_series),
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    print(f"绩效摘要已保存: {summary_path}")


def cmd_status(args):
    """查看数据状态"""
    print("\n" + "=" * 60)
    print("  ETF 策略 - 数据状态")
    print("=" * 60)

    loader = ETFDataLoader()
    db = loader._get_db()

    # ETF 行情
    collection = db["stock_daily_quotes"]
    etf_prefixes = ("51", "15", "56", "58")
    for prefix in etf_prefixes:
        count = collection.count_documents({"symbol": {"$regex": f"^{prefix}"}})
        print(f"  ETF {prefix}xxxx 行情: {count:,} 条")

    # 沪深300
    hs300_count = collection.count_documents({"symbol": "000300"})
    print(f"  沪深300 行情:      {hs300_count:,} 条")

    # 日期范围
    pipeline = [
        {"$match": {"symbol": {"$regex": "^(51|15|56|58)"}}},
        {"$group": {"_id": None, "min_date": {"$min": "$trade_date"}, "max_date": {"$max": "$trade_date"}}},
    ]
    result = list(collection.aggregate(pipeline))
    if result:
        print(f"  日期范围:          {result[0]['min_date']} ~ {result[0]['max_date']}")

    # ETF 基础信息
    basic_info = db["stock_basic_info"]
    etf_info_count = 0
    for prefix in etf_prefixes:
        etf_info_count += basic_info.count_documents({"symbol": {"$regex": f"^{prefix}"}})
    print(f"  ETF 基础信息:      {etf_info_count} 条")

    # 总 ETF 数量
    etf_symbols = collection.distinct("symbol", {"symbol": {"$regex": "^(51|15|56|58)"}})
    print(f"  ETF 代码数:        {len(etf_symbols)}")

    print("=" * 60)


def cmd_init(args):
    """一键初始化：清库 + 全量拉取"""
    print("\n" + "=" * 60)
    print("  ETF 策略 - 一键初始化（清库 + 全量拉取）")
    print("=" * 60)

    if not args.yes:
        confirm = input(
            "\n⚠️  即将执行:\n"
            "  1. 清除 ETF 行情数据（不影响股票数据）\n"
            "  2. 全量拉取 ETF + 沪深300（自 2017-01-01）\n"
            "  此过程可能耗时较长。确认继续？(yes/no): "
        )
        if confirm.lower() != "yes":
            print("已取消")
            return

    # 清库
    loader = ETFDataLoader()
    loader.clear_market_data(clear_etf_only=True)

    # 全量拉取
    stats = loader.full_pull(
        start_date=args.start,
        include_hs300=True,
        batch_delay=args.delay,
        max_workers=args.workers,
    )

    print("\n" + "=" * 60)
    print("  初始化完成:")
    print(f"    ETF 成功/总数: {stats['success_etfs']}/{stats['total_etfs']}")
    print(f"    总数据条数:    {stats['total_records'] + stats['hs300_records']}")
    print("=" * 60)


def cmd_all(args):
    """全流程：清库 → 全量拉取 → 回测"""
    print("\n" + "=" * 60)
    print("  ETF 策略 - 全流程（清库 → 拉取 → 回测）")
    print("=" * 60)

    if not args.yes:
        confirm = input(
            "\n⚠️  即将执行全流程操作，耗时较长。确认继续？(yes/no): "
        )
        if confirm.lower() != "yes":
            print("已取消")
            return

    # 1. 清库
    print("\n[1/3] 清库...")
    loader = ETFDataLoader()
    loader.clear_market_data(clear_etf_only=True)

    # 2. 全量拉取
    print("\n[2/3] 全量拉取...")
    loader.full_pull(
        start_date=args.start,
        include_hs300=True,
        batch_delay=args.delay,
        max_workers=args.workers,
    )

    # 3. 回测
    print("\n[3/3] 回测...")
    cmd_backtest(args)


def main():
    parser = argparse.ArgumentParser(
        description="ETF 底部右侧启动趋势策略 V4.3 Final - 命令行工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # clear
    p_clear = subparsers.add_parser("clear", help="清除行情数据")
    p_clear.add_argument("--etf-only", action="store_true", default=True, help="仅清ETF相关（默认）")
    p_clear.add_argument("--all", action="store_true", help="清除全部行情（含股票）")
    p_clear.add_argument("-y", "--yes", action="store_true", help="跳过确认")

    # pull
    p_pull = subparsers.add_parser("pull", help="全量拉取ETF+沪深300")
    p_pull.add_argument("--start", default="2017-01-01", help="起始日期（默认 2017-01-01）")
    p_pull.add_argument("--end", default=None, help="结束日期（默认今天）")
    p_pull.add_argument("--delay", type=float, default=0.3, help="API调用间隔秒数（默认0.3）")
    p_pull.add_argument("--workers", type=int, default=16, help="并行线程数（默认16，pytdx高并发）")

    # update
    p_update = subparsers.add_parser("update", help="增量更新最近N个交易日")
    p_update.add_argument("--days", type=int, default=10, help="交易日天数（默认10）")
    p_update.add_argument("--workers", type=int, default=16, help="并行线程数（默认16）")

    # backtest
    p_bt = subparsers.add_parser("backtest", help="运行回测")
    p_bt.add_argument("--end", default=None, help="回测结束日期")

    # status
    p_status = subparsers.add_parser("status", help="查看数据状态")

    # init
    p_init = subparsers.add_parser("init", help="一键初始化（清库+全量拉取）")
    p_init.add_argument("--start", default="2017-01-01", help="起始日期")
    p_init.add_argument("--delay", type=float, default=0.3, help="API调用间隔秒数")
    p_init.add_argument("--workers", type=int, default=16, help="并行线程数（默认16）")
    p_init.add_argument("-y", "--yes", action="store_true", help="跳过确认")

    # all
    p_all = subparsers.add_parser("all", help="全流程（清库→拉取→回测）")
    p_all.add_argument("--start", default="2017-01-01", help="起始日期")
    p_all.add_argument("--delay", type=float, default=0.3, help="API调用间隔秒数")
    p_all.add_argument("--end", default=None, help="回测结束日期")
    p_all.add_argument("--workers", type=int, default=16, help="并行线程数（默认16）")
    p_all.add_argument("-y", "--yes", action="store_true", help="跳过确认")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    setup_logging(args.verbose)

    # 处理 --all 标志
    if hasattr(args, "all") and args.all:
        args.etf_only = False

    commands = {
        "clear": cmd_clear,
        "pull": cmd_pull,
        "update": cmd_update,
        "backtest": cmd_backtest,
        "status": cmd_status,
        "init": cmd_init,
        "all": cmd_all,
    }

    func = commands.get(args.command)
    if func:
        func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
