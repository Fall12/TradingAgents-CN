#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ETF 底部右侧启动趋势策略 V4.3 Final - API 路由

提供以下接口:
- 数据管理: 清库 / 全量拉取 / 增量更新 / 数据状态
- 回测: 运行回测 / 查看结果
- 策略配置: 查看策略参数
- 定时更新: 注册 16:30 增量更新定时任务
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List

from fastapi import APIRouter, HTTPException, Depends, Query, BackgroundTasks
from pydantic import BaseModel, Field

from app.routers.auth_db import get_current_user
from app.core.response import ok, fail
from app.core.database import get_mongo_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/etf-strategy", tags=["etf-strategy"])


# ==================== 请求模型 ====================

class ClearDataRequest(BaseModel):
    """清库请求"""
    etf_only: bool = Field(default=True, description="仅清ETF相关数据（默认True，保留股票数据）")


class FullPullRequest(BaseModel):
    """全量拉取请求"""
    start_date: str = Field(default="2017-01-01", description="起始日期")
    end_date: Optional[str] = Field(default=None, description="结束日期（默认今天）")
    batch_delay: float = Field(default=0.3, description="API调用间隔秒数")


class IncrementalUpdateRequest(BaseModel):
    """增量更新请求"""
    days: int = Field(default=10, description="更新最近N个交易日")


class BacktestRequest(BaseModel):
    """回测请求"""
    end_date: Optional[str] = Field(default=None, description="回测结束日期")


class ScheduleUpdateRequest(BaseModel):
    """定时更新任务请求"""
    enabled: bool = Field(default=True, description="是否启用")
    cron: str = Field(default="30 16 * * 1-5", description="CRON表达式（默认交易日16:30）")


# ==================== 辅助函数 ====================

def _get_loader():
    """获取 ETFDataLoader 实例"""
    from tradingagents.strategy.etf_bottom_right.data_loader import ETFDataLoader
    return ETFDataLoader()


def _get_config():
    """获取策略配置"""
    from tradingagents.strategy.etf_bottom_right.config import StrategyConfig
    return StrategyConfig()


# ==================== 数据管理接口 ====================

@router.get("/status")
async def get_data_status(user: dict = Depends(get_current_user)):
    """
    获取 ETF 策略数据状态

    返回 ETF 行情数量、沪深300数量、日期范围等信息
    """
    try:
        db = get_mongo_db()
        collection = db["stock_daily_quotes"]
        etf_prefixes = ("51", "15", "56", "58")

        # 按前缀统计
        prefix_stats = {}
        for prefix in etf_prefixes:
            count = await collection.count_documents({"symbol": {"$regex": f"^{prefix}"}})
            prefix_stats[f"{prefix}xxxx"] = count

        # 沪深300
        hs300_count = await collection.count_documents({"symbol": "000300"})

        # 日期范围
        pipeline = [
            {"$match": {"symbol": {"$regex": "^(51|15|56|58)"}}},
            {"$group": {
                "_id": None,
                "min_date": {"$min": "$trade_date"},
                "max_date": {"$max": "$trade_date"},
            }},
        ]
        date_range = await collection.aggregate(pipeline).to_list(length=1)

        # ETF 基础信息
        basic_info = db["stock_basic_info"]
        etf_info_count = 0
        for prefix in etf_prefixes:
            etf_info_count += await basic_info.count_documents({"symbol": {"$regex": f"^{prefix}"}})

        # 总 ETF 代码数
        etf_symbols = await collection.distinct(
            "symbol", {"symbol": {"$regex": "^(51|15|56|58)"}}
        )

        data = {
            "etf_quotes_by_prefix": prefix_stats,
            "etf_total_quotes": sum(prefix_stats.values()),
            "hs300_quotes": hs300_count,
            "etf_info_count": etf_info_count,
            "etf_code_count": len(etf_symbols),
            "date_range": date_range[0] if date_range else None,
            "checked_at": datetime.now().isoformat(),
        }

        return ok(data=data, message="ETF策略数据状态")
    except Exception as e:
        logger.error(f"获取数据状态失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取数据状态失败: {str(e)}")


