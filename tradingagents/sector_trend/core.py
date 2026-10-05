"""
板块趋势核心计算（无 Streamlit 依赖）

供 Web 板块趋势页与股票智能分析共用：
- 东财行业映射 + MongoDB 全市场日线
- 市值加权板块指数、状态/生命周期阶段、起势分等
"""

from __future__ import annotations

import os
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import pandas as pd
from pymongo import MongoClient

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NDAYS = 300
CACHE_TTL = 3600
FS_STOCK = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
INDEXES = ("000300", "000905", "000016", "000001", "399001", "399006")

STATE_ORDER = ["多头", "反弹", "回调", "空头"]
STAGE_ORDER = ["①萌芽", "②强趋势", "③衰竭", "④反转"]

_cache_lock = threading.Lock()
_ttl_cache: Dict[str, Tuple[float, Any]] = {}


def ttl_cache(ttl: int = CACHE_TTL):
    """进程内 TTL 缓存，Web 与分析链路共享。"""

    def deco(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = f"{fn.__module__}.{fn.__name__}:{args!r}:{sorted(kwargs.items())!r}"
            now = time.time()
            with _cache_lock:
                hit = _ttl_cache.get(key)
                if hit and now - hit[0] < ttl:
                    return hit[1]
            value = fn(*args, **kwargs)
            with _cache_lock:
                _ttl_cache[key] = (now, value)
            return value

        wrapper.cache_clear = lambda: _clear_fn_cache(fn)  # type: ignore[attr-defined]
        return wrapper

    return deco


def _clear_fn_cache(fn: Callable) -> None:
    prefix = f"{fn.__module__}.{fn.__name__}:"
    with _cache_lock:
        for k in list(_ttl_cache):
            if k.startswith(prefix):
                _ttl_cache.pop(k, None)


def clear_all_caches() -> None:
    with _cache_lock:
        _ttl_cache.clear()


def _mongo():
    url = os.getenv(
        "MONGODB_URL",
        "mongodb://admin:tradingagents123@localhost:27017/tradingagentscn?authSource=admin",
    )
    return MongoClient(url, serverSelectionTimeoutMS=5000)


@ttl_cache(CACHE_TTL)
def load_industry_map() -> pd.DataFrame:
    """股票->东财行业映射, 当日CSV缓存, 拉取时绕过系统代理"""
    cache_dir = PROJECT_ROOT / "logs/sector_trend"
    cache = cache_dir / f"industry_map_{time.strftime('%Y%m%d')}.csv"
    if cache.exists():
        df = pd.read_csv(cache, dtype={"code": str})
        if len(df) > 5000:
            return df
    old_caches = sorted(cache_dir.glob("industry_map_*.csv"), reverse=True)
    for oc in old_caches:
        try:
            df = pd.read_csv(oc, dtype={"code": str})
            if len(df) > 5000:
                cache_dir.mkdir(parents=True, exist_ok=True)
                df.to_csv(cache, index=False, encoding="utf-8-sig")
                return df
        except Exception:
            continue
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    import requests
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    rows, pn = [], 1
    while True:
        try:
            r = s.get(
                "https://push2delay.eastmoney.com/api/qt/clist/get",
                params={
                    "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                    "fid": "f12", "fs": FS_STOCK, "fields": "f12,f14,f100,f20",
                },
                timeout=15,
            )
            d = (r.json().get("data") or {})
            diff = d.get("diff") or []
        except Exception:
            time.sleep(2)
            continue
        if not diff:
            break
        rows.extend(diff)
        if len(rows) >= (d.get("total") or 0):
            break
        pn += 1
        time.sleep(0.25)
    df = pd.DataFrame([{
        "code": x["f12"], "name": x["f14"], "industry": x["f100"],
        "mcap": pd.to_numeric(x.get("f20"), errors="coerce"),
    } for x in rows]).drop_duplicates("code")
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache, index=False, encoding="utf-8-sig")
    return df


