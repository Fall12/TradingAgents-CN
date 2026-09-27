#!/usr/bin/env python3
"""补全 stock_basic_info.industry：优先 WeStock 申万二级行业成份，Baostock 证监会行业兜底。"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Dict, Tuple

from pymongo import UpdateOne

from tradingagents.dataflows.providers.westock_cli import (
    from_westock_code,
    parse_markdown_tables,
    run_westock,
)


_CSRC_PREFIX = re.compile(r"^[A-Z]\d{2}")


def _clean_csrc_industry(raw: str) -> str:
    """C38电气机械和器材制造业 -> 电气机械和器材制造业"""
    s = (raw or "").strip()
    if not s:
        return ""
    return _CSRC_PREFIX.sub("", s).strip() or s


def _load_westock_industry_map(sleep_s: float = 0.2) -> Dict[str, str]:
    """code -> 申万行业名"""
    ok, out = run_westock(["sector", "ranking", "--kind", "industry"], timeout=90)
    if not ok or not out:
        print(f"westock sector ranking FAIL: {(out or '')[:160]}", flush=True)
        return {}
    boards = parse_markdown_tables(out)
    print(f"westock industry boards={len(boards)}", flush=True)
    mapping: Dict[str, str] = {}
    for i, board in enumerate(boards, 1):
        pt = (board.get("code") or "").strip()
        name = (board.get("name") or "").strip()
        if not pt or not name:
            continue
        ok2, out2 = run_westock(
            ["sector", "constituent", pt, "--limit", "0"],
            timeout=60,
        )
        if not ok2 or not out2:
            print(f"  [{i}/{len(boards)}] {pt} {name} FAIL", flush=True)
            time.sleep(max(sleep_s, 0.5))
            continue
        rows = parse_markdown_tables(out2)
        n = 0
        for row in rows:
            code = from_westock_code(row.get("code") or "")
            if len(code) == 6 and code.isdigit():
                mapping[code] = name
                n += 1
        print(f"  [{i}/{len(boards)}] {name}: {n} stocks (map={len(mapping)})", flush=True)
        time.sleep(sleep_s)
    return mapping


def _load_baostock_industry_map() -> Dict[str, str]:
    """code -> 证监会行业（去前缀）"""
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        print(f"baostock login fail: {lg.error_msg}", flush=True)
        return {}
    rs = bs.query_stock_industry()
    mapping: Dict[str, str] = {}
    while rs.error_code == "0" and rs.next():
        row = dict(zip(rs.fields, rs.get_row_data()))
        raw_code = (row.get("code") or "").strip()  # sh.600000
        ind = _clean_csrc_industry(row.get("industry") or "")
        if not ind:
            continue
        code = raw_code.split(".")[-1] if "." in raw_code else raw_code
        if len(code) == 6 and code.isdigit():
            mapping[code] = ind
    bs.logout()
    print(f"baostock industry map={len(mapping)}", flush=True)
    return mapping


def enrich_industry(*, prefer_westock: bool = True, sleep_s: float = 0.15) -> None:
    from app.core.database import get_mongo_db_sync

    db = get_mongo_db_sync()
    print(f"DB={db.name}", flush=True)

    westock_map: Dict[str, str] = {}
    if prefer_westock:
        try:
            westock_map = _load_westock_industry_map(sleep_s=sleep_s)
        except Exception as e:
            print(f"westock industry load error: {e}", flush=True)

    bao_map = _load_baostock_industry_map()

    # 合并：WeStock 优先（名称更短更好读），缺的用 BaoStock
    merged: Dict[str, Tuple[str, str]] = {}
    for code, ind in bao_map.items():
        merged[code] = (ind, "baostock")
    for code, ind in westock_map.items():
        merged[code] = (ind, "westock_sw")

    print(
        f"merged={len(merged)} westock={len(westock_map)} baostock={len(bao_map)}",
        flush=True,
    )
    if not merged:
        print("NO DATA", flush=True)
        return

    now = datetime.now(timezone.utc)
    ops = []
    for code, (ind, src) in merged.items():
        ops.append(
            UpdateOne(
                {"$or": [{"code": code}, {"symbol": code}]},
                {
                    "$set": {
                        "code": code,
                        "symbol": code,
                        "industry": ind,
                        "industry_source": src,
                        "industry_updated_at": now,
                        "updated_at": now,
                    }
                },
                upsert=False,
            )
        )

    # 分批写，避免一次过大
    wrote = 0
    batch = 1000
    for i in range(0, len(ops), batch):
        res = db.stock_basic_info.bulk_write(ops[i : i + batch], ordered=False)
        wrote += res.modified_count
        print(f"  write {i // batch + 1}: modified≈{res.modified_count}", flush=True)

    nonempty = db.stock_basic_info.count_documents(
        {"industry": {"$type": "string", "$nin": ["", None]}}
    )
    for code in ("600519", "300750", "000001", "688981"):
        d = db.stock_basic_info.find_one(
            {"$or": [{"code": code}, {"symbol": code}]},
            {"_id": 0, "code": 1, "name": 1, "industry": 1, "industry_source": 1},
        )
        print("VERIFY", code, d, flush=True)
    print(f"DONE wrote≈{wrote} industry_nonempty={nonempty}", flush=True)


if __name__ == "__main__":
    enrich_industry()
