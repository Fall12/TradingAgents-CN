"""
从日线按需聚合成周线/月线，并做 1 天缓存。

规则（标准 OHLC）:
- week:  按 ISO 周（周一为起点）分组；open=首根开, high=最高, low=最低, close=末根收
- month: 按 YYYY-MM 分组；同上
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Literal
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger("webapi")

PeriodAgg = Literal["week", "month"]
CACHE_COLLECTION = "kline_resample_cache"
CACHE_TTL = timedelta(days=1)


def _to_dt(s: Any) -> Optional[pd.Timestamp]:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    try:
        return pd.to_datetime(str(s)[:10])
    except Exception:
        return None


def resample_ohlc(daily_items: List[Dict[str, Any]], period: PeriodAgg) -> List[Dict[str, Any]]:
    """把日线 items 聚合成周/月线。daily_items 需含 time/open/high/low/close，可选 volume/amount。"""
    if not daily_items:
        return []

    rows = []
    for it in daily_items:
        dt = _to_dt(it.get("time") or it.get("trade_date") or it.get("date"))
        if dt is None:
            continue
        try:
            o = float(it.get("open") or 0)
            h = float(it.get("high") or 0)
            l = float(it.get("low") or 0)
            c = float(it.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            continue
        rows.append({
            "dt": dt,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": float(it.get("volume") or it.get("vol") or 0),
            "amount": float(it.get("amount") or 0),
        })

    if not rows:
        return []

    df = pd.DataFrame(rows).sort_values("dt")
    if period == "week":
        # 周标签用该周最后一个交易日
        grp = df.set_index("dt").resample("W-FRI")
    else:
        grp = df.set_index("dt").resample("ME")

    out: List[Dict[str, Any]] = []
    for ts, g in grp:
        if g.empty:
            continue
        last_day = g.index.max()
        out.append({
            "time": last_day.strftime("%Y-%m-%d"),
            "open": float(g.iloc[0]["open"]),
            "high": float(g["high"].max()),
            "low": float(g["low"].min()),
            "close": float(g.iloc[-1]["close"]),
            "volume": float(g["volume"].sum()),
            "amount": float(g["amount"].sum()) if "amount" in g.columns else None,
        })
    return out


def cache_key(market: str, code: str, period: str, limit: int) -> str:
    return f"{market}:{code}:{period}:{int(limit)}"


async def get_cached_agg(db, key: str, as_of: str, now: Optional[datetime] = None) -> Optional[List[Dict]]:
    """若缓存未过期且 as_of 一致（或 1 天内），返回 items。"""
    if db is None:
        return None
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    doc = await db[CACHE_COLLECTION].find_one({"_id": key}, {"_id": 0})
    if not doc:
        return None
    created = doc.get("created_at")
    if created and getattr(created, "tzinfo", None) is None:
        created = created.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Asia/Shanghai"))
    if created and now - created > CACHE_TTL:
        return None
    # 日线未更新时允许复用；日线更新则 as_of 变化自动失效
    if doc.get("as_of") and as_of and doc.get("as_of") != as_of:
        # 仍在 TTL 内但日线已更新 -> 失效
        return None
    items = doc.get("items")
    return items if isinstance(items, list) and items else None


async def set_cached_agg(db, key: str, as_of: str, items: List[Dict], now: Optional[datetime] = None) -> None:
    if db is None or not items:
        return
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    try:
        await db[CACHE_COLLECTION].update_one(
            {"_id": key},
            {"$set": {
                "as_of": as_of,
                "items": items,
                "created_at": now,
                "expire_at": now + CACHE_TTL,
            }},
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"写周/月线缓存失败: {e}")


def daily_lookback_days(period: PeriodAgg, limit: int) -> int:
    """为凑够 limit 根周/月线，需要拉多少自然日的日线。"""
    if period == "week":
        return max(limit * 7 + 30, 400)
    return max(limit * 31 + 60, 800)