@ttl_cache(CACHE_TTL)
def load_market_data(ndays: int = 300):
    """MongoDB全市场close/amount矩阵 + 沪深300序列"""
    client = _mongo()
    db = client["tradingagentscn"]
    q = db["stock_daily_quotes"]
    dates = [
        d["trade_date"]
        for d in q.find({"symbol": "000300"}, {"trade_date": 1})
        .sort("trade_date", -1)
        .limit(ndays)
    ]
    dates = sorted(dates)
    cur = q.find(
        {"trade_date": {"$gte": dates[0]}},
        {"_id": 0, "symbol": 1, "trade_date": 1, "close": 1, "amount": 1},
    )
    df = pd.DataFrame(list(cur))
    client.close()
    df = df.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    hs = df[df["symbol"] == "000300"].set_index("trade_date")["close"].astype(float)
    hs = hs.reindex(dates)
    stock_df = df[~df["symbol"].isin(INDEXES)]
    px = stock_df.pivot(index="trade_date", columns="symbol", values="close").astype(float)
    px = px.reindex(dates)
    amt = stock_df.pivot(index="trade_date", columns="symbol", values="amount").astype(float)
    amt = amt.reindex(dates)
    return hs, px, amt, dates


def _wret(ret_m, weights):
    w = weights.reindex(ret_m.columns).fillna(1e6)
    v = ret_m.notna() * w
    return (ret_m.fillna(0) * w).sum(axis=1) / v.sum(axis=1)


@ttl_cache(CACHE_TTL)
def build_sectors(ndays: int = 300):
    """返回: 各行业市值加权指数 + 行业->成分信息"""
    imap = load_industry_map()
    hs, px, _, dates = load_market_data(ndays)
    rets = px.pct_change(fill_method=None)
    sectors = {}
    members = {}
    for ind, g in imap.groupby("industry"):
        codes = [c for c in g["code"] if c in px.columns]
        if len(codes) < 5:
            continue
        w = g.set_index("code")["mcap"]
        idx = (1 + _wret(rets[codes], w)).cumprod()
        sectors[ind] = idx
        members[ind] = g.set_index("code").loc[codes]
    return sectors, members, hs, dates


def trend_segments(idx: pd.Series, fast=20, slow=60):
    """MA20/MA60双均线分段"""
    ma_f = idx.rolling(fast).mean()
    ma_s = idx.rolling(slow).mean()
    valid = ma_s.notna()
    up = ma_f > ma_s
    segs = []
    state = start = None
    for i in range(len(idx)):
        if not valid.iloc[i]:
            continue
        cur = bool(up.iloc[i])
        if state is None:
            state, start = cur, i
        elif cur != state:
            segs.append((start, i - 1, state))
            state, start = cur, i
    if state is not None:
        segs.append((start, len(idx) - 1, state))
    out = []
    for s, e, up_ in segs:
        if e <= s:
            continue
        seg_idx = idx.iloc[s:e + 1]
        out.append({
            "start": seg_idx.index[0], "end": seg_idx.index[-1],
            "days": e - s + 1, "up": up_,
            "ret": seg_idx.iloc[-1] / seg_idx.iloc[0] - 1,
        })
    return out


def classify_state(idx):
    last = idx.iloc[-1]
    ma20 = idx.tail(20).mean()
    ma60 = idx.tail(60).mean() if len(idx) >= 60 else None
    a20, a60 = last > ma20, (ma60 is not None and last > ma60)
    if a20 and a60:
        return "多头"
    if a20:
        return "反弹"
    if a60:
        return "回调"
    return "空头"


def classify_stage(seg_up, state, breadth):
    if not seg_up:
        return "①萌芽" if state in ("多头", "反弹") else "④反转"
    if state == "多头" and breadth is not None and breadth >= 0.6:
        return "②强趋势"
    return "③衰竭"


def _window_ret(c, n):
    if len(c) > n and c.iloc[-n - 1] > 0:
        return c.iloc[-1] / c.iloc[-n - 1] - 1
    return None


def _trend_structure(r5, r20, r60, r120):
    def arw(x):
        if x is None or x != x:
            return "→"
        return "↑" if x > 0.008 else ("↓" if x < -0.008 else "→")

    a5, a20, a60, a120 = arw(r5), arw(r20), arw(r60), arw(r120)
    if a60 == "↑" and a120 == "↑":
        label = "主升趋势" if a5 != "↓" else "高位转弱"
    elif a60 == "↑":
        label = "趋势修复" if a5 != "↓" else "高位转弱"
    elif a60 == "↓" and a5 == "↑" and a20 != "↓":
        label = "底部起势"
    elif a60 == "↓" and a5 == "↓" and a20 == "↓":
        label = "持续弱势"
    else:
        label = "震荡"
    return a5 + a20 + a60 + a120, label