@router.post("/data/clear")
async def clear_data(
    request: ClearDataRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """
    清除行情数据（仅清ETF相关或全部行情）

    ⚠️ 此操作不可逆！
    """
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可以执行清库操作")

    try:
        loader = _get_loader()

        # 在后台执行（清库可能耗时）
        def _do_clear():
            loader.clear_market_data(clear_etf_only=request.etf_only)

        background_tasks.add_task(_do_clear)

        return ok(
            data={"etf_only": request.etf_only},
            message=f"清库任务已提交（{'仅ETF相关' if request.etf_only else '全部行情'}）",
        )
    except Exception as e:
        logger.error(f"清库失败: {e}")
        raise HTTPException(status_code=500, detail=f"清库失败: {str(e)}")


@router.post("/data/full-pull")
async def full_pull(
    request: FullPullRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """
    全量拉取 ETF + 沪深300 日线数据

    从指定起始日期拉取全部 ETF 和沪深300指数数据。
    此操作耗时较长，在后台执行。
    """
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可以执行全量拉取")

    try:
        loader = _get_loader()

        # 生成任务ID
        task_id = f"etf_pull_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        def _do_pull():
            stats = loader.full_pull(
                start_date=request.start_date,
                end_date=request.end_date,
                include_hs300=True,
                batch_delay=request.batch_delay,
            )
            logger.info(f"全量拉取完成 {task_id}: {stats}")

        background_tasks.add_task(_do_pull)

        return ok(
            data={
                "task_id": task_id,
                "start_date": request.start_date,
                "end_date": request.end_date or "今天",
            },
            message="全量拉取任务已提交，在后台执行",
        )
    except Exception as e:
        logger.error(f"全量拉取启动失败: {e}")
        raise HTTPException(status_code=500, detail=f"全量拉取启动失败: {str(e)}")


@router.post("/data/update")
async def incremental_update(
    request: IncrementalUpdateRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """
    增量更新最近 N 个交易日的数据（覆盖修正）

    适用于定时任务调用或手动触发。
    """
    try:
        loader = _get_loader()

        def _do_update():
            stats = loader.incremental_update(days=request.days)
            logger.info(f"增量更新完成: {stats}")

        background_tasks.add_task(_do_update)

        return ok(
            data={"days": request.days},
            message=f"增量更新任务已提交（最近{request.days}个交易日）",
        )
    except Exception as e:
        logger.error(f"增量更新启动失败: {e}")
        raise HTTPException(status_code=500, detail=f"增量更新启动失败: {str(e)}")


# ==================== 回测接口 ====================

@router.post("/backtest")
async def run_backtest(
    request: BacktestRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """
    运行 ETF 策略回测

    从 MongoDB 加载数据并执行回测，结果保存到 logs/etf_backtest/。
    此操作耗时较长，在后台执行。
    """
    try:
        from tradingagents.strategy.etf_bottom_right.config import StrategyConfig
        from tradingagents.strategy.etf_bottom_right.backtest import BacktestEngine

        config = StrategyConfig()
        loader = _get_loader(config)

        task_id = f"etf_backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        def _do_backtest():
            import json
            import pandas as pd

            # 加载数据
            etf_data, hs300_df = loader.load_from_mongodb(
                start_date=config.backtest.data_start_date,
                end_date=request.end_date or config.backtest.end_date,
            )
            basic_info = loader.load_etf_basic_info()

            if not etf_data or hs300_df.empty:
                logger.error("回测数据不足，请先执行全量拉取")
                return

            # 运行回测
            engine = BacktestEngine(config)
            result = engine.run(etf_data, hs300_df, basic_info)

            # 保存结果
            from pathlib import Path
            report_dir = Path("logs") / "etf_backtest"
            report_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")

            # 净值曲线
            result.nav_series.to_csv(report_dir / f"nav_{ts}.csv", header=["nav"])

            # 交易记录
            trades_data = [
                {
                    "date": t.date, "symbol": t.symbol, "direction": t.direction,
                    "price": t.price, "shares": t.shares, "amount": t.amount,
                    "commission": t.commission, "slippage_cost": t.slippage_cost,
                    "reason": t.reason,
                }
                for t in result.trades
            ]
            pd.DataFrame(trades_data).to_csv(report_dir / f"trades_{ts}.csv", index=False)

            # 绩效摘要
            summary = {
                "timestamp": ts,
                "metrics": result.metrics,
                "in_sample_metrics": result.in_sample_metrics,
                "out_sample_metrics": result.out_sample_metrics,
                "n_trades": len(result.trades),
                "n_trading_days": len(result.nav_series),
            }
            with open(report_dir / f"summary_{ts}.json", "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

            logger.info(f"回测完成 {task_id}: {summary['metrics']}")

        background_tasks.add_task(_do_backtest)

        return ok(
            data={"task_id": task_id},
            message="回测任务已提交，在后台执行",
        )
    except Exception as e:
        logger.error(f"回测启动失败: {e}")
        raise HTTPException(status_code=500, detail=f"回测启动失败: {str(e)}")


@router.get("/backtest/results")
async def list_backtest_results(
    user: dict = Depends(get_current_user),
    limit: int = Query(default=20, ge=1, le=100),
):
    """
    列出历史回测结果

    返回 logs/etf_backtest/ 下的回测摘要文件列表
    """
    from pathlib import Path
    import json

    report_dir = Path("logs") / "etf_backtest"
    if not report_dir.exists():
        return ok(data=[], message="暂无回测结果")

    summaries = []
    for f in sorted(report_dir.glob("summary_*.json"), reverse=True)[:limit]:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                summary = json.load(fp)
                summary["filename"] = f.name
                summaries.append(summary)
        except Exception:
            continue

    return ok(data=summaries, message=f"找到 {len(summaries)} 个回测结果")


# ==================== 策略配置接口 ====================

@router.get("/config")
async def get_strategy_config(user: dict = Depends(get_current_user)):
    """
    获取策略配置参数（V4.3 Final 冻结版）
    """
    try:
        from dataclasses import asdict
        config = _get_config()
        config_dict = asdict(config)

        return ok(data=config_dict, message="ETF策略配置（V4.3 Final）")
    except Exception as e:
        logger.error(f"获取策略配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取策略配置失败: {str(e)}")


# ==================== 定时任务接口 ====================

@router.post("/schedule/update")
async def schedule_incremental_update(
    request: ScheduleUpdateRequest,
    user: dict = Depends(get_current_user),
):
    """
    管理 ETF 数据增量更新定时任务

    默认 CRON: `30 16 * * 1-5`（工作日 16:30）
    """
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可以管理定时任务")

    try:
        from app.services.scheduler_service import get_scheduler_service
        service = await get_scheduler_service()

        job_id = "etf_strategy_incremental_update"

        if request.enabled:
            # 注册定时任务
            async def _run_update():
                loader = _get_loader()
                loader.incremental_update(days=10)

            # 检查是否已存在
            existing = service.scheduler.get_job(job_id)
            if existing:
                service.scheduler.remove_job(job_id)

            from apscheduler.triggers.cron import CronTrigger
            trigger = CronTrigger.from_crontab(request.cron)
            service.scheduler.add_job(
                _run_update,
                trigger=trigger,
                id=job_id,
                name="ETF策略-增量更新(10日)",
                replace_existing=True,
            )

            return ok(
                data={"job_id": job_id, "cron": request.cron, "enabled": True},
                message=f"定时任务已启用: {request.cron}",
            )
        else:
            # 禁用定时任务
            existing = service.scheduler.get_job(job_id)
            if existing:
                service.scheduler.remove_job(job_id)
                return ok(data={"job_id": job_id, "enabled": False}, message="定时任务已禁用")
            else:
                return ok(data={"job_id": job_id, "enabled": False}, message="定时任务不存在")

    except Exception as e:
        logger.error(f"管理定时任务失败: {e}")
        raise HTTPException(status_code=500, detail=f"管理定时任务失败: {str(e)}")


@router.get("/schedule/status")
async def get_schedule_status(user: dict = Depends(get_current_user)):
    """
    查看定时任务状态
    """
    try:
        from app.services.scheduler_service import get_scheduler_service
        service = await get_scheduler_service()

        job_id = "etf_strategy_incremental_update"
        job = service.scheduler.get_job(job_id)

        if job:
            data = {
                "job_id": job_id,
                "enabled": True,
                "name": job.name,
                "next_run_time": str(job.next_run_time) if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
        else:
            data = {
                "job_id": job_id,
                "enabled": False,
            }

        return ok(data=data, message="定时任务状态")
    except Exception as e:
        logger.error(f"获取定时任务状态失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取定时任务状态失败: {str(e)}")
