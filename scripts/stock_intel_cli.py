#!/usr/bin/env python3
"""
股票情报 CLI：新闻 / 情绪代理 / 公告研报 一键查询（走 WeStock）

用法:
  python scripts/stock_intel_cli.py news 01211 --limit 8
  python scripts/stock_intel_cli.py sentiment 01211
  python scripts/stock_intel_cli.py news 002594
  python scripts/stock_intel_cli.py search 比亚迪
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
os.environ.setdefault("NO_PROXY", "*")


def _load_westock_cli():
    """直接加载 westock_cli.py，避免触发 tradingagents 包重依赖。"""
    path = project_root / "tradingagents" / "dataflows" / "providers" / "westock_cli.py"
    spec = importlib.util.spec_from_file_location("westock_cli_standalone", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def cmd_search(query: str, limit: int) -> int:
    w = _load_westock_cli()
    ok, out = w.run_westock(["search", query])
    print(out if ok else f"❌ 搜索失败: {out}")
    return 0 if ok else 1


def cmd_news(ticker: str, limit: int, market: str) -> int:
    w = _load_westock_cli()
    if not w.find_westock_bin():
        print("❌ 未找到 westock，请先安装或设置 WESTOCK_BIN")
        return 2
    text = w.build_news_bundle(ticker, max_news=limit, market_hint=market)
    if not text:
        print(f"❌ 未获取到 {ticker} 的新闻/公告/研报")
        return 1
    print(text)
    return 0


def cmd_sentiment(ticker: str, market: str) -> int:
    w = _load_westock_cli()
    if not w.find_westock_bin():
        print("❌ 未找到 westock，请先安装或设置 WESTOCK_BIN")
        return 2
    today = datetime.now().strftime("%Y-%m-%d")
    text = w.build_sentiment_bundle(ticker, today, market_hint=market)
    if not text:
        print(f"❌ 未获取到 {ticker} 的情绪代理数据")
        return 1
    print(text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="股票情报 CLI（WeStock）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("search", help="按中文名/代码搜索")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)

    p_news = sub.add_parser("news", help="新闻+公告+研报")
    p_news.add_argument("ticker", help="如 01211 / 002594 / 00700")
    p_news.add_argument("--limit", type=int, default=8)
    p_news.add_argument("--market", default="", help="可选 HK/CN/US")

    p_sent = sub.add_parser("sentiment", help="情绪代理（新闻热度+资金/南下/卖空）")
    p_sent.add_argument("ticker")
    p_sent.add_argument("--market", default="")

    args = parser.parse_args()
    if args.cmd == "search":
        return cmd_search(args.query, args.limit)
    if args.cmd == "news":
        return cmd_news(args.ticker, args.limit, args.market)
    if args.cmd == "sentiment":
        return cmd_sentiment(args.ticker, args.market)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