@ttl_cache(CACHE_TTL)
def build_sector_table(freq: str = "D"):
    """板块榜单: 行情指标+趋势段+状态+生命周期阶段+起势分/反转确认度"""
    ndays = 300 if freq == "D" else 900
    sectors, members, hs, dates = build_sectors(ndays)
    _, px, amt, _ = load_market_data(ndays)

    def resample(s):
        if freq == "D":
            return s
        t = pd.to_datetime(s.index)
        w = s.copy()
        w.index = t
        return w.resample("W-FRI").last().dropna()

    if freq == "W":
        px_t = px.copy()
        px_t.index = pd.to_datetime(px_t.index)
        px_r = px_t.resample("W-FRI").last().dropna(how="all")
        amt_t = amt.copy()
        amt_t.index = pd.to_datetime(amt_t.index)
        amt_r = amt_t.resample("W-FRI").sum()
    else:
        px_r = px
        amt_r = amt
    last_px = px_r.iloc[-1]
    pxf = px_r.ffill()
    ma20_all = pxf.rolling(20).mean().iloc[-1]
    ma60_all = pxf.rolling(60).mean().iloc[-1]
    hi20_all = px_r.rolling(20).max().iloc[-1]
    r20_stk = px_r.iloc[-1] / px_r.iloc[-21] - 1
    up20_stk = r20_stk > 0
    amt_ma20 = (amt_r.rolling(20).mean().iloc[-1] if len(amt_r) >= 20 else None)
    last_amt = amt_r.iloc[-1] if len(amt_r) else None

    hs_r = resample(hs)

    def hr(n):
        return _window_ret(hs_r, n)

    rows = []
    segs_map = {}
    for ind, idx in sectors.items():
        idx_r = resample(idx)
        if len(idx_r) < 61:
            continue
        segs = trend_segments(idx_r)
        if not segs:
            continue
        segs_map[ind] = (idx_r, segs)
        r20, r60, r120 = _window_ret(idx_r, 20), _window_ret(idx_r, 60), _window_ret(idx_r, 120)
        r5 = _window_ret(idx_r, 5)
        ex20 = None if r20 is None else r20 - hr(20)
        ex60 = None if r60 is None else r60 - hr(60)
        ex5 = None if r5 is None else r5 - hr(5)
        cur = segs[-1]
        prev = segs[-2] if len(segs) > 1 else None
        last_px_ind = idx_r.iloc[-1]
        ma20_ind = idx_r.rolling(20).mean()
        above = (idx_r > ma20_ind).iloc[-1]
        slope_up = bool(ma20_ind.iloc[-1] > ma20_ind.iloc[-6])
        ma60_ind = idx_r.rolling(60).mean()
        ma120_last = (idx_r.rolling(120).mean().iloc[-1] if len(idx_r) >= 120 else None)
        ma60_slope_down = bool(len(ma60_ind) >= 11 and ma60_ind.iloc[-1] < ma60_ind.iloc[-11])
        state = classify_state(idx_r)
        codes = [c for c in members[ind].index if c in px_r.columns]
        breadth = (None if not codes else float((last_px[codes] > ma20_all[codes]).mean()))
        b60 = (None if not codes else float((last_px[codes] > ma60_all[codes]).mean()))
        up_ratio = (None if not codes else float(up20_stk[codes].mean()))
        newhi = (None if not codes else float((last_px[codes] >= hi20_all[codes]).mean()))
        vol_pct = None
        if codes and amt_ma20 is not None:
            vol_pct = float((last_amt[codes] > 1.2 * amt_ma20[codes]).mean())
        stage = classify_stage(cur["up"], state, breadth)

        volr = None
        if codes and len(amt_r) >= 60:
            sa = amt_r[codes].sum(axis=1, min_count=1)
            m60 = sa.tail(60).mean()
            if m60 and m60 > 0:
                volr = float(sa.tail(20).mean() / m60)

        lead20 = lag20 = None
        spread_lab = ""
        if codes:
            mc = members[ind].loc[codes, "mcap"]
            med = mc.median()
            lead_c = [c for c in codes if mc.get(c, 0) >= med]
            lag_c = [c for c in codes if mc.get(c, 0) < med]
            if lead_c:
                lead20 = float(r20_stk[lead_c].median())
            if lag_c:
                lag20 = float(r20_stk[lag_c].median())
            if lead20 is not None and lag20 is not None:
                if lead20 > 0 and lag20 > 0:
                    spread_lab = ("普涨扩散" if lag20 >= lead20 * 0.6 else "龙头拉动")
                elif lead20 > 0:
                    spread_lab = "龙头独涨"
                else:
                    spread_lab = "普跌"

        e5 = ex5 if ex5 is not None else 0.0
        e20v = ex20 if ex20 is not None else 0.0
        if e5 > 0 and e20v > 0:
            shape = "持续强"
        elif e5 > 0:
            shape = "刚启动"
        elif e20v > 0:
            shape = "高位回调"
        else:
            shape = "持续弱"

        confirm = None
        ctype = ""
        if stage == "①萌芽":
            confirm = (
                20 * bool(above)
                + 20 * (e20v > 0)
                + 20 * ((breadth or 0) >= 0.5)
                + 15 * ((volr or 0) >= 1.05)
                + 15 * slope_up
                + 10 * (e5 > 0)
            )
            ctype = (
                "趋势反转确认" if (confirm >= 70 and (volr or 0) >= 1.0)
                else "底部反转" if confirm >= 40 else "弱势反弹"
            )

        arrows, tstruct = _trend_structure(r5, r20, r60, r120)

        vp = ""
        if r5 is not None and r5 == r5 and volr is not None:
            if r5 > 0.005:
                vp = "放量上涨" if volr >= 1.05 else "缩量上涨"
            elif r5 < -0.005:
                vp = "放量下跌" if volr >= 1.05 else "缩量回调"
            else:
                vp = "放量盘整" if volr >= 1.05 else "缩量盘整"

        fake = None
        if stage in ("①萌芽", "④反转"):
            conds = [
                bool(r20 is not None and r20 > 0 and r60 is not None and r60 < 0),
                not cur["up"],
                (breadth or 0) < 0.55,
                (volr or 0) < 1.0,
                bool((ex20 is None or ex20 <= 0) or (ex5 is not None and ex5 <= 0)),
                ma60_slope_down,
            ]
            fake = int(round(100 * sum(bool(c) for c in conds) / 6))

        cons_pts = int(bool(above)) + int(ma20_ind.iloc[-1] > ma60_ind.iloc[-1])
        cons_n = 2
        if ma120_last is not None:
            cons_pts += int(ma60_ind.iloc[-1] > ma120_last)
            cons_n += 1
        consistency = round(100 * cons_pts / cons_n)

        rows.append({
            "行业": ind,
            "家数": len(members[ind]),
            "5日%": None if r5 is None else round(r5 * 100, 2),
            "20日%": None if r20 is None else round(r20 * 100, 2),
            "60日%": None if r60 is None else round(r60 * 100, 2),
            "120日%": None if r120 is None else round(r120 * 100, 2),
            "超额5日": None if ex5 is None else round(ex5 * 100, 2),
            "超额20日": None if ex20 is None else round(ex20 * 100, 2),
            "超额60日": None if ex60 is None else round(ex60 * 100, 2),
            "状态": state,
            "阶段": stage,
            "广度": None if breadth is None else round(breadth * 100),
            "量能比": None if volr is None else round(volr, 2),
            "动能形态": shape,
            "反转确认度": confirm,
            "反转类型": ctype,
            "龙头20日%": None if lead20 is None else round(lead20 * 100, 1),
            "跟随20日%": None if lag20 is None else round(lag20 * 100, 1),
            "扩散": spread_lab,
            "段方向": "↑" if cur["up"] else "↓",
            "持续": cur["days"],
            "段收益%": round(cur["ret"] * 100, 1),
            "上段方向": ("↑" if prev["up"] else "↓") if prev else "",
            "上段持续": prev["days"] if prev else 0,
            "上段收益%": round(prev["ret"] * 100, 1) if prev else 0.0,
            "MA60广度": None if b60 is None else round(b60 * 100),
            "上涨比例": None if up_ratio is None else round(up_ratio * 100),
            "新高比例": None if newhi is None else round(newhi * 100),
            "放量比例": None if vol_pct is None else round(vol_pct * 100),
            "趋势结构": f"{tstruct} {arrows}",
            "量价关系": vp,
            "假反转风险": fake,
            "趋势一致性": consistency,
            "创新高": bool(idx_r.tail(60).max() <= last_px_ind),
            "价上MA20": bool(above),
        })
    tbl = pd.DataFrame(rows)
    if len(tbl):
        tbl["加速度"] = tbl["超额5日"].fillna(0) - tbl["超额20日"].fillna(0) / 4
        rk = lambda s: s.rank(pct=True) * 100
        bcomp = (
            0.5 * tbl["广度"].fillna(0)
            + 0.3 * tbl["MA60广度"].fillna(0)
            + 0.2 * tbl["上涨比例"].fillna(0)
        )
        tbl["起势分"] = (
            0.25 * rk(tbl["60日%"].fillna(-99))
            + 0.20 * rk((tbl["超额20日"].fillna(-99) + tbl["超额60日"].fillna(-99)) / 2)
            + 0.20 * rk(tbl["加速度"])
            + 0.15 * bcomp
            + 0.10 * rk(tbl["量能比"].fillna(0))
            + 0.10 * tbl["趋势一致性"].fillna(0)
        ).round(0)
    info = {
        "hs": hs_r, "dates": dates, "members": members,
        "segs_map": segs_map, "freq": freq,
    }
    return tbl, info


