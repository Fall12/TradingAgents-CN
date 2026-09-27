"""
策略选股服务：趋势交易（S4 低波动量）/ 价值交易（PE·PB·ROE）

价值：计算前用 WeStock 批量同步 PE/PB/ROE/市值 → 写入 stock_basic_info → 本地筛选。
趋势：WeStock 日线 + S4 策略；本地日线不足时由 WeStock kline 补齐。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from pymongo import UpdateOne

logger = logging.getLogger("webapi")

_FIN_NAME_RE = re.compile(r"银行|证券|保险|信托|期货|太保|人寿|财险|券商|金控|金融")
_ST_NAME_RE = re.compile(r"(^|\*)ST|退$", re.IGNORECASE)

# 同步池：比价值条件更宽，一次/多次 screen 灌入 fundamentals
_SYNC_EXPR = (
    "intersect(["
    "PE_TTM > 0, PE_TTM < 80, "
    "PB > 0, PB < 10, "
    "ROETTM > 0, "
    "TotalMV >= 1000000000"
    "])"
)
_SYNC_LIMIT = 250
# 价值本地筛选
_VALUE_PE_MIN, _VALUE_PE_MAX = 5.0, 25.0
_VALUE_PB_MAX = 3.0
_VALUE_ROE_MIN = 15.0
_VALUE_MV_YI_MIN = 50.0  # 亿元

# 策略结果缓存：价值/趋势均为日线口径，按自然日（上海）缓存 1 天
_CACHE_COLLECTION = "strategy_screen_cache"
_CACHE_TTL = timedelta(days=1)
_TZ_SH = ZoneInfo("Asia/Shanghai")
_FUNDAMENTALS_SYNC_TTL = timedelta(days=1)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_excluded_name(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return True
    if _ST_NAME_RE.search(n.replace(" ", "")):
        return True
    if _FIN_NAME_RE.search(n):
        return True
    return False


def _pure_code(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s.startswith(("sh", "sz", "bj")) and len(s) > 2:
        return s[2:]
    return re.sub(r"\D", "", s) or s


class StrategyScreeningService:
    """趋势 / 价值策略选股"""

    def __init__(self, db=None):
        self.db = db

    def _today_sh(self) -> str:
        return datetime.now(_TZ_SH).strftime("%Y-%m-%d")

    def _cache_key(self, mode: str, limit: int) -> str:
        return f"{mode}:{int(limit)}:{self._today_sh()}"

    def _get_result_cache(self, mode: str, limit: int) -> Optional[Dict[str, Any]]:
        if self.db is None:
            return None
        try:
            doc = self.db[_CACHE_COLLECTION].find_one({"_id": self._cache_key(mode, limit)})
            if not doc:
                return None
            created = doc.get("created_at")
            if created and getattr(created, "tzinfo", None) is None:
                created = created.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if created and now - created.astimezone(timezone.utc) > _CACHE_TTL:
                return None
            payload = doc.get("payload")
            if not isinstance(payload, dict):
                return None
            out = dict(payload)
            out["cached"] = True
            return out
        except Exception as e:
            logger.warning(f"读策略缓存失败: {e}")
            return None

    def _set_result_cache(self, mode: str, limit: int, payload: Dict[str, Any]) -> None:
        if self.db is None or not payload:
            return
        try:
            now = datetime.now(timezone.utc)
            to_store = {k: v for k, v in payload.items() if k != "cached"}
            self.db[_CACHE_COLLECTION].update_one(
                {"_id": self._cache_key(mode, limit)},
                {"$set": {
                    "mode": mode,
                    "limit": int(limit),
                    "day": self._today_sh(),
                    "payload": to_store,
                    "created_at": now,
                    "expire_at": now + _CACHE_TTL,
                }},
                upsert=True,
            )
        except Exception as e:
            logger.warning(f"写策略缓存失败: {e}")

    def _fundamentals_fresh(self) -> bool:
        """basics 里 fundamentals 是否在 1 天内同步过（足够多条）。"""
        if self.db is None:
            return False
        try:
            since = datetime.now(timezone.utc) - _FUNDAMENTALS_SYNC_TTL
            n = self.db.stock_basic_info.count_documents(
                {"fundamentals_synced_at": {"$gte": since}, "roe": {"$gt": 0}},
            )
            return n >= 50
        except Exception:
            return False

    async def scan_value(self, limit: int = 20) -> Dict[str, Any]:
        """价值交易：先同步 fundamentals，再本地按 PE/PB/ROE/市值筛选。"""
        limit = max(1, min(int(limit or 20), 100))
        return await asyncio.to_thread(self._scan_value_sync, limit)

    def _sync_value_fundamentals(self) -> Dict[str, Any]:
        """
        WeStock 条件选股批量拉取 PE/PB/ROE/市值，写入 stock_basic_info。
        多路 orderby 合并，提高覆盖；比逐票 quote 少打 API。
        """
        from tradingagents.dataflows.providers.westock_cli import (
            from_westock_code,
            screen_by_condition,
        )

        if self.db is None:
            return {"synced": 0, "note": "无数据库连接"}

        merged: Dict[str, Dict[str, Any]] = {}
        for orderby in ("ROETTM", "PE_TTM", "TotalMV"):
            try:
                rows = screen_by_condition(
                    _SYNC_EXPR,
                    limit=_SYNC_LIMIT,
                    orderby=orderby,
                    desc=True,
                    market="hs",
                )
            except Exception as e:
                logger.warning(f"同步 fundamentals 失败 orderby={orderby}: {e}")
                continue
            for row in rows or []:
                code = from_westock_code(row.get("code") or "")
                if len(code) != 6 or not code.isdigit():
                    continue
                pe = _safe_float(row.get("PE_TTM") or row.get("pe"))
                pb = _safe_float(row.get("PB") or row.get("pb"))
                roe = _safe_float(row.get("ROETTM") or row.get("roe"))
                mv_yuan = _safe_float(row.get("TotalMV"))
                if pe <= 0 and pb <= 0 and roe <= 0 and mv_yuan <= 0:
                    continue
                prev = merged.get(code) or {}
                merged[code] = {
                    "code": code,
                    "symbol": code,
                    "name": (row.get("name") or prev.get("name") or "").strip(),
                    "pe": pe if pe > 0 else prev.get("pe"),
                    "pb": pb if pb > 0 else prev.get("pb"),
                    "roe": roe if roe > 0 else prev.get("roe"),
                    "total_mv": round(mv_yuan / 1e8, 4) if mv_yuan > 0 else prev.get("total_mv"),
                    "close": _safe_float(row.get("ClosePrice")) or prev.get("close"),
                    "pct_chg": (
                        _safe_float(row.get("ChangePCT"))
                        if row.get("ChangePCT") not in (None, "")
                        else prev.get("pct_chg")
                    ),
                }

        if not merged:
            return {"synced": 0, "note": "WeStock 未返回 fundamentals"}

        now = datetime.now(timezone.utc)
        ops = []
        for code, rec in merged.items():
            fields: Dict[str, Any] = {
                "code": code,
                "symbol": code,
                "source": "westock",
                "fundamentals_source": "westock_screen",
                "fundamentals_synced_at": now,
                "updated_at": now,
            }
            if rec.get("name"):
                fields["name"] = rec["name"]
            if rec.get("pe") and rec["pe"] > 0:
                fields["pe"] = rec["pe"]
                fields["pe_ttm"] = rec["pe"]
            if rec.get("pb") and rec["pb"] > 0:
                fields["pb"] = rec["pb"]
                fields["pb_mrq"] = rec["pb"]
            if rec.get("roe") and rec["roe"] > 0:
                fields["roe"] = rec["roe"]
                fields["roe_ttm"] = rec["roe"]
            if rec.get("total_mv") and rec["total_mv"] > 0:
                fields["total_mv"] = rec["total_mv"]
            if rec.get("close"):
                fields["close"] = rec["close"]
            if rec.get("pct_chg") is not None:
                fields["pct_chg"] = rec["pct_chg"]
            ops.append(
                UpdateOne(
                    {"$or": [{"code": code}, {"symbol": code}]},
                    {"$set": fields},
                    upsert=True,
                )
            )

        res = self.db.stock_basic_info.bulk_write(ops, ordered=False)
        n = int(res.modified_count) + int(res.upserted_count)
        logger.info(f"✅ 价值 fundamentals 已同步: rows={len(ops)} wrote≈{n}")
        return {"synced": len(ops), "wrote": n, "as_of": now.strftime("%Y-%m-%d")}

    def _scan_value_from_db(self, limit: int) -> List[Dict[str, Any]]:
        """从 stock_basic_info 本地筛选价值标的。"""
        if self.db is None:
            return []
        cursor = self.db.stock_basic_info.find(
            {
                "pe": {"$gte": _VALUE_PE_MIN, "$lte": _VALUE_PE_MAX},
                "pb": {"$gt": 0, "$lte": _VALUE_PB_MAX},
                "roe": {"$gte": _VALUE_ROE_MIN},
                "total_mv": {"$gte": _VALUE_MV_YI_MIN},
            },
            {
                "_id": 0,
                "code": 1,
                "symbol": 1,
                "name": 1,
                "pe": 1,
                "pb": 1,
                "roe": 1,
                "total_mv": 1,
                "close": 1,
                "pct_chg": 1,
                "industry": 1,
                "fundamentals_synced_at": 1,
            },
        )
        items: List[Dict[str, Any]] = []
        for doc in cursor:
            name = doc.get("name") or ""
            if _is_excluded_name(name):
                continue
            code = str(doc.get("code") or doc.get("symbol") or "")
            if len(code) != 6:
                continue
            pe = _safe_float(doc.get("pe"))
            pb = _safe_float(doc.get("pb"))
            roe = _safe_float(doc.get("roe"))
            if pe <= 0:
                continue
            score = round(roe / pe, 4)
            items.append({
                "code": code,
                "symbol": code,
                "name": name,
                "market": "A股",
                "pe": round(pe, 2),
                "pb": round(pb, 2),
                "roe": round(roe, 2),
                "total_mv": round(_safe_float(doc.get("total_mv")), 2) or None,
                "close": round(_safe_float(doc.get("close")), 2) or None,
                "pct_chg": round(_safe_float(doc.get("pct_chg")), 2)
                if doc.get("pct_chg") is not None
                else None,
                "industry": doc.get("industry") or "",
                "score": score,
                "mode": "value",
            })
        items.sort(key=lambda x: (-x.get("score", 0), x.get("pe", 99)))
        return items[:limit]

    def _scan_value_sync(self, limit: int) -> Dict[str, Any]:
        cached = self._get_result_cache("value", limit)
        if cached:
            logger.info(f"✅ 价值选股命中缓存 limit={limit}")
            return cached

        if self._fundamentals_fresh():
            sync_meta = {
                "synced": 0,
                "as_of": self._today_sh(),
                "note": "fundamentals 仍在有效期内，跳过同步",
                "skipped": True,
            }
            logger.info("价值选股：跳过 fundamentals 同步（1天内已同步）")
        else:
            sync_meta = self._sync_value_fundamentals()

        items = self._scan_value_from_db(limit)
        note = ""
        if sync_meta.get("skipped"):
            note = ""
        elif sync_meta.get("synced", 0) == 0:
            note = sync_meta.get("note") or "同步 fundamentals 失败，结果可能过期"
        elif not items:
            note = "同步完成，但本地无符合 PE/PB/ROE/市值条件的标的"
        if not items and not note:
            note = "本地无符合 PE/PB/ROE/市值条件的标的"

        result = {
            "mode": "value",
            "label": "价值交易 · PE/PB/ROE",
            "as_of": sync_meta.get("as_of") or self._today_sh(),
            "market_ok": True,
            "market_note": note,
            "synced": sync_meta.get("synced", 0),
            "cached": False,
            "total": len(items),
            "items": items,
        }
        if items:
            self._set_result_cache("value", limit, result)
        return result

    async def scan_trend(self, limit: int = 20) -> Dict[str, Any]:
        """趋势交易：S4 低波动量入场扫描"""
        limit = max(1, min(int(limit or 20), 100))
        return await asyncio.to_thread(self._scan_trend_sync, limit)

    def _candidate_codes(self, max_n: int = 280) -> List[Tuple[str, str]]:
        """候选池：(code, name)，优先高成交额，排除 ST。"""
        codes: List[Tuple[str, str]] = []
        seen = set()

        # 1) 本地行情按成交额
        if self.db is not None:
            try:
                quotes = list(
                    self.db["market_quotes"]
                    .find({}, {"_id": 0, "code": 1, "symbol": 1, "amount": 1})
                    .sort("amount", -1)
                    .limit(max_n * 2)
                )
                name_map = {}
                basic = self.db["stock_basic_info"].find(
                    {}, {"_id": 0, "code": 1, "symbol": 1, "name": 1}
                )
                for b in basic:
                    c = b.get("code") or b.get("symbol")
                    if c:
                        name_map[str(c)] = b.get("name") or ""
                for q in quotes:
                    c = str(q.get("code") or q.get("symbol") or "")
                    if len(c) != 6 or c in seen:
                        continue
                    name = name_map.get(c, "")
                    if _ST_NAME_RE.search(name.replace(" ", "")):
                        continue
                    seen.add(c)
                    codes.append((c, name))
                    if len(codes) >= max_n:
                        return codes
            except Exception as e:
                logger.warning(f"从 market_quotes 取候选失败: {e}")

        # 2) WeStock 均线多头作为补充
        try:
            from tradingagents.dataflows.providers.westock_cli import screen_by_strategy

            for stype in ("ma_long", "today_upbreak_ma60"):
                rows = screen_by_strategy(stype, limit=120)
                for row in rows:
                    c = _pure_code(row.get("code") or "")
                    name = row.get("name") or ""
                    if len(c) != 6 or c in seen:
                        continue
                    if _ST_NAME_RE.search((name or "").replace(" ", "")):
                        continue
                    seen.add(c)
                    codes.append((c, name))
                    if len(codes) >= max_n:
                        return codes
        except Exception as e:
            logger.warning(f"WeStock 策略候选失败: {e}")

        return codes[:max_n]

    def _scan_trend_sync(self, limit: int) -> Dict[str, Any]:
        cached = self._get_result_cache("trend", limit)
        if cached:
            logger.info(f"✅ 趋势选股命中缓存 limit={limit}")
            return cached

        from tradingagents.dataflows.providers.westock_cli import fetch_daily_klines
        from tradingagents.strategy.stock_momentum.low_vol_momentum import (
            LowVolMomentumStrategy,
            STRATEGY_CONFIG,
        )

        candidates = self._candidate_codes(max_n=120)
        if not candidates:
            return {
                "mode": "trend",
                "label": "趋势交易 · S4 低波动量",
                "as_of": None,
                "market_ok": True,
                "market_note": "无候选股票",
                "cached": False,
                "total": 0,
                "items": [],
            }

        name_map = {c: n for c, n in candidates}
        code_list = [c for c, _ in candidates]

        # 沪深300 + 候选日线（指数代码必须用 sh000300，不能按个股规则走 sz）
        klines = fetch_daily_klines(["sh000300"] + code_list, limit=280, fq="qfq")
        hs_bars = klines.get("000300") or klines.get("sh000300") or []
        if len(hs_bars) < 250:
            logger.warning(f"沪深300 K线不足: {len(hs_bars)}")
            return {
                "mode": "trend",
                "label": "趋势交易 · S4 低波动量",
                "as_of": None,
                "market_ok": False,
                "market_note": "沪深300行情不足，无法计算市场过滤",
                "cached": False,
                "total": 0,
                "items": [],
            }

        hs_df = pd.DataFrame(hs_bars)
        hs_df["close"] = hs_df["close"].astype(float)
        hs20 = float(hs_df["close"].pct_change(20).iloc[-1])
        if np.isnan(hs20):
            hs20 = 0.0
        hs_ma250 = float(hs_df["close"].rolling(250).mean().iloc[-1])
        hs_close = float(hs_df["close"].iloc[-1])
        market_ok = (not np.isnan(hs_ma250)) and hs_close > hs_ma250
        as_of = str(hs_df["trade_date"].iloc[-1])

        strategy = LowVolMomentumStrategy()
        hard_stop_pct = float(STRATEGY_CONFIG["exit"]["hard_stop_loss"])
        items: List[Dict[str, Any]] = []

        for code in code_list:
            bars = klines.get(code) or []
            if len(bars) < 250:
                continue
            df = pd.DataFrame(bars)
            for col in ("open", "high", "low", "close", "volume", "amount"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = strategy.calculate_indicators(df)
            r = df.iloc[-1]
            passed, score, reason = strategy.check_entry(r, hs20)
            if not passed:
                continue
            close = float(r["close"])
            atr_pct = float(r["atr_pct"]) if not np.isnan(r["atr_pct"]) else 0
            mom_mid = float(r["mom_mid"]) if not np.isnan(r["mom_mid"]) else 0
            ret_20 = float(r["return_20"]) if not np.isnan(r["return_20"]) else 0
            items.append({
                "code": code,
                "symbol": code,
                "name": name_map.get(code) or code,
                "market": "A股",
                "close": round(close, 2),
                "score": round(float(score), 2),
                "mom_mid": round(mom_mid * 100, 2),
                "excess_20": round((ret_20 - hs20) * 100, 2),
                "atr_pct": round(atr_pct * 100, 2),
                "hard_stop": round(close * (1 - hard_stop_pct), 2),
                "pct_chg": None,
                "mode": "trend",
                "reason": reason,
            })
            if len(df) >= 2:
                prev = float(df.iloc[-2]["close"])
                if prev > 0:
                    items[-1]["pct_chg"] = round((close / prev - 1) * 100, 2)

        items.sort(key=lambda x: -x.get("score", 0))
        items = items[:limit]

        note = ""
        if not market_ok:
            note = "大盘弱于年线（沪深300 < MA250），请谨慎"
        if not items:
            note = note or "当日无符合条件标的"

        result = {
            "mode": "trend",
            "label": "趋势交易 · S4 低波动量",
            "as_of": as_of,
            "market_ok": market_ok,
            "market_note": note,
            "cached": False,
            "total": len(items),
            "items": items,
        }
        # 有结果或明确空结果都缓存，避免同日反复打 API
        self._set_result_cache("trend", limit, result)
        return result


_service: Optional[StrategyScreeningService] = None


def get_strategy_screening_service(db=None) -> StrategyScreeningService:
    global _service
    if _service is None or (db is not None and _service.db is None):
        _service = StrategyScreeningService(db)
    elif db is not None:
        _service.db = db
    return _service
