"""
WeStock CLI 适配层：把 westock 命令封装成可供分析工具 / 本地 CLI 调用的结果。

依赖本机已安装的 `westock`（默认 ~/.local/bin/westock）。
用于港股/A股新闻、公告、南下持仓、资金流等实时补充，避免分析师空转。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 25


def find_westock_bin() -> Optional[str]:
    env_bin = os.getenv("WESTOCK_BIN")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    which = shutil.which("westock")
    if which:
        return which
    # 常见本机安装路径
    for p in (
        os.path.expanduser("~/.local/bin/westock"),
        "/usr/local/bin/westock",
        "/opt/homebrew/bin/westock",
    ):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def to_westock_code(ticker: str, market_hint: str = "") -> str:
    """把项目内代码转成 westock 前缀格式：sh/sz/hk/us。"""
    raw = (ticker or "").strip().upper()
    hint = (market_hint or "").upper()

    if raw.startswith(("SH", "SZ", "HK", "US")) and len(raw) > 2 and raw[2:].isdigit():
        # 已是 sh600519 / hk01211 形态（大小写不一）
        prefix = raw[:2].lower()
        return f"{prefix}{raw[2:]}"

    # 01211.HK / 0700.HK
    m = re.match(r"^0*(\d{1,5})\.HK$", raw)
    if m or "HK" in hint or "港" in hint:
        digits = m.group(1) if m else re.sub(r"\D", "", raw)
        if digits:
            return f"hk{digits.zfill(5)}"

    # 美股 ticker
    if re.match(r"^[A-Z]{1,5}$", raw) and ("US" in hint or "美" in hint or not raw.isdigit()):
        if not raw.isdigit():
            return f"us{raw}"

    # A股 6 位
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 6:
        if digits.startswith(("5", "6", "9")):
            return f"sh{digits}"
        return f"sz{digits}"

    # 纯数字港股（4~5 位）
    if digits.isdigit() and 1 <= len(digits) <= 5:
        return f"hk{digits.zfill(5)}"

    return raw.lower()


def run_westock(args: List[str], timeout: int = _DEFAULT_TIMEOUT) -> Tuple[bool, str]:
    bin_path = find_westock_bin()
    if not bin_path:
        return False, "未找到 westock CLI（请安装或设置 WESTOCK_BIN）"

    cmd = [bin_path, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            msg = err or out or f"exit={proc.returncode}"
            logger.warning(f"[westock] 命令失败: {' '.join(cmd)} -> {msg[:200]}")
            return False, msg
        return True, out
    except subprocess.TimeoutExpired:
        return False, f"westock 超时（{timeout}s）: {' '.join(args)}"
    except Exception as e:
        return False, f"westock 执行异常: {e}"


def fetch_news(ticker: str, limit: int = 10, market_hint: str = "") -> str:
    code = to_westock_code(ticker, market_hint)
    ok, out = run_westock(["news", "list", code, "--limit", str(int(limit))])
    if not ok or not out or len(out) < 20:
        return ""
    return f"## WeStock 新闻（{code}）\n\n{out}"


def fetch_notices(ticker: str, limit: int = 5, market_hint: str = "") -> str:
    code = to_westock_code(ticker, market_hint)
    ok, out = run_westock(["notice", "list", code, "--limit", str(int(limit))])
    if not ok or not out or len(out) < 20:
        return ""
    return f"## WeStock 公告（{code}）\n\n{out}"


def fetch_reports(ticker: str, limit: int = 5, market_hint: str = "") -> str:
    code = to_westock_code(ticker, market_hint)
    ok, out = run_westock(["report", "list", code, "--limit", str(int(limit))])
    if not ok or not out or len(out) < 20:
        return ""
    return f"## WeStock 研报（{code}）\n\n{out}"


def fetch_fund_flow(ticker: str, market_hint: str = "") -> str:
    code = to_westock_code(ticker, market_hint)
    ok, out = run_westock(["fund", "flow", code])
    if not ok or not out:
        return ""
    return f"## 资金流向（{code}）\n\n{out}"


def fetch_south_holding(ticker: str, market_hint: str = "HK") -> str:
    code = to_westock_code(ticker, market_hint or "HK")
    if not code.startswith("hk"):
        return ""
    ok, out = run_westock(["fund", "south-holding", code])
    if not ok or not out:
        return ""
    return f"## 港股通南下持仓（{code}）\n\n{out}"


def fetch_short(ticker: str, market_hint: str = "HK") -> str:
    code = to_westock_code(ticker, market_hint)
    ok, out = run_westock(["fund", "short", code])
    if not ok or not out:
        return ""
    return f"## 卖空数据（{code}）\n\n{out}"


def search_stocks(query: str, market_hint: str = "", limit: int = 20) -> List[dict]:
    """
    用 westock search 按名称/代码检索，返回 [{code, name, market, westock_code, type}]。
    market_hint: CN/HK/US 用于过滤；空则不过滤。
    """
    ok, out = run_westock(["search", query])
    if not ok or not out:
        return []

    hint = (market_hint or "").upper()
    rows: List[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # | code | name | type |
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 2:
            continue
        wcode, name = cols[0], cols[1]
        typ = cols[2] if len(cols) > 2 else ""
        if wcode.lower() == "code" or wcode.startswith("---"):
            continue
        wcode_l = wcode.lower()
        if wcode_l.startswith("hk"):
            market = "HK"
            code = wcode_l[2:].zfill(5)
        elif wcode_l.startswith("sh") or wcode_l.startswith("sz") or wcode_l.startswith("bj"):
            market = "CN"
            code = wcode_l[2:]
        elif wcode_l.startswith("us"):
            market = "US"
            code = wcode[2:].upper()
        else:
            continue

        if hint in ("CN", "HK", "US") and market != hint:
            continue
        # 过滤认股证/ADR 冗余时可按 type 再筛；先保留主股
        if market == "HK" and (code.startswith("8") or "-R" in name or "ADR" in name.upper()):
            # 港股通衍生品/R 股靠后
            pass

        rows.append({
            "code": code,
            "name": name,
            "market": market,
            "westock_code": wcode_l,
            "type": typ,
            "source": "westock",
        })
        if len(rows) >= limit * 3:  # 多取一点再排序截断
            break

    # 名称相关度优先，其次主股（非 -R/ADR/8xxx）
    q = (query or "").strip().lower()

    def rank(item: dict) -> tuple:
        name = item.get("name") or ""
        name_l = name.lower()
        code = item.get("code") or ""
        typ = item.get("type") or ""
        # 相关度：越小越靠前
        if name_l == q or code == q or code.lstrip("0") == q.lstrip("0"):
            relevance = 0
        elif name_l.startswith(q):
            relevance = 1
        elif q in name_l:
            relevance = 2
        else:
            relevance = 9
        penalty = 0
        if "-R" in name or "ADR" in name.upper() or "认股" in name or "-SW" in name:
            penalty += 10
        if item.get("market") == "HK" and code.startswith("8"):
            penalty += 5
        if "GP-A" in typ or typ == "GP":
            penalty -= 1
        return (relevance, penalty, len(name), code)

    rows.sort(key=rank)
    return rows[:limit]


def build_sentiment_bundle(ticker: str, curr_date: str, market_hint: str = "") -> str:
    """情绪代理包：新闻标题热度 + 资金/南下/卖空（港股）。"""
    parts: List[str] = [
        f"# {ticker} 情绪与市场关注度（WeStock）",
        f"**分析日期**: {curr_date}",
        "",
        "> 说明：中文社交平台直连受限时，用新闻热度、资金流、港股通持仓、卖空比例作为可验证代理指标。",
        "",
    ]

    news = fetch_news(ticker, limit=8, market_hint=market_hint)
    if news:
        parts.append(news)
        parts.append("")

    flow = fetch_fund_flow(ticker, market_hint=market_hint)
    if flow:
        parts.append(flow)
        parts.append("")

    # 港股增强
    code = to_westock_code(ticker, market_hint)
    if code.startswith("hk") or "HK" in (market_hint or "").upper() or "港" in (market_hint or ""):
        south = fetch_south_holding(ticker, market_hint="HK")
        if south:
            parts.append(south)
            parts.append("")
        short = fetch_short(ticker, market_hint="HK")
        if short:
            parts.append(short)
            parts.append("")

    notices = fetch_notices(ticker, limit=3, market_hint=market_hint)
    if notices:
        parts.append(notices)

    text = "\n".join(parts).strip()
    if len(text) < 80:
        return ""
    return text


def build_news_bundle(ticker: str, max_news: int = 10, market_hint: str = "") -> str:
    parts: List[str] = []
    news = fetch_news(ticker, limit=max_news, market_hint=market_hint)
    if news:
        parts.append(news)
    notices = fetch_notices(ticker, limit=min(5, max_news), market_hint=market_hint)
    if notices:
        parts.append(notices)
    reports = fetch_reports(ticker, limit=min(5, max_news), market_hint=market_hint)
    if reports:
        parts.append(reports)
    return "\n\n".join(parts).strip()