def normalize_ashare_code(ticker: str) -> Optional[str]:
    import re
    if not ticker:
        return None
    t = str(ticker).strip().upper()
    m = re.search(r"(\d{6})", t)
    return m.group(1) if m else None


def resolve_industry_for_symbol(symbol: str) -> Optional[str]:
    """优先东财行业映射（与板块趋势页一致），否则回退 basics.industry。"""
    code = normalize_ashare_code(symbol)
    if not code:
        return None
    try:
        imap = load_industry_map()
        hit = imap.loc[imap["code"] == code, "industry"]
        if len(hit):
            ind = str(hit.iloc[0]).strip()
            if ind and ind.lower() != "nan":
                return ind
    except Exception:
        pass
    try:
        client = _mongo()
        db = client["tradingagentscn"]
        doc = db["stock_basic_info"].find_one(
            {"$or": [{"code": code}, {"symbol": code}]},
            {"industry": 1, "industry_name": 1},
        )
        client.close()
        if doc:
            ind = (doc.get("industry") or doc.get("industry_name") or "").strip()
            if ind and ind not in {"主板", "中小板", "创业板", "科创板"}:
                return ind
    except Exception:
        pass
    return None


def get_sector_row_for_symbol(symbol: str, freq: str = "D") -> Optional[dict]:
    """返回个股所属板块在榜单中的一行（dict）；找不到则 None。"""
    industry = resolve_industry_for_symbol(symbol)
    if not industry:
        return None
    tbl, _ = build_sector_table(freq)
    if tbl is None or len(tbl) == 0:
        return None
    hit = tbl[tbl["行业"] == industry]
    if len(hit) == 0:
        # 宽松匹配：行业名互相包含
        mask = tbl["行业"].astype(str).apply(
            lambda x: industry in x or x in industry
        )
        hit = tbl[mask]
    if len(hit) == 0:
        return {"行业": industry, "_missing_metrics": True}
    row = hit.iloc[0].to_dict()
    row["_missing_metrics"] = False
    return row


