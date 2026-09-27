#!/usr/bin/env python3
"""
板块趋势监控页面

数据: 本地MongoDB全市场日线(含沪深300) + 东财行业映射(当日缓存)
板块指数: 成分股总市值加权日收益累计, 口径对齐沪深300
趋势判断: MA20/MA60双均线状态机(与中期趋势策略V2市场过滤一致)
  - 趋势段: MA20上穿MA60起=向上段, 下穿起=向下段, 展示当前段持续天数/涨跌幅
  - 状态: 多头(价>MA20&MA60&超额20日>0) / 反弹 / 回调 / 空头
  - 阶段: 生命周期四阶段(①萌芽/②强趋势/③衰竭/④反转), 由段方向+状态+广度组合判定
  - 广度: 成分股收盘价站上自身MA20的占比
视图: 表格(含阶段/广度列) / 走线图(对比走线+分格走线两种形式)
周期: 日线 / 周线(周五收盘重采样)
关注: MongoDB system_configs (config_name=sector_watchlist) 持久化
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pymongo import MongoClient

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from dotenv import load_dotenv

load_dotenv(Path(project_root) / ".env", override=True)

NDAYS = 300
CACHE_TTL = 3600
FS_STOCK = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
INDEXES = ("000300", "000905", "000016", "000001", "399001", "399006")

STATE_ORDER = ["多头", "反弹", "回调", "空头"]
STATE_COLORS = {"多头": "#1DC981", "反弹": "#EFAA17", "回调": "#A9AEFF", "空头": "#E8463A"}
STAGE_ORDER = ["①萌芽", "②强趋势", "③衰竭", "④反转"]
STAGE_COLORS = {"①萌芽": "#EFAA17", "②强趋势": "#1DC981",
                "③衰竭": "#9B51E0", "④反转": "#E8463A"}


def _mongo():
    url = os.getenv("MONGODB_URL",
                    "mongodb://admin:tradingagents123@localhost:27017/tradingagentscn?authSource=admin")
    return MongoClient(url, serverSelectionTimeoutMS=5000)


# ---------------- 数据层 ----------------

@st.cache_data(ttl=CACHE_TTL, show_spinner="拉取行业映射(东财)...")
def load_industry_map():
    """股票->东财行业映射, 当日CSV缓存, 拉取时绕过系统代理"""
    cache = Path(project_root) / "logs/sector_trend" / f"industry_map_{time.strftime('%Y%m%d')}.csv"
    if cache.exists():
        df = pd.read_csv(cache, dtype={"code": str})
        if len(df) > 5000:
            return df
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
            r = s.get("https://push2delay.eastmoney.com/api/qt/clist/get", params={
                "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f12", "fs": FS_STOCK, "fields": "f12,f14,f100,f20"}, timeout=15)
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


@st.cache_data(ttl=CACHE_TTL, show_spinner="加载全市场行情(约10-30秒, 每小时缓存一次)...")
def load_market_data(ndays=300):
    """MongoDB全市场close/amount矩阵 + 沪深300序列
    日线300天够MA60+120日收益; 周线900天(约120周)够周线MA60"""
    client = _mongo()
    db = client["tradingagentscn"]
    q = db["stock_daily_quotes"]
    dates = [d["trade_date"] for d in q.find(
        {"symbol": "000300"}, {"trade_date": 1}).sort("trade_date", -1).limit(ndays)]
    dates = sorted(dates)
    cur = q.find({"trade_date": {"$gte": dates[0]}},
                 {"_id": 0, "symbol": 1, "trade_date": 1, "close": 1, "amount": 1})
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


@st.cache_data(ttl=CACHE_TTL, show_spinner="构建板块指数...")
def build_sectors(ndays=300):
    """返回: 各行业市值加权指数(dict{industry: Series}) + 行业->成分信息"""
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


# ---------------- 趋势层 ----------------

def trend_segments(idx: pd.Series, fast=20, slow=60):
    """MA20/MA60双均线分段: [(start_i, end_i, up)]"""
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
    """均线四象限: 多头(双上)/反弹(上MA20下MA60)/回调(下MA20上MA60)/空头(双下)
    超额收益单独展示, 不混入状态判定"""
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
    """趋势生命周期四阶段(与趋势段/状态/广度组合判定):
    ①萌芽: 段↓但价格先站回MA20 -> 均线结构未修复, 等金叉确认
    ②强趋势: 段↑+价格多头+广度>=60% -> 普涨主升, 持有区
    ③衰竭: 段↑但价格走弱或广度掉队 -> 背离警戒, 收缩区
    ④反转: 段↓+价破MA20 -> 趋势已反转, 回避区"""
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
    """5/20/60/120期收益符号 -> 箭头串 + 趋势结构标签 (死区0.8%防噪音)"""
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


@st.cache_data(ttl=CACHE_TTL, show_spinner="计算板块趋势指标...")
def build_sector_table(freq="D"):
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
    amt_ma20 = (amt_r.rolling(20).mean().iloc[-1]
                if len(amt_r) >= 20 else None)
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
        ma120_last = (idx_r.rolling(120).mean().iloc[-1]
                      if len(idx_r) >= 120 else None)
        ma60_slope_down = bool(len(ma60_ind) >= 11
                               and ma60_ind.iloc[-1] < ma60_ind.iloc[-11])
        state = classify_state(idx_r)
        codes = [c for c in members[ind].index if c in px_r.columns]
        breadth = (None if not codes else
                   float((last_px[codes] > ma20_all[codes]).mean()))
        b60 = (None if not codes else
               float((last_px[codes] > ma60_all[codes]).mean()))
        up_ratio = (None if not codes else float(up20_stk[codes].mean()))
        newhi = (None if not codes else
                 float((last_px[codes] >= hi20_all[codes]).mean()))
        vol_pct = None
        if codes and amt_ma20 is not None:
            vol_pct = float((last_amt[codes] > 1.2 * amt_ma20[codes]).mean())
        stage = classify_stage(cur["up"], state, breadth)

        # 量能比: 板块成交额近20期均值/近60期均值 (>1=放量)
        volr = None
        if codes and len(amt_r) >= 60:
            sa = amt_r[codes].sum(axis=1, min_count=1)
            m60 = sa.tail(60).mean()
            if m60 and m60 > 0:
                volr = float(sa.tail(20).mean() / m60)

        # 扩散: 大/小市值半区20期涨幅中位数对比
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

        # 动能形态: 近端vs中段超额
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

        # 反转确认度(仅萌芽期): 价格/超额/广度/量能/均线斜率/近端动量
        confirm = None
        ctype = ""
        if stage == "①萌芽":
            confirm = (20 * bool(above)
                       + 20 * (e20v > 0)
                       + 20 * ((breadth or 0) >= 0.5)
                       + 15 * ((volr or 0) >= 1.05)
                       + 15 * slope_up
                       + 10 * (e5 > 0))
            ctype = ("趋势反转确认" if (confirm >= 70 and (volr or 0) >= 1.0)
                     else "底部反转" if confirm >= 40 else "弱势反弹")

        # 趋势结构: 5/20/60/120期收益符号
        arrows, tstruct = _trend_structure(r5, r20, r60, r120)

        # 量价关系: 近5期价格方向 × 量能比
        vp = ""
        if r5 is not None and r5 == r5 and volr is not None:
            if r5 > 0.005:
                vp = "放量上涨" if volr >= 1.05 else "缩量上涨"
            elif r5 < -0.005:
                vp = "放量下跌" if volr >= 1.05 else "缩量回调"
            else:
                vp = "放量盘整" if volr >= 1.05 else "缩量盘整"

        # 假反转风险: 六条件计数(短涨中期未修复/均线压制/广度不足/缩量/相对弱/MA60下行)
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

        # 趋势一致性: 价>MA20>MA60>MA120 堆叠程度
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
        bcomp = (0.5 * tbl["广度"].fillna(0)
                 + 0.3 * tbl["MA60广度"].fillna(0)
                 + 0.2 * tbl["上涨比例"].fillna(0))
        tbl["起势分"] = (0.25 * rk(tbl["60日%"].fillna(-99))
                        + 0.20 * rk((tbl["超额20日"].fillna(-99)
                                     + tbl["超额60日"].fillna(-99)) / 2)
                        + 0.20 * rk(tbl["加速度"])
                        + 0.15 * bcomp
                        + 0.10 * rk(tbl["量能比"].fillna(0))
                        + 0.10 * tbl["趋势一致性"].fillna(0)).round(0)
    info = {"hs": hs_r, "dates": dates, "members": members,
            "segs_map": segs_map, "freq": freq}
    return tbl, info


# ---------------- 关注列表 ----------------

def load_watchlist():
    try:
        client = _mongo()
        doc = client["tradingagentscn"]["system_configs"].find_one(
            {"config_name": "sector_watchlist"})
        client.close()
        if doc and doc.get("items"):
            return {it["industry"]: it.get("added_at", "") for it in doc["items"]}
    except Exception:
        pass
    return {}


def save_watchlist(wl: dict):
    client = _mongo()
    client["tradingagentscn"]["system_configs"].update_one(
        {"config_name": "sector_watchlist"},
        {"$set": {
            "config_type": "watchlist",
            "items": [{"industry": k, "added_at": v} for k, v in wl.items()],
            "updated_at": datetime.now(),
        }},
        upsert=True)
    client.close()


# ---------------- 详情图 ----------------

def sector_detail_chart(ind: str, info, stage=None):
    idx_r, segs = info["segs_map"][ind]
    hs_r = info["hs"]
    base_i = idx_r.iloc[0]
    base_h = hs_r.reindex(idx_r.index).ffill().iloc[0]
    fig = go.Figure()
    for sg in segs:
        color = "rgba(29,201,129,0.10)" if sg["up"] else "rgba(138,138,142,0.12)"
        fig.add_vrect(x0=sg["start"], x1=sg["end"], fillcolor=color, line_width=0,
                      layer="below")
    line_color = STAGE_COLORS.get(stage, "#4B3FE3") if stage else "#4B3FE3"
    fig.add_trace(go.Scatter(
        x=idx_r.index, y=idx_r / base_i * 100, name="板块指数",
        line=dict(color=line_color, width=2)))
    hs_al = hs_r.reindex(idx_r.index).ffill()
    fig.add_trace(go.Scatter(
        x=hs_al.index, y=hs_al / base_h * 100, name="沪深300",
        line=dict(color="#A1A1AA", width=1.5, dash="dash")))
    fig.update_layout(
        height=380, margin=dict(l=10, r=10, t=36, b=10),
        yaxis_title="归一化(起点=100)",
        legend=dict(orientation="h", y=1.12),
        hovermode="x unified", dragmode="pan",
        xaxis=dict(rangeslider=dict(visible=True, thickness=0.06)))
    return fig


def member_table(ind: str, info, px=None):
    members = info["members"][ind]
    _, pxf, _, dates = load_market_data()
    last = pxf.iloc[-1]
    r20 = pxf.iloc[-1] / pxf.iloc[-21] - 1
    r60 = pxf.iloc[-1] / pxf.iloc[-61] - 1
    ma20 = pxf.ffill().rolling(20).mean().iloc[-1]
    rows = []
    for c, r in members.iterrows():
        if c not in pxf.columns or pd.isna(last.get(c)):
            continue
        rows.append({
            "代码": c, "名称": r["name"], "总市值(亿)": round(r["mcap"] / 1e8, 1),
            "20日%": round(r20[c] * 100, 1) if pd.notna(r20[c]) else None,
            "60日%": round(r60[c] * 100, 1) if pd.notna(r60[c]) else None,
            "价上MA20": bool(last[c] > ma20[c]) if pd.notna(ma20.get(c)) else False,
        })
    return pd.DataFrame(rows).sort_values("60日%", ascending=False)


# ---------------- 走线图 ----------------

def trend_compare_chart(inds, show, info, window, wl):
    """对比走线图: 一个板块一条归一化曲线, 颜色=生命周期阶段"""
    fig = go.Figure()
    for ind in inds:
        idx_r = info["segs_map"][ind][0]
        s = idx_r if window is None else idx_r.tail(window)
        if len(s) < 2:
            continue
        stage = str(show.loc[show["行业"] == ind, "阶段"].iloc[0])
        label = f"{'⭐' if ind in wl else ''}{ind}·{stage}"
        fig.add_trace(go.Scatter(
            x=s.index, y=s / s.iloc[0] * 100, name=label,
            line=dict(color=STAGE_COLORS.get(stage, "#4B3FE3"), width=2),
            hovertemplate=f"<b>{label}</b><br>%{{x|%Y-%m-%d}}  %{{y:.1f}}<extra></extra>"))
    hsr = info["hs"] if window is None else info["hs"].tail(window)
    if len(hsr) >= 2:
        fig.add_trace(go.Scatter(
            x=hsr.index, y=hsr / hsr.iloc[0] * 100, name="沪深300",
            line=dict(color="#8E8E93", width=3, dash="dot"),
            hovertemplate="<b>沪深300</b><br>%{x|%Y-%m-%d}  %{y:.1f}<extra></extra>"))
    # 四阶段图例代理: 即使当前板块不含某阶段, 图例也完整显示颜色对照
    for stage, color in STAGE_COLORS.items():
        fig.add_trace(go.Scatter(
            x=[None], y=[None], name=stage,
            mode="markers",
            marker=dict(color=color, size=10),
            visible="legendonly"))
    fig.update_layout(
        height=500, margin=dict(l=10, r=10, t=30, b=10),
        yaxis_title="归一化(窗口起点=100)",
        legend=dict(orientation="h", y=1.15, font=dict(size=11)),
        hovermode="closest", hoverdistance=15, dragmode="pan")
    return fig


def trend_grid_chart(inds, show, info, window, wl, unit):
    """分格走线图: 每板块一格, 曲线颜色=阶段, 底色=趋势段方向(绿↑灰↓)"""
    # 分格走线图板块多时分3列
    ncols = 3 if len(inds) > 12 else 2
    nrows = (len(inds) + ncols - 1) // ncols
    titles = []
    for ind in inds:
        rw = show[show["行业"] == ind].iloc[0]
        titles.append(f"{rw['行业']} · {rw['阶段']} · "
                      f"{rw['段方向']}{int(rw['持续'])}{unit} {rw['段收益%']:+.1f}%")
    fig = make_subplots(rows=nrows, cols=ncols, subplot_titles=titles)
    for i, ind in enumerate(inds):
        rw = show[show["行业"] == ind].iloc[0]
        r, c = i // ncols + 1, i % ncols + 1
        idx_r, segs = info["segs_map"][ind]
        s = idx_r if window is None else idx_r.tail(window)
        if len(s) < 2:
            continue
        stage = str(show.loc[show["行业"] == ind, "阶段"].iloc[0])
        lo = s.index[0]
        for sg in segs:
            if sg["end"] < lo:
                continue
            fig.add_shape(
                type="rect",
                x0=sg["start"] if sg["start"] > lo else lo, x1=sg["end"],
                fillcolor=("rgba(29,201,129,0.14)" if sg["up"]
                           else "rgba(138,138,142,0.16)"),
                line_width=0, layer="below", row=r, col=c)
        seg_desc = f"{rw['段方向']}{int(rw['持续'])}{unit} {rw['段收益%']:+.1f}%"
        fig.add_trace(go.Scatter(
            x=s.index, y=s / s.iloc[0] * 100, showlegend=False,
            name=rw["行业"],
            line=dict(color=STAGE_COLORS.get(stage, "#4B3FE3"), width=1.8),
            hovertemplate=f"<b>{rw['行业']}</b> · {stage}<br>{seg_desc}<br>"
                          f"%{{x|%Y-%m-%d}}  %{{y:.1f}}<extra></extra>"),
            row=r, col=c)
        hsr = info["hs"].reindex(s.index).ffill()
        if hsr.notna().any():
            fig.add_trace(go.Scatter(
                x=s.index, y=hsr / hsr.iloc[0] * 100, showlegend=False,
                name="沪深300",
                line=dict(color="#8E8E93", width=1.2, dash="dot"),
                hovertemplate="<b>沪深300</b><br>%{x|%Y-%m-%d}  %{y:.1f}<extra></extra>"),
                row=r, col=c)
    for ann in fig.layout.annotations:
        ann.font = dict(size=11)
    fig.update_layout(
        height=nrows * 215 + 40, margin=dict(l=10, r=10, t=30, b=10),
        showlegend=False, dragmode="pan",
        hovermode="closest", hoverdistance=15)
    return fig


# ---------------- 页面 ----------------

def render_sector_trend():
    st.title("📊 板块趋势监控")
    cap = st.caption("板块=东财行业(市值加权指数) | 趋势=MA20/MA60双均线 | 基准=沪深300")

    _t, _r = st.columns([4, 1])
    with _r:
        if st.button("🔄 刷新数据", use_container_width=True,
                     help="清空行情缓存并从MongoDB重新加载(数据更新后立即生效)"):
            load_industry_map.clear()
            load_market_data.clear()
            build_sectors.clear()
            build_sector_table.clear()
            st.rerun()

    freq = st.radio("周期", ["日线", "周线"], horizontal=True, label_visibility="collapsed")
    fkey = "D" if freq == "日线" else "W"

    tbl, info = build_sector_table(fkey)
    hs_r = info["hs"]
    st.caption(f"数据截至 **{info['dates'][-1]}** | 本页缓存1小时, 数据更新后点右上「🔄 刷新数据」立即生效")

    # 市场状态条
    hs_last = hs_r.iloc[-1]
    hs_ma20 = hs_r.tail(20).mean()
    wl = load_watchlist()
    state_cnt = tbl["状态"].value_counts()
    stage_cnt = tbl["阶段"].value_counts()

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("沪深300", f"{hs_last:.0f}",
                  f"MA20{'上方' if hs_last > hs_ma20 else '下方'}",
                  delta_color="off")
    with c2:
        st.metric("多头板块", int(state_cnt.get("多头", 0)))
    with c3:
        st.metric("②强趋势板块", int(stage_cnt.get("②强趋势", 0)))
    with c4:
        st.metric("跑赢HS300(20日)", int((tbl["超额20日"] > 0).sum()))
    with c5:
        st.metric("关注板块", len(wl))

    st.divider()

    view = st.radio("视图", ["🩺 扫描器", "📊 表格", "📈 走线图"], horizontal=True,
                    label_visibility="collapsed", key="view_mode")

    if view == "🩺 扫描器":
        render_scanner(tbl, info, wl)
        return

    # 一键场景预设: (名称, 状态, 阶段, 段方向, 排序, 假反转过滤)
    # 五预设=状态机完全切分(122板块零重叠零遗漏):
    #   🚀②段↑+多头+广度≥60 | 🌱①段↓+价收复MA20 | 🛡️③段↑+价<MA20>MA60
    #   ⚠️③段↑+走弱/破位 | 📉④段↓+价双下
    PRESETS = [
        ("🚀 主升浪", ["多头"], ["②强趋势"], "↑", "超额20日", None),
        ("🌱 底部起势", ["多头", "反弹"], ["①萌芽"], "↓", "起势分", 60),
        ("🛡️ 强势回调", ["回调"], ["③衰竭"], "↑", "超额60日", None),
        ("⚠️ 趋势衰竭", ["多头", "空头"], ["③衰竭"], "↑", "段收益%", None),
        ("📉 下降趋势", ["空头"], ["④反转"], "↓", "超额20日", None),
    ]
    st.markdown("**一键场景**")
    pcols = st.columns(5)
    for i, (pname, ps, pg, pu, psort, pfake) in enumerate(PRESETS):
        active = st.session_state.get("preset_name") == pname
        if pcols[i].button(pname, use_container_width=True, type=("primary" if active else "secondary"),
                           help=f"状态={','.join(ps)} · 阶段={','.join(pg)} · "
                                f"段{'↑' if pu == '↑' else ('↓' if pu == '↓' else '全部')} · "
                                f"排序={psort}" + (f" · 假反转风险<{pfake}" if pfake else "")):
            st.session_state.update(
                preset_name=pname, f_states=ps, f_stages=pg,
                f_up=("全部" if pu is None else ("↑" if pu == "↑" else "↓")),
                f_sort=psort, f_fake=pfake)
            st.rerun()

    # 筛选控件(预设可一键填充, 亦可手动微调)
    fcol1, fcol2, fcol3, fcol4 = st.columns([1.6, 1.8, 1.3, 1.3])
    with fcol1:
        states = st.multiselect("状态筛选 (组内OR)", STATE_ORDER,
                                default=["多头", "反弹"], key="f_states")
    with fcol2:
        stages_f = st.multiselect("阶段筛选 (组内OR)", STAGE_ORDER,
                                  default=STAGE_ORDER, key="f_stages")
    with fcol3:
        sort_opts = ["超额20日", "超额60日", "起势分", "持续",
                     "60日%", "120日%", "家数", "段收益%"]
        sort_by = st.selectbox("排序", sort_opts,
                               index=0, key="f_sort")
    with fcol4:
        seg_opts = ["↑", "↓", "全部"]
        seg_sel = st.selectbox("段方向", seg_opts, index=2, key="f_up")
        st.caption(f"数据截至 {info['dates'][-1]} | {freq}线")
    fake_cap = st.session_state.get("f_fake")
    if fake_cap:
        st.caption(f"🌱 预设已启用: 仅显示假反转风险 < {fake_cap} 的板块 (回测: 低风险组20日胜率59.3% vs 高风险组48.1%)")
    st.caption("筛选逻辑: 同组多选=任一满足(OR) · 状态×阶段=同时满足(AND) · "
               "阶段全选=不筛阶段 · 顶部一键场景可快速配置全部条件")

    show = tbl[tbl["状态"].isin(states) & tbl["阶段"].isin(stages_f)].copy()
    if seg_sel == "↑":
        show = show[show["段方向"] == "↑"]
    elif seg_sel == "↓":
        show = show[show["段方向"] == "↓"]
    if fake_cap is not None:
        fake_col = show.get("假反转风险")
        if fake_col is not None:
            show = show[pd.isna(fake_col) | (fake_col < fake_cap)]
    show = show.sort_values(sort_by, ascending=False)
    show.insert(0, "⭐", show["行业"].map(lambda x: "⭐" if x in wl else ""))

    if view == "📊 表格":
        st.subheader(f"板块榜单 ({len(show)}个)")
        event = st.dataframe(
            show.reset_index(drop=True),
            width='stretch', height=430, hide_index=True,
            on_select="rerun", selection_mode="single-row", key="sector_tbl",
            column_config={
                "5日%": st.column_config.NumberColumn(format="%.1f"),
                "20日%": st.column_config.NumberColumn(format="%.1f"),
                "60日%": st.column_config.NumberColumn(format="%.1f"),
                "120日%": st.column_config.NumberColumn(format="%.1f"),
                "超额5日": st.column_config.NumberColumn(format="%.1f"),
                "超额20日": st.column_config.NumberColumn(format="%.1f"),
                "超额60日": st.column_config.NumberColumn(format="%.1f"),
                "广度": st.column_config.ProgressColumn(
                    "广度(价上MA20)", min_value=0, max_value=100, format="%.0f%%"),
                "MA60广度": st.column_config.ProgressColumn(
                    "MA60广度", min_value=0, max_value=100, format="%.0f%%"),
                "起势分": st.column_config.ProgressColumn(
                    "起势分", min_value=0, max_value=100, format="%.0f"),
                "反转确认度": st.column_config.ProgressColumn(
                    "反转确认度", min_value=0, max_value=100, format="%.0f"),
                "量能比": st.column_config.NumberColumn(format="%.2f"),
                "龙头20日%": st.column_config.NumberColumn(format="%.1f"),
                "跟随20日%": st.column_config.NumberColumn(format="%.1f"),
                "段收益%": st.column_config.NumberColumn(format="%.1f"),
                "上段收益%": st.column_config.NumberColumn(format="%.1f"),
            })

        sel_rows = []
        if event is not None and hasattr(event, "selection") and event.selection:
            sel_rows = list(event.selection.rows or [])
        if sel_rows and sel_rows[0] < len(show):
            _render_detail(show.iloc[sel_rows[0]]["行业"], show, info, wl)
        else:
            st.info("👆 点击榜单任意一行, 查看板块趋势详情")
        return

    # ---- 走线图视图 ----
    if not len(show):
        st.warning("当前筛选条件下没有板块")
        return
    g1, g2, g3 = st.columns([1.4, 1.2, 2.4])
    topn = g1.slider("展示前N个板块", 4, 30, 8, help="按上方排序取前N")
    wopts = ({"近60日": 60, "近120日": 120, "近250日": 250, "全部": None}
             if fkey == "D" else
             {"近26周": 26, "近52周": 52, "近104周": 104, "全部": None})
    win_label = g2.selectbox("走线窗口", list(wopts), index=1)
    form = g3.radio("形式", ["对比走线", "分格走线(带趋势段底色)"],
                    horizontal=True, label_visibility="collapsed")
    inds = show.head(topn)["行业"].tolist()
    unit = "天" if fkey == "D" else "周"
    # 常显图例: 阶段颜色对照(不受当前筛选影响)
    st.markdown(
        f'<span style="font-size:12.5px">阶段图例：'
        f'<span style="color:#EFAA17">●</span> ①萌芽&nbsp;&nbsp;'
        f'<span style="color:#1DC981">●</span> ②强趋势&nbsp;&nbsp;'
        f'<span style="color:#9B51E0">●</span> ③衰竭&nbsp;&nbsp;'
        f'<span style="color:#E8463A">●</span> ④反转</span>'
        f'<br><span style="font-size:11.5px;color:gray">提示: ③衰竭多在榜单中后段; '
        f'④反转的状态是"回调/空头", 默认状态筛选(多头+反弹)会将其隐藏, '
        f'勾选状态筛选可查看</span>',
        unsafe_allow_html=True)
    if form == "对比走线":
        st.plotly_chart(trend_compare_chart(inds, show, info, wopts[win_label], wl),
                        width='stretch')
    else:
        st.plotly_chart(trend_grid_chart(inds, show, info, wopts[win_label], wl, unit),
                        width='stretch')
    st.caption("曲线颜色=生命周期阶段(绿=②强趋势 紫=③衰竭 橙=①萌芽 红=④反转) | "
               "灰色点线=沪深300 | 分格图底色: 绿=向上段, 灰=向下段")
    sel = st.selectbox("选择板块查看详情", inds)
    if sel:
        _render_detail(sel, show, info, wl)


# ---------------- 扫描器 ----------------

@st.cache_data(ttl=6 * 3600)
def load_stage_stats():
    """读取阶段状态机历史回测结果(scripts/sector_stage_backtest.py产出)"""
    p = Path("/Users/fall/code/TradingAgents-CN/logs/sector_trend/stage_stats.json")
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def _scan_table(df, cols, cfg, key, unit):
    return st.dataframe(
        df[cols].reset_index(drop=True), width='stretch', height=330,
        hide_index=True, on_select="rerun", selection_mode="single-row",
        key=key, column_config=cfg)


def render_scanner(tbl, info, wl):
    """板块扫描器: 今日焦点 + 三榜(趋势/起势/风险) + 信号回测验证"""
    st.subheader("板块扫描器: 钱在哪里 · 哪里刚开始 · 哪里正在结束")
    stats = load_stage_stats()
    sbins = (stats or {}).get("score_bins") or {}

    def _bin_wr(score):
        if not sbins or score is None or score != score:
            return None
        for lab, v in sbins.items():
            lo, hi = lab.split("-")
            if float(lo) <= score <= float(hi):
                return v.get("win20")
        return None

    # 今日重点关注: 强趋势/萌芽 按起势分+反转确认度加权取前3
    focus = tbl[tbl["阶段"].isin(["②强趋势", "①萌芽"])].copy()
    if len(focus):
        focus["_k"] = focus["起势分"] + focus["反转确认度"].fillna(0) * 0.3
        focus = focus.nlargest(3, "_k")
        st.markdown("**⭐ 今日重点关注**")
        fc = st.columns(3)
        for i, (_, r) in enumerate(focus.iterrows()):
            with fc[i]:
                emoji = {"②强趋势": "🟢", "①萌芽": "🟡"}.get(r["阶段"], "⚪")
                rc = r.get("反转确认度")
                rc_s = f"{rc:.0f}" if rc is not None and rc == rc else "-"
                wr = _bin_wr(r["起势分"])
                wr_s = f"{wr * 100:.0f}%" if wr is not None else "待回测"
                st.markdown(
                    f"{emoji} **{r['行业']}**"
                    f"<span style='color:gray;font-size:12px'> · {r['阶段']}"
                    f" · {r['趋势结构']}</span>",
                    unsafe_allow_html=True)
                st.caption(f"起势分 {r['起势分']:.0f} | 反转确认 {rc_s} | "
                           f"该分位20日历史胜率 {wr_s} | {r['量价关系']}")
        st.divider()

    tabs = st.tabs(["🟢 趋势榜 · 现在最强", "🟡 起势榜 · 刚开始变强",
                    "🔴 风险榜 · 正在衰退"])
    unit = "天" if info["freq"] == "D" else "周"

    def star(df):
        df = df.copy()
        df.insert(0, "⭐", df["行业"].map(lambda x: "⭐" if x in wl else ""))
        return df

    cfg = {
        "起势分": st.column_config.ProgressColumn("起势分", min_value=0,
                                                  max_value=100, format="%.0f"),
        "广度": st.column_config.ProgressColumn("广度", min_value=0, max_value=100,
                                                format="%.0f%%"),
        "MA60广度": st.column_config.ProgressColumn("MA60广度", min_value=0,
                                                    max_value=100, format="%.0f%%"),
        "量能比": st.column_config.NumberColumn(format="%.2f"),
        "超额5日": st.column_config.NumberColumn(format="%.1f"),
        "超额20日": st.column_config.NumberColumn(format="%.1f"),
        "超额60日": st.column_config.NumberColumn(format="%.1f"),
        "龙头20日%": st.column_config.NumberColumn(format="%.1f"),
        "跟随20日%": st.column_config.NumberColumn(format="%.1f"),
    }
    cols = ["⭐", "行业", "起势分", "阶段", "状态", "趋势结构", "段方向", "持续",
            "广度", "MA60广度", "量能比", "量价关系", "超额5日", "超额20日",
            "动能形态"]

    strong = star(tbl[tbl["阶段"] == "②强趋势"]
                  .sort_values("起势分", ascending=False).head(15))
    buds = star(tbl[tbl["阶段"] == "①萌芽"]
                .sort_values("反转确认度", ascending=False).head(15))
    risk = tbl[tbl["阶段"].isin(["③衰竭", "④反转"])].copy()
    risk["_d"] = (risk["动能形态"] == "高位回调").astype(int)
    risk = star(risk.sort_values(["_d", "超额20日"], ascending=False).head(15))

    def _sel(df, ev):
        if ev is not None and hasattr(ev, "selection") and ev.selection:
            rows = list(ev.selection.rows or [])
            if rows and rows[0] < len(df):
                return df.iloc[rows[0]]["行业"]
        return None

    with tabs[0]:
        ev = _scan_table(strong, cols, cfg, "scan_a", unit)
        st.caption("②强趋势板块·按起势分排序 (趋势25%+相对强度20%+加速度20%"
                   "+广度20%+量能15%, 截面百分位合成)")
        s = _sel(strong, ev)
        if s:
            _render_detail(s, tbl, info, wl)
        else:
            st.info("👆 点击任意板块行查看详情")

    with tabs[1]:
        bcols = cols + ["反转确认度", "反转类型", "假反转风险", "扩散",
                        "龙头20日%", "跟随20日%"]
        bcfg = dict(cfg)
        bcfg["反转确认度"] = st.column_config.ProgressColumn(
            "反转确认度", min_value=0, max_value=100, format="%.0f")
        ev = _scan_table(buds, bcols, bcfg, "scan_b", unit)
        st.caption("①萌芽板块·按反转确认度排序 (价格上MA20·超额20日·广度≥50%"
                   "·量能≥1.05·MA20斜率·近端动量 六因子; ≥70且量能≥1.0趋势反转确认/"
                   "≥40底部反弹/<40弱势反弹) | 假反转风险: 六风险条件计数"
                   "(短涨中期跌/均线压制/广度<55%/缩量/相对弱/MA60下行)")
        s = _sel(buds, ev)
        if s:
            _render_detail(s, tbl, info, wl)
        else:
            st.info("👆 点击任意板块行查看详情")

    with tabs[2]:
        ev = _scan_table(risk, cols, cfg, "scan_c", unit)
        st.caption("③衰竭/④反转板块·高位回调形态优先 — 林奇铁律: 趋势结束的"
                   "板块第一波下跌往往不是终点")
        s = _sel(risk, ev)
        if s:
            _render_detail(s, tbl, info, wl)
        else:
            st.info("👆 点击任意板块行查看详情")

    # 阶段回测验证
    stats = load_stage_stats()
    with st.expander("🔬 阶段历史回测验证 — 状态是否为可交易信号"):
        if not stats:
            st.caption("尚未生成: 运行 python scripts/sector_stage_backtest.py "
                       "(扫描2018年至今全市场, 约1-2分钟)")
        else:
            rows = []
            for k, v in stats["fwd20"].items():
                if k == "NA":
                    continue
                r = {"状态": k, "样本": v["n"],
                     "未来20日胜率": f"{v['win_rate'] * 100:.0f}%",
                     "20日平均超额": f"{v['avg_excess'] * 100:+.1f}%"}
                v60 = stats.get("fwd60", {}).get(k)
                if v60:
                    r["未来60日胜率"] = f"{v60['win_rate'] * 100:.0f}%"
                    r["60日平均超额"] = f"{v60['avg_excess'] * 100:+.1f}%"
                rows.append(r)
            st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)
            trows = [{"转移": k, "样本": v["n"], "概率": f"{v['prob'] * 100:.0f}%"}
                     for k, v in stats["transition"].items()]
            if trows:
                st.dataframe(pd.DataFrame(trows), width='stretch', hide_index=True)
            if stats.get("score_bins"):
                srows = [{"起势分区间": k, "样本": v["n"],
                          "20日胜率": f"{v['win20'] * 100:.0f}%",
                          "20日平均超额": f"{v['avg20'] * 100:+.2f}%",
                          "60日胜率": f"{v['win60'] * 100:.0f}%",
                          "60日平均超额": f"{v['avg60'] * 100:+.2f}%"}
                         for k, v in stats["score_bins"].items()]
                st.markdown("**起势分分桶 — 评分是否单调有效**")
                st.dataframe(pd.DataFrame(srows), width='stretch', hide_index=True)
            if stats.get("fake_risk"):
                frows = [{"假反转风险": k, "样本": v["n"],
                          "20日胜率": f"{v['win20'] * 100:.0f}%",
                          "20日平均超额": f"{v['avg20'] * 100:+.2f}%",
                          "60日平均超额": f"{v['avg60'] * 100:+.2f}%"}
                         for k, v in stats["fake_risk"].items()]
                st.markdown("**假反转风险分桶 (弱段·价上样本内) — 能否区分真假起势**")
                st.dataframe(pd.DataFrame(frows), width='stretch', hide_index=True)
            st.caption(f"口径: {stats['window']} | {stats['n_sectors']}个板块 | "
                       f"生成于 {stats['generated']} | 状态=弱段/强段×价上/价下 "
                       f"(萌芽≈弱段·价上, 反转≈弱段·价下, 衰竭含强段·价下) | "
                       f"板块指数=现市值静态加权(存在幸存者偏差)")


def _render_detail(ind, show, info, wl):
    st.divider()
    row = show[show["行业"] == ind].iloc[0]
    hl = st.columns([2.6, 1.1, 1, 1.1])
    with hl[0]:
        st.subheader(f"🎯 {ind}")
        extra = f"{int(row['家数'])}只成分股 | 状态 {row['状态']} | 阶段 {row['阶段']}"
        rc = row.get("反转确认度")
        if rc is not None and rc == rc:
            extra += f" | 反转确认 {int(rc)} ({row.get('反转类型', '')})"
        if row.get("扩散"):
            extra += f" | 扩散: {row['扩散']}"
        st.caption(extra)
    with hl[1]:
        st.metric("当前段", f"{row['段方向']} {int(row['持续'])}"
                  f"{'天' if info['freq'] == 'D' else '周'}",
                  f"{row['段收益%']:+.1f}%")
    with hl[2]:
        score = row.get("起势分")
        st.metric("起势分", f"{score:.0f}" if score is not None and score == score else "-",
                  f"量能比 {row['量能比']:.2f}" if row.get("量能比") is not None else None,
                  delta_color="off")
    with hl[3]:
        if ind in wl:
            if st.button("移出关注", width='stretch', key=f"rm_{ind}"):
                wl.pop(ind)
                save_watchlist(wl)
                st.rerun()
            st.caption(f"关注于 {wl.get(ind, '')}")
        else:
            if st.button("⭐ 开始关注", width='stretch', key=f"add_{ind}",
                         type="primary"):
                wl[ind] = datetime.now().strftime("%Y-%m-%d")
                save_watchlist(wl)
                st.rerun()

    st.plotly_chart(sector_detail_chart(ind, info, stage=row.get("阶段")), width='stretch')

    # 多维广度 + 量价 + 假反转
    mrow = st.columns(6)
    for col, (lab, v) in zip(mrow, [
            ("MA20广度", row.get("广度")), ("MA60广度", row.get("MA60广度")),
            ("20日上涨", row.get("上涨比例")), ("20日新高", row.get("新高比例")),
            ("放量股比", row.get("放量比例"))]):
        col.metric(lab, f"{v:.0f}%" if v is not None and v == v else "-")
    fake = row.get("假反转风险")
    if fake is not None and fake == fake:
        lv = "🟢低" if fake < 34 else ("🟡中" if fake < 67 else "🔴高")
        mrow[5].metric("假反转风险", f"{int(fake)} {lv}")
    else:
        mrow[5].metric("量价关系", row.get("量价关系") or "-")

    with st.expander(f"成分股 ({int(row['家数'])}只, 按60日涨幅排序)"):
        mt = member_table(ind, info)
        st.dataframe(mt.reset_index(drop=True), width='stretch',
                     height=320, hide_index=True,
                     column_config={
                         "20日%": st.column_config.NumberColumn(format="%.1f"),
                         "60日%": st.column_config.NumberColumn(format="%.1f"),
                     })


if __name__ == "__main__":
    render_sector_trend()