def stock_vs_sector_excess(symbol: str, freq: str = "D") -> Optional[dict]:
    """个股相对所属板块的 20/60 日超额（百分点）。"""
    code = normalize_ashare_code(symbol)
    industry = resolve_industry_for_symbol(symbol)
    if not code or not industry:
        return None
    ndays = 300 if freq == "D" else 900
    try:
        sectors, _, _, _ = build_sectors(ndays)
        _, px, _, _ = load_market_data(ndays)
    except Exception:
        return None
    if industry not in sectors or code not in px.columns:
        return None
    idx = sectors[industry]
    stk = px[code].dropna()
    if freq == "W":
        idx = idx.copy()
        idx.index = pd.to_datetime(idx.index)
        idx = idx.resample("W-FRI").last().dropna()
        stk = stk.copy()
        stk.index = pd.to_datetime(stk.index)
        stk = stk.resample("W-FRI").last().dropna()
    out = {}
    for n, key in ((20, "相对板块20日"), (60, "相对板块60日")):
        if len(stk) > n and len(idx) > n and stk.iloc[-n - 1] > 0 and idx.iloc[-n - 1] > 0:
            sr = stk.iloc[-1] / stk.iloc[-n - 1] - 1
            ir = idx.iloc[-1] / idx.iloc[-n - 1] - 1
            out[key] = round((sr - ir) * 100, 2)
    return out or None
