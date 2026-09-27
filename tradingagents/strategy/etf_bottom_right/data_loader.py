"""
数据管道模块

功能:
1. 清库：只清行情相关（stock_daily_quotes / market_quotes / stock_basic_info 中 ETF 相关）
2. 全量拉取：ETF + 沪深300，从 2017-01-01 起
3. 增量更新：最近 10 个交易日（覆盖修正）
4. 定时任务：交易日收盘后 16:30 调 10 日更新

数据源优先级: pytdx(通达信TCP) > 新浪 > 腾讯 > 东财 > AKShare > Tushare
pytdx 为 TCP 二进制协议，0.05s/次，比 HTTP API 快 100 倍，无限流。
"""
import logging
import asyncio
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# V8 引擎锁：东方财富接口使用 py_mini_racer(V8)，非线程安全，需加锁
_v8_lock = threading.Lock()

# ==================== 通达信 pytdx 连接管理 ====================
# pytdx 使用 TCP 二进制协议，不走 HTTP，无代理问题，无限流
# 每次请求约 0.05s，比 HTTP API 快 100 倍

_TDX_SERVERS = [
    ('115.238.56.198', 7709),
    ('115.238.90.165', 7709),
    ('117.184.140.156', 7709),
    ('180.153.39.51', 7709),
    ('221.231.141.60', 7709),
    ('180.153.18.170', 7709),
    ('14.17.75.71', 7709),
    ('59.173.18.140', 7709),
]

_tdx_local = threading.local()
_tdx_server_idx = 0
_tdx_server_lock = threading.Lock()


def _get_tdx_api():
    """获取当前线程的 pytdx 连接（线程安全，每线程独立连接）"""
    api = getattr(_tdx_local, 'api', None)
    if api is not None:
        # 检查连接是否仍然有效
        try:
            api.get_security_count(1)
            return api
        except Exception:
            try:
                api.disconnect()
            except Exception:
                pass
            _tdx_local.api = None

    try:
        from pytdx.hq import TdxHq_API
    except ImportError:
        return None

    api = TdxHq_API()

    global _tdx_server_idx
    with _tdx_server_lock:
        start_idx = _tdx_server_idx
        _tdx_server_idx = (_tdx_server_idx + 1) % len(_TDX_SERVERS)

    for i in range(len(_TDX_SERVERS)):
        server_idx = (start_idx + i) % len(_TDX_SERVERS)
        ip, port = _TDX_SERVERS[server_idx]
        try:
            if api.connect(ip, port, time_out=5):
                _tdx_local.api = api
                return api
        except Exception:
            continue

    logger.warning("pytdx 所有服务器连接失败")
    return None


class AdaptiveRateLimiter:
    """
    自适应限流器：所有线程共享，根据成功/失败自动调节请求间隔。

    - 成功时逐步降低延迟（最快到 min_delay）
    - 普通失败时翻倍延迟（指数退避）
    - 被限流(429/403)时三倍延迟
    """

    def __init__(self, initial_delay=0.3, max_delay=8.0, min_delay=0.1):
        self.delay = initial_delay
        self.max_delay = max_delay
        self.min_delay = min_delay
        self._consecutive_failures = 0
        self._lock = threading.Lock()

    def wait(self):
        """请求前等待"""
        with self._lock:
            d = self.delay
        if d > 0:
            time.sleep(d)

    def on_success(self):
        with self._lock:
            self._consecutive_failures = 0
            self.delay = max(self.min_delay, self.delay * 0.85)

    def on_failure(self):
        with self._lock:
            self._consecutive_failures += 1
            self.delay = min(self.max_delay, self.delay * 2)

    def on_rate_limited(self):
        with self._lock:
            self._consecutive_failures += 1
            self.delay = min(self.max_delay, self.delay * 3)

    @property
    def current_delay(self):
        with self._lock:
            return self.delay


# 全局限流器实例（所有线程共享）
_rate_limiter = AdaptiveRateLimiter(initial_delay=0.3, max_delay=8.0, min_delay=0.1)

# 需要清理的集合（只清行情相关，不动用户/配置）
MARKET_COLLECTIONS = [
    "stock_daily_quotes",   # 日线行情
    "market_quotes",        # 实时行情
]

# stock_basic_info 中只清 ETF 相关（保留股票）
# ETF 代码特征：51xxxx(SH) / 15xxxx(SZ) / 56xxxx(SH) / 58xxxx(SH)
ETF_CODE_PREFIXES = ("51", "15", "56", "58")


def _worker_pull_etf(task_args):
    """
    模块级工作函数：拉取单只 ETF 日线并保存到 MongoDB（多进程安全）

    每个子进程创建独立的 ETFDataLoader 实例，
    拥有独立的 V8 引擎和 MongoDB 连接，避免线程安全问题。
    """
    symbol, ts_code, start_date, end_date = task_args
    try:
        loader = ETFDataLoader()
        df = loader.fetch_etf_daily(symbol, ts_code, start_date, end_date)
        if df is not None and not df.empty:
            saved = loader.save_to_mongodb(df)
            return ("success", symbol, saved)
        else:
            return ("no_data", symbol, 0)
    except Exception as e:
        return ("error", symbol, str(e))


def _worker_update_etf(task_args):
    """
    模块级工作函数：增量更新单只 ETF 日线（多进程安全）
    """
    symbol, ts_code, start_date, end_date, days = task_args
    try:
        loader = ETFDataLoader()
        df = loader.fetch_etf_daily(symbol, ts_code, start_date, end_date)
        if df is not None and not df.empty:
            df = df.sort_values("trade_date").tail(days * 2)
            saved = loader.save_to_mongodb(df)
            return ("success", symbol, saved)
        return ("no_data", symbol, 0)
    except Exception as e:
        return ("error", symbol, str(e))


class ETFDataLoader:
    """ETF 数据加载器"""

    def __init__(self, config=None):
        self.config = config
        self._tushare_api = None
        self._tushare_checked = False
        self._akshare_checked = False

    # ==================== 数据源检测 ====================

    def _get_tushare_api(self):
        """获取 Tushare API（优先数据库 token，回退环境变量）"""
        if self._tushare_checked:
            return self._tushare_api
        self._tushare_checked = True

        try:
            from tradingagents.dataflows.providers.china.tushare import TushareProvider
            provider = TushareProvider()
            if provider.connect_sync():
                self._tushare_api = provider.api
                logger.info("✅ Tushare 数据源可用")
                return self._tushare_api
        except Exception as e:
            logger.warning(f"Tushare 初始化失败: {e}")

        self._tushare_api = None
        return None

    def _check_akshare(self) -> bool:
        """检查 AKShare 是否可用"""
        if self._akshare_checked:
            return self._akshare_available
        self._akshare_checked = True
        try:
            import akshare as ak  # noqa: F401
            self._akshare_available = True
            logger.info("✅ AKShare 数据源可用")
            return True
        except ImportError:
            self._akshare_available = False
            logger.warning("AKShare 未安装")
            return False

    # ==================== Tushare 数据拉取 ====================

    def _fetch_etf_list_tushare(self) -> Optional[pd.DataFrame]:
        """通过 Tushare 获取 ETF 基础信息"""
        api = self._get_tushare_api()
        if api is None:
            return None
        try:
            df = api.fund_basic(market="E")
            if df is not None and not df.empty:
                logger.info(f"Tushare: 获取 {len(df)} 只 ETF 基础信息")
                return df
        except Exception as e:
            logger.error(f"Tushare fund_basic 失败: {e}")
        return None

    def _fetch_etf_daily_tushare(
        self, ts_code: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """通过 Tushare 获取 ETF 日线"""
        api = self._get_tushare_api()
        if api is None:
            return None
        try:
            df = api.fund_daily(
                ts_code=ts_code,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
            return df
        except Exception as e:
            logger.debug(f"Tushare fund_daily({ts_code}) 失败: {e}")
        return None

    def _fetch_index_daily_tushare(
        self, ts_code: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """通过 Tushare 获取指数日线"""
        api = self._get_tushare_api()
        if api is None:
            return None
        try:
            df = api.index_daily(
                ts_code=ts_code,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
            return df
        except Exception as e:
            logger.error(f"Tushare index_daily({ts_code}) 失败: {e}")
        return None

    # ==================== AKShare 数据拉取 ====================

    def _fetch_etf_list_akshare(self) -> Optional[pd.DataFrame]:
        """通过 AKShare 获取 ETF 列表（东方财富实时行情接口）"""
        if not self._check_akshare():
            return None
        try:
            import akshare as ak
            # 优先用东方财富 ETF 实时行情（返回标准 6 位代码）
            try:
                df = ak.fund_etf_spot_em()
                if df is not None and not df.empty:
                    logger.info(f"AKShare(东财): 获取 {len(df)} 只 ETF")
                    return df
            except Exception as e:
                logger.debug(f"fund_etf_spot_em 失败: {e}")

            # 回退新浪
            df = ak.fund_etf_category_sina()
            if df is not None and not df.empty:
                logger.info(f"AKShare(新浪): 获取 {len(df)} 只 ETF")
                return df
        except Exception as e:
            logger.error(f"AKShare ETF 列表获取失败: {e}")
        return None

    # ==================== 新浪 API 直连（线程安全，无 V8 依赖）====================

    _SINA_KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    _SINA_HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
    _NO_PROXY = {"http": None, "https": None}

    def _sina_symbol(self, code: str) -> str:
        """将纯数字代码转为新浪格式（带 sh/sz 前缀）"""
        if code.startswith(("5", "6", "9")):
            return f"sh{code}"
        elif code.startswith(("0", "1", "2", "3")):
            return f"sz{code}"
        return f"sh{code}"

    def _fetch_etf_daily_sina(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """
        直接调用新浪 API 获取 ETF 日线（纯 HTTP，线程安全，无 V8 依赖）

        使用全局限流器自适应控制请求频率，被限流时自动降速。
        新浪返回最近 datalen 条日K数据（降序），需按日期过滤。
        """
        import requests
        import json

        sina_sym = self._sina_symbol(symbol)
        params = {
            "symbol": sina_sym,
            "scale": "240",    # 日K线
            "ma": "no",
            "datalen": "2500",  # 约 10 年日线
        }

        max_retries = 5
        for attempt in range(max_retries):
            _rate_limiter.wait()
            try:
                r = requests.get(
                    self._SINA_KLINE_URL, params=params,
                    headers=self._SINA_HEADERS, timeout=15,
                    proxies=self._NO_PROXY,
                )

                # 限流检测（456=新浪自定义封禁, 429=标准限流, 403=禁止访问）
                if r.status_code in (429, 403, 456):
                    _rate_limiter.on_rate_limited()
                    logger.debug(f"新浪API({symbol}) 被限流 HTTP {r.status_code}, "
                                 f"延迟升至 {_rate_limiter.current_delay:.1f}s")
                    continue

                if r.status_code != 200:
                    _rate_limiter.on_failure()
                    logger.debug(f"新浪API({symbol}) HTTP {r.status_code}")
                    continue

                data = json.loads(r.text)
                if not data:
                    _rate_limiter.on_success()
                    return None

                df = pd.DataFrame(data)
                # 列名映射: day->trade_date, 保持数值列为 float
                df = df.rename(columns={"day": "trade_date"})
                for col in ("open", "high", "low", "close", "volume"):
                    df[col] = df[col].astype(float)

                # 按日期过滤（新浪返回降序，需过滤后升序）
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
                mask = (df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)
                df = df.loc[mask].copy()

                if df.empty:
                    _rate_limiter.on_success()
                    return None

                # 补充字段
                df["amount"] = 0.0  # 新浪 API 不提供成交额
                df["symbol"] = symbol
                df["data_source"] = "sina"
                df["period"] = "daily"
                df["market"] = "CN"

                df = df.sort_values("trade_date").reset_index(drop=True)
                _rate_limiter.on_success()
                return df

            except Exception as e:
                _rate_limiter.on_failure()
                if attempt < max_retries - 1:
                    logger.debug(f"新浪API({symbol}) 第{attempt+1}次失败: {e}, "
                                 f"延迟升至 {_rate_limiter.current_delay:.1f}s")
                else:
                    logger.debug(f"新浪API({symbol}) 放弃: {e}")
        return None

    def _fetch_index_daily_sina(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """直接调用新浪 API 获取指数日线（纯 HTTP，线程安全）"""
        import requests
        import json

        sina_sym = self._sina_symbol(symbol)
        params = {
            "symbol": sina_sym,
            "scale": "240",
            "ma": "no",
            "datalen": "2500",
        }

        max_retries = 5
        for attempt in range(max_retries):
            _rate_limiter.wait()
            try:
                r = requests.get(
                    self._SINA_KLINE_URL, params=params,
                    headers=self._SINA_HEADERS, timeout=15,
                    proxies=self._NO_PROXY,
                )

                if r.status_code in (429, 403, 456):
                    _rate_limiter.on_rate_limited()
                    continue

                if r.status_code != 200:
                    _rate_limiter.on_failure()
                    continue

                data = json.loads(r.text)
                if not data:
                    _rate_limiter.on_success()
                    return None

                df = pd.DataFrame(data)
                df = df.rename(columns={"day": "trade_date"})
                for col in ("open", "high", "low", "close", "volume"):
                    df[col] = df[col].astype(float)

                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
                mask = (df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)
                df = df.loc[mask].copy()

                if df.empty:
                    _rate_limiter.on_success()
                    return None

                df["amount"] = 0.0
                df["symbol"] = symbol
                df["data_source"] = "sina"
                df["period"] = "daily"
                df["market"] = "CN"

                df = df.sort_values("trade_date").reset_index(drop=True)
                _rate_limiter.on_success()
                return df

            except Exception as e:
                _rate_limiter.on_failure()
                if attempt >= max_retries - 1:
                    logger.debug(f"新浪指数API({symbol}) 放弃: {e}")
        return None

    # ==================== 东方财富直连 HTTP API（线程安全，无 V8 依赖）====================

    _EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    _EM_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.eastmoney.com"}

    def _em_secid(self, code: str) -> str:
        """将纯数字代码转为东财 secid 格式（1=上海, 0=深圳）"""
        if code.startswith(("5", "6", "9")):
            return f"1.{code}"   # 上海
        elif code.startswith(("0", "1", "2", "3")):
            return f"0.{code}"   # 深圳
        return f"1.{code}"

    def _fetch_etf_daily_em(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """
        直接调用东方财富 HTTP API 获取 ETF 日线（纯 HTTP，线程安全）

        东财 klines 格式: "date,open,close,high,low,volume,amount,..."
        """
        import requests

        secid = self._em_secid(symbol)
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",     # 日K线
            "fqt": "1",       # 前复权
            "beg": start_date.replace("-", ""),
            "end": end_date.replace("-", ""),
        }

        max_retries = 5
        for attempt in range(max_retries):
            _rate_limiter.wait()
            try:
                r = requests.get(
                    self._EM_KLINE_URL, params=params,
                    headers=self._EM_HEADERS, timeout=15,
                    proxies=self._NO_PROXY,
                )

                if r.status_code in (429, 403, 456):
                    _rate_limiter.on_rate_limited()
                    continue

                if r.status_code != 200:
                    _rate_limiter.on_failure()
                    continue

                data = r.json()
                klines = data.get("data", {}).get("klines", [])
                if not klines:
                    _rate_limiter.on_success()
                    return None

                # 解析 klines: "date,open,close,high,low,volume,amount,..."
                records = []
                for line in klines:
                    parts = line.split(",")
                    records.append({
                        "trade_date": parts[0],
                        "open": float(parts[1]),
                        "close": float(parts[2]),
                        "high": float(parts[3]),
                        "low": float(parts[4]),
                        "volume": float(parts[5]),
                        "amount": float(parts[6]),
                    })

                df = pd.DataFrame(records)
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
                df["symbol"] = symbol
                df["data_source"] = "eastmoney"
                df["period"] = "daily"
                df["market"] = "CN"

                df = df.sort_values("trade_date").reset_index(drop=True)
                _rate_limiter.on_success()
                return df

            except Exception as e:
                _rate_limiter.on_failure()
                if attempt >= max_retries - 1:
                    logger.debug(f"东财直连({symbol}) 放弃: {e}")
        return None

    def _fetch_index_daily_em(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """直接调用东方财富 HTTP API 获取指数日线"""
        import requests

        secid = self._em_secid(symbol)
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "1",
            "beg": start_date.replace("-", ""),
            "end": end_date.replace("-", ""),
        }

        max_retries = 5
        for attempt in range(max_retries):
            _rate_limiter.wait()
            try:
                r = requests.get(
                    self._EM_KLINE_URL, params=params,
                    headers=self._EM_HEADERS, timeout=15,
                    proxies=self._NO_PROXY,
                )

                if r.status_code in (429, 403, 456):
                    _rate_limiter.on_rate_limited()
                    continue

                if r.status_code != 200:
                    _rate_limiter.on_failure()
                    continue

                data = r.json()
                klines = data.get("data", {}).get("klines", [])
                if not klines:
                    _rate_limiter.on_success()
                    return None

                records = []
                for line in klines:
                    parts = line.split(",")
                    records.append({
                        "trade_date": parts[0],
                        "open": float(parts[1]),
                        "close": float(parts[2]),
                        "high": float(parts[3]),
                        "low": float(parts[4]),
                        "volume": float(parts[5]),
                        "amount": float(parts[6]),
                    })

                df = pd.DataFrame(records)
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
                df["symbol"] = symbol
                df["data_source"] = "eastmoney"
                df["period"] = "daily"
                df["market"] = "CN"

                df = df.sort_values("trade_date").reset_index(drop=True)
                _rate_limiter.on_success()
                return df

            except Exception as e:
                _rate_limiter.on_failure()
                if attempt >= max_retries - 1:
                    logger.debug(f"东财指数直连({symbol}) 放弃: {e}")
        return None

    # ==================== 腾讯财经直连 HTTP API（线程安全，无 V8 依赖）====================

    _TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    _TX_HEADERS = {"User-Agent": "Mozilla/5.0"}

    def _fetch_etf_daily_tencent(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """
        直接调用腾讯财经 API 获取 ETF 日线（纯 HTTP，线程安全）

        腾讯返回格式: [date, open, close, high, low, volume, ...]
        注意: close 在 high/low 之前，与标准 OHLC 顺序不同。
        """
        import requests

        sina_sym = self._sina_symbol(symbol)
        # param=sh510300,day,start,end,count,qfq
        param = f"{sina_sym},day,{start_date},{end_date},3000,qfq"
        params = {"param": param}

        max_retries = 3
        for attempt in range(max_retries):
            _rate_limiter.wait()
            try:
                r = requests.get(
                    self._TX_KLINE_URL, params=params,
                    headers=self._TX_HEADERS, timeout=10,
                    proxies=self._NO_PROXY,
                )

                if r.status_code in (429, 403, 456):
                    _rate_limiter.on_rate_limited()
                    continue

                if r.status_code != 200:
                    _rate_limiter.on_failure()
                    continue

                data = r.json()
                # 优先取前复权数据，回退不复权
                klines = (
                    data.get("data", {}).get(sina_sym, {}).get("qfqday")
                    or data.get("data", {}).get(sina_sym, {}).get("day")
                )
                if not klines:
                    _rate_limiter.on_success()
                    return None

                # 解析: [date, open, close, high, low, volume, ...]
                records = []
                for item in klines:
                    records.append({
                        "trade_date": item[0],
                        "open": float(item[1]),
                        "close": float(item[2]),
                        "high": float(item[3]),
                        "low": float(item[4]),
                        "volume": float(item[5]) if len(item) > 5 else 0.0,
                        "amount": float(item[6]) if len(item) > 6 else 0.0,
                    })

                df = pd.DataFrame(records)
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
                df["symbol"] = symbol
                df["data_source"] = "tencent"
                df["period"] = "daily"
                df["market"] = "CN"

                df = df.sort_values("trade_date").reset_index(drop=True)
                _rate_limiter.on_success()
                return df

            except Exception as e:
                _rate_limiter.on_failure()
                if attempt >= max_retries - 1:
                    logger.debug(f"腾讯API({symbol}) 放弃: {e}")
        return None

    def _fetch_index_daily_tencent(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """直接调用腾讯财经 API 获取指数日线"""
        import requests

        sina_sym = self._sina_symbol(symbol)
        param = f"{sina_sym},day,{start_date},{end_date},3000,qfq"
        params = {"param": param}

        max_retries = 3
        for attempt in range(max_retries):
            _rate_limiter.wait()
            try:
                r = requests.get(
                    self._TX_KLINE_URL, params=params,
                    headers=self._TX_HEADERS, timeout=10,
                    proxies=self._NO_PROXY,
                )

                if r.status_code in (429, 403, 456):
                    _rate_limiter.on_rate_limited()
                    continue

                if r.status_code != 200:
                    _rate_limiter.on_failure()
                    continue

                data = r.json()
                klines = (
                    data.get("data", {}).get(sina_sym, {}).get("qfqday")
                    or data.get("data", {}).get(sina_sym, {}).get("day")
                )
                if not klines:
                    _rate_limiter.on_success()
                    return None

                records = []
                for item in klines:
                    records.append({
                        "trade_date": item[0],
                        "open": float(item[1]),
                        "close": float(item[2]),
                        "high": float(item[3]),
                        "low": float(item[4]),
                        "volume": float(item[5]) if len(item) > 5 else 0.0,
                        "amount": 0.0,
                    })

                df = pd.DataFrame(records)
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
                df["symbol"] = symbol
                df["data_source"] = "tencent"
                df["period"] = "daily"
                df["market"] = "CN"

                df = df.sort_values("trade_date").reset_index(drop=True)
                _rate_limiter.on_success()
                return df

            except Exception as e:
                _rate_limiter.on_failure()
                if attempt >= max_retries - 1:
                    logger.debug(f"腾讯指数API({symbol}) 放弃: {e}")
        return None

    # ==================== 通达信 pytdx（TCP 二进制，最快，线程安全）====================

    def _tdx_market(self, code: str) -> int:
        """代码转 pytdx 市场代码: 0=深, 1=沪"""
        if code.startswith(("5", "6", "9")):
            return 1  # 沪
        return 0  # 深

    def _tdx_index_market(self, code: str) -> int:
        """指数代码转 pytdx 市场代码: 0=深, 1=沪

        沪市指数: 000xxx (如 000300 沪深300, 000001 上证指数)
        深市指数: 399xxx (如 399001 深证成指, 399006 创业板指)
        """
        if code.startswith("399"):
            return 0  # 深
        return 1  # 沪（000xxx 系列指数在沪市）

    def _fetch_etf_daily_pytdx(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """
        通过 pytdx 获取 ETF 日线（TCP 协议，0.05s/次，比 HTTP 快 100 倍）

        pytdx 每次返回最多 800 条，需翻页拉取完整历史。
        数据按时间倒序返回（最新在前），每批内为正序（旧到新）。
        """
        api = _get_tdx_api()
        if api is None:
            return None

        market = self._tdx_market(symbol)

        # 翻页拉取（最多 3200 条 ≈ 13 年日线）
        all_data = []
        for offset in range(0, 3200, 800):
            try:
                batch = api.get_security_bars(4, market, symbol, offset, 800)
            except Exception as e:
                logger.debug(f"pytdx({symbol}) offset={offset} 失败: {e}")
                # 重连
                api = _get_tdx_api()
                if api is None:
                    break
                try:
                    batch = api.get_security_bars(4, market, symbol, offset, 800)
                except Exception:
                    break

            if not batch:
                break
            all_data.extend(batch)
            if len(batch) < 800:
                break  # 已到历史尽头

        if not all_data:
            return None

        # 解析为 DataFrame
        records = []
        for row in all_data:
            dt = row.get("datetime", "")
            if not dt:
                continue
            trade_date = dt[:10]  # "2024-01-15 15:00" -> "2024-01-15"
            records.append({
                "trade_date": trade_date,
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "volume": float(row.get("vol", 0)),
                "amount": float(row.get("amount", 0)),
            })

        df = pd.DataFrame(records)
        if df.empty:
            return None

        # 去重 + 日期过滤 + 排序
        df = df.drop_duplicates(subset=["trade_date"]).reset_index(drop=True)
        mask = (df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)
        df = df.loc[mask].copy()

        if df.empty:
            return None

        df["symbol"] = symbol
        df["data_source"] = "pytdx"
        df["period"] = "daily"
        df["market"] = "CN"
        df = df.sort_values("trade_date").reset_index(drop=True)
        return df

    def _fetch_index_daily_pytdx(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """通过 pytdx 获取指数日线（TCP 协议，极快）"""
        api = _get_tdx_api()
        if api is None:
            return None

        market = self._tdx_index_market(symbol)

        all_data = []
        for offset in range(0, 3200, 800):
            try:
                batch = api.get_index_bars(4, market, symbol, offset, 800)
            except Exception as e:
                logger.debug(f"pytdx指数({symbol}) offset={offset} 失败: {e}")
                api = _get_tdx_api()
                if api is None:
                    break
                try:
                    batch = api.get_index_bars(4, market, symbol, offset, 800)
                except Exception:
                    break

            if not batch:
                break
            all_data.extend(batch)
            if len(batch) < 800:
                break

        if not all_data:
            return None

        records = []
        for row in all_data:
            dt = row.get("datetime", "")
            if not dt:
                continue
            trade_date = dt[:10]
            records.append({
                "trade_date": trade_date,
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "volume": float(row.get("vol", 0)),
                "amount": float(row.get("amount", 0)),
            })

        df = pd.DataFrame(records)
        if df.empty:
            return None

        df = df.drop_duplicates(subset=["trade_date"]).reset_index(drop=True)
        mask = (df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)
        df = df.loc[mask].copy()

        if df.empty:
            return None

        df["symbol"] = symbol
        df["data_source"] = "pytdx"
        df["period"] = "daily"
        df["market"] = "CN"
        df = df.sort_values("trade_date").reset_index(drop=True)
        return df

    def _fetch_etf_daily_akshare(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """通过 AKShare 获取 ETF 日线（保留作为回退，单线程调用）"""
        if not self._check_akshare():
            return None

        try:
            import akshare as ak
            with _v8_lock:
                df = ak.fund_etf_hist_em(
                    symbol=symbol,
                    period="daily",
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust="qfq",
                )
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.debug(f"AKShare fund_etf_hist_em({symbol}) 失败: {e}")

        return None

    def _fetch_index_daily_akshare(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """通过 AKShare 获取指数日线（保留作为回退）"""
        if not self._check_akshare():
            return None
        try:
            import akshare as ak
            with _v8_lock:
                df = ak.stock_zh_index_daily(symbol=symbol)
            if df is not None and not df.empty:
                df["date"] = pd.to_datetime(df["date"])
                df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
            return df
        except Exception as e:
            logger.error(f"AKShare 指数日线({symbol}) 失败: {e}")
        return None

    # ==================== 统一拉取接口 ====================

    def fetch_etf_list(self) -> pd.DataFrame:
        """获取 ETF 列表（Tushare 优先，AKShare 回退）"""
        # 先试 Tushare
        df = self._fetch_etf_list_tushare()
        if df is not None and not df.empty:
            return self._normalize_etf_list_tushare(df)

        # 回退 AKShare
        df = self._fetch_etf_list_akshare()
        if df is not None and not df.empty:
            return self._normalize_etf_list_akshare(df)

        logger.error("无法获取 ETF 列表：Tushare 和 AKShare 均不可用")
        return pd.DataFrame()

    def _normalize_etf_list_tushare(self, df: pd.DataFrame) -> pd.DataFrame:
        """标准化 Tushare ETF 列表"""
        result = pd.DataFrame()
        result["symbol"] = df["ts_code"].str.split(".").str[0]
        result["ts_code"] = df["ts_code"]
        result["name"] = df.get("name", "")
        result["list_date"] = df.get("list_date", "")
        result["management_fee"] = df.get("management_fee", "")
        result["custodian_fee"] = df.get("custodian_fee", "")
        result["invest_type"] = df.get("invest_type", "")
        result["data_source"] = "tushare"
        return result

    def _normalize_etf_list_akshare(self, df: pd.DataFrame) -> pd.DataFrame:
        """标准化 AKShare ETF 列表"""
        # AKShare fund_etf_category_sina 返回列: 代码、名称、最新价、涨跌幅等
        col_map = {
            "代码": "symbol", "symbol": "symbol",
            "名称": "name", "name": "name",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        # 新浪代码带市场前缀（sh510300 / sz159915），需提取纯数字代码
        def extract_code(raw):
            code = str(raw).strip()
            # 去掉 sh/sz 前缀
            if code.startswith(("sh", "sz")):
                code = code[2:]
            return code.zfill(6)

        df["symbol"] = df["symbol"].apply(extract_code)

        # 只保留标准 ETF 代码前缀（51/15/56/58），过滤 LOF（16xxxx）等
        etf_prefixes = ("51", "15", "56", "58")
        df = df[df["symbol"].str.startswith(etf_prefixes)].copy()

        result = pd.DataFrame()
        result["symbol"] = df["symbol"].values
        result["name"] = df.get("name", "").values
        result["list_date"] = ""
        result["data_source"] = "akshare"

        # 生成 ts_code
        def to_ts_code(code):
            if code.startswith(("51", "56", "58")):
                return f"{code}.SH"
            elif code.startswith("15"):
                return f"{code}.SZ"
            return f"{code}.SH"

        result["ts_code"] = result["symbol"].apply(to_ts_code)
        return result

    def fetch_etf_daily(
        self, symbol: str, ts_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """
        获取单只 ETF 日线数据

        优先级: pytdx(通达信TCP) > 新浪直连 > 腾讯直连 > 东财直连 > AKShare(加锁) > Tushare

        pytdx 为 TCP 二进制协议，0.05s/次，无 HTTP 限流，线程安全（每线程独立连接）。
        其余为 HTTP 回退。
        """
        # 0. pytdx 通达信（TCP 协议，最快，线程安全）
        df = self._fetch_etf_daily_pytdx(symbol, start_date, end_date)
        if df is not None and not df.empty:
            return df

        # 1. 新浪直连（纯 HTTP，线程安全）
        df = self._fetch_etf_daily_sina(symbol, start_date, end_date)
        if df is not None and not df.empty:
            return df

        # 2. 腾讯直连（纯 HTTP，线程安全）
        df = self._fetch_etf_daily_tencent(symbol, start_date, end_date)
        if df is not None and not df.empty:
            return df

        # 3. 东财直连（纯 HTTP，线程安全）
        df = self._fetch_etf_daily_em(symbol, start_date, end_date)
        if df is not None and not df.empty:
            return df

        # 4. AKShare 东方财富（加 V8 锁，回退）
        df = self._fetch_etf_daily_akshare(symbol, start_date, end_date)
        if df is not None and not df.empty:
            return self._normalize_daily_akshare(df, symbol)

        # 5. Tushare
        df = self._fetch_etf_daily_tushare(ts_code, start_date, end_date)
        if df is not None and not df.empty:
            return self._normalize_daily_tushare(df, symbol)

        logger.warning(f"无法获取 {symbol} 日线数据")
        return pd.DataFrame()

    def fetch_index_daily(
        self, symbol: str, ts_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """
        获取指数日线数据

        优先级: pytdx(通达信TCP) > 新浪直连 > 腾讯直连 > 东财直连 > Tushare > AKShare(加锁)
        """
        # 0. pytdx 通达信（TCP 协议，最快）
        df = self._fetch_index_daily_pytdx(symbol, start_date, end_date)
        if df is not None and not df.empty:
            return df

        # 1. 优先新浪直连
        df = self._fetch_index_daily_sina(symbol, start_date, end_date)
        if df is not None and not df.empty:
            return df

        # 2. 腾讯直连
        df = self._fetch_index_daily_tencent(symbol, start_date, end_date)
        if df is not None and not df.empty:
            return df

        # 3. 东财直连
        df = self._fetch_index_daily_em(symbol, start_date, end_date)
        if df is not None and not df.empty:
            return df

        # 4. 试 Tushare
        df = self._fetch_index_daily_tushare(ts_code, start_date, end_date)
        if df is not None and not df.empty:
            return self._normalize_daily_tushare(df, symbol)

        # 5. 回退 AKShare（加锁）
        ak_symbol = f"sh{symbol}" if symbol.startswith("000") else f"sz{symbol}"
        df = self._fetch_index_daily_akshare(ak_symbol, start_date, end_date)
        if df is not None and not df.empty:
            return self._normalize_daily_akshare_index(df, symbol)

        logger.warning(f"无法获取指数 {symbol} 日线数据")
        return pd.DataFrame()

    def _normalize_daily_tushare(
        self, df: pd.DataFrame, symbol: str
    ) -> pd.DataFrame:
        """标准化 Tushare 日线数据"""
        result = pd.DataFrame()
        result["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
        result["open"] = df["open"].astype(float)
        result["high"] = df["high"].astype(float)
        result["low"] = df["low"].astype(float)
        result["close"] = df["close"].astype(float)
        result["volume"] = df["vol"].astype(float)
        result["amount"] = df["amount"].astype(float) * 1000  # Tushare amount 单位千元
        result["symbol"] = symbol
        result["data_source"] = "tushare"
        result["period"] = "daily"
        result["market"] = "CN"
        return result.sort_values("trade_date").reset_index(drop=True)

    def _normalize_daily_akshare(
        self, df: pd.DataFrame, symbol: str
    ) -> pd.DataFrame:
        """标准化 AKShare ETF 日线数据"""
        col_map = {
            "日期": "trade_date", "date": "trade_date",
            "开盘": "open", "open": "open",
            "最高": "high", "high": "high",
            "最低": "low", "low": "low",
            "收盘": "close", "close": "close",
            "成交量": "volume", "volume": "volume",
            "成交额": "amount", "amount": "amount",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        result = pd.DataFrame()
        result["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        result["open"] = df["open"].astype(float)
        result["high"] = df["high"].astype(float)
        result["low"] = df["low"].astype(float)
        result["close"] = df["close"].astype(float)
        result["volume"] = df["volume"].astype(float)
        result["amount"] = df["amount"].astype(float)
        result["symbol"] = symbol
        result["data_source"] = "akshare"
        result["period"] = "daily"
        result["market"] = "CN"
        return result.sort_values("trade_date").reset_index(drop=True)

    def _normalize_daily_akshare_index(
        self, df: pd.DataFrame, symbol: str
    ) -> pd.DataFrame:
        """标准化 AKShare 指数日线数据"""
        result = pd.DataFrame()
        result["trade_date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        result["open"] = df["open"].astype(float)
        result["high"] = df["high"].astype(float)
        result["low"] = df["low"].astype(float)
        result["close"] = df["close"].astype(float)
        result["volume"] = df["volume"].astype(float)
        result["amount"] = 0.0  # 指数无成交额
        result["symbol"] = symbol
        result["data_source"] = "akshare"
        result["period"] = "daily"
        result["market"] = "CN"
        return result.sort_values("trade_date").reset_index(drop=True)

    # ==================== MongoDB 操作 ====================

    def _get_db(self):
        """获取 MongoDB 数据库实例"""
        from app.core.database import get_mongo_db_sync
        return get_mongo_db_sync()

    def clear_market_data(self, clear_etf_only: bool = True):
        """
        清除行情相关数据

        Args:
            clear_etf_only: True 只清 ETF 相关数据，False 清全部行情
        """
        db = self._get_db()
        logger.info("=" * 50)
        logger.info("开始清除行情数据...")
        logger.info(f"  模式: {'仅ETF相关' if clear_etf_only else '全部行情'}")
        logger.info("=" * 50)

        for collection_name in MARKET_COLLECTIONS:
            collection = db[collection_name]
            if clear_etf_only:
                # 只删除 ETF 代码前缀的数据
                for prefix in ETF_CODE_PREFIXES:
                    query = {"symbol": {"$regex": f"^{prefix}"}}
                    result = collection.delete_many(query)
                    logger.info(f"  {collection_name} ({prefix}*): 删除 {result.deleted_count} 条")
                # 也删除沪深300指数数据
                result = collection.delete_many({"symbol": "000300"})
                logger.info(f"  {collection_name} (000300): 删除 {result.deleted_count} 条")
            else:
                result = collection.delete_many({})
                logger.info(f"  {collection_name}: 删除 {result.deleted_count} 条")

        # stock_basic_info: 清 ETF 相关
        basic_info = db["stock_basic_info"]
        if clear_etf_only:
            for prefix in ETF_CODE_PREFIXES:
                query = {"$or": [
                    {"symbol": {"$regex": f"^{prefix}"}},
                    {"code": {"$regex": f"^{prefix}"}},
                ]}
                result = basic_info.delete_many(query)
                logger.info(f"  stock_basic_info ({prefix}*): 删除 {result.deleted_count} 条")
        else:
            result = basic_info.delete_many({})
            logger.info(f"  stock_basic_info: 删除 {result.deleted_count} 条")

        logger.info("✅ 行情数据清除完成")

    def save_to_mongodb(self, df: pd.DataFrame, collection_name: str = "stock_daily_quotes"):
        """保存数据到 MongoDB（upsert 模式）"""
        if df.empty:
            return 0

        from pymongo import ReplaceOne

        db = self._get_db()
        collection = db[collection_name]

        # 转换为字典列表
        records = df.to_dict("records")

        # 批量 upsert
        bulk_ops = []
        saved_count = 0
        for record in records:
            filter_query = {
                "symbol": record["symbol"],
                "trade_date": record["trade_date"],
                "data_source": record.get("data_source", ""),
                "period": record.get("period", "daily"),
            }
            bulk_ops.append(ReplaceOne(
                filter=filter_query,
                replacement=record,
                upsert=True,
            ))

            if len(bulk_ops) >= 1000:
                try:
                    result = collection.bulk_write(bulk_ops, ordered=False)
                    saved_count += result.upserted_count + result.modified_count
                except Exception as e:
                    logger.warning(f"批量写入部分失败: {e}")
                bulk_ops = []

        if bulk_ops:
            try:
                result = collection.bulk_write(bulk_ops, ordered=False)
                saved_count += result.upserted_count + result.modified_count
            except Exception as e:
                logger.warning(f"批量写入部分失败: {e}")

        return saved_count

    def save_basic_info(self, df: pd.DataFrame):
        """保存 ETF 基础信息到 stock_basic_info"""
        if df.empty:
            return 0

        from pymongo import ReplaceOne

        db = self._get_db()
        collection = db["stock_basic_info"]

        records = df.to_dict("records")
        bulk_ops = []
        saved_count = 0
        for record in records:
            # 补充 code/source 字段以兼容 stock_basic_info 的唯一索引 (code_1_source_1)
            record.setdefault("code", record.get("symbol", ""))
            record.setdefault("source", record.get("data_source", "akshare"))
            filter_query = {"code": record["code"], "source": record["source"]}
            bulk_ops.append(ReplaceOne(
                filter=filter_query,
                replacement=record,
                upsert=True,
            ))

            if len(bulk_ops) >= 1000:
                try:
                    result = collection.bulk_write(bulk_ops, ordered=False)
                    saved_count += result.upserted_count + result.modified_count
                except Exception as e:
                    logger.warning(f"基础信息批量写入部分失败: {e}")
                bulk_ops = []

        if bulk_ops:
            try:
                result = collection.bulk_write(bulk_ops, ordered=False)
                saved_count += result.upserted_count + result.modified_count
            except Exception as e:
                logger.warning(f"基础信息批量写入部分失败: {e}")

        return saved_count

    # ==================== 断点续传 ====================

    def _get_existing_latest_date(self, symbol: str) -> Optional[str]:
        """查询数据库中某标的的最新交易日（用于断点续传）"""
        db = self._get_db()
        collection = db["stock_daily_quotes"]
        doc = collection.find_one(
            {"symbol": symbol},
            sort=[("trade_date", -1)],
            projection={"trade_date": 1},
        )
        if doc:
            return doc.get("trade_date")
        return None

    def _get_all_existing_latest_dates(self) -> Dict[str, str]:
        """批量查询所有 ETF 的最新交易日（一次聚合查询，避免逐个查询）

        Returns:
            {symbol: latest_trade_date}
        """
        db = self._get_db()
        collection = db["stock_daily_quotes"]

        # 构建 ETF 代码前缀正则
        import re
        prefix_pattern = "|".join(f"^{p}" for p in ETF_CODE_PREFIXES)

        try:
            pipeline = [
                {"$match": {"symbol": {"$regex": prefix_pattern}}},
                {"$group": {
                    "_id": "$symbol",
                    "latest_date": {"$max": "$trade_date"},
                }},
            ]
            results = {}
            for doc in collection.aggregate(pipeline):
                results[doc["_id"]] = doc["latest_date"]
            logger.info(f"断点续传批量查询: {len(results)} 只 ETF 已有数据")
            return results
        except Exception as e:
            logger.warning(f"批量查询失败，回退逐个查询: {e}")
            # 回退到逐个查询
            results = {}
            for prefix in ETF_CODE_PREFIXES:
                for doc in collection.find(
                    {"symbol": {"$regex": f"^{prefix}"}},
                    projection={"symbol": 1, "trade_date": 1},
                ).sort("trade_date", -1):
                    sym = doc.get("symbol")
                    if sym and sym not in results:
                        results[sym] = doc.get("trade_date")
            return results

    def _get_existing_etf_symbols(self) -> set:
        """查询数据库中已有行情数据的 ETF 代码集合"""
        db = self._get_db()
        collection = db["stock_daily_quotes"]
        symbols = set()
        for prefix in ETF_CODE_PREFIXES:
            docs = collection.distinct("symbol", {"symbol": {"$regex": f"^{prefix}"}})
            symbols.update(docs)
        return symbols

    # ==================== 全量拉取 ====================

    def full_pull(
        self,
        start_date: str = "2017-01-01",
        end_date: str = None,
        etf_codes: List[str] = None,
        include_hs300: bool = True,
        batch_delay: float = 0.3,
        max_workers: int = 16,
        skip_existing: bool = True,
    ) -> dict:
        """
        全量拉取 ETF + 沪深300 日线数据（并行版，支持断点续传）

        pytdx 为主数据源（TCP 协议，0.05s/次），16 线程并发约 30 秒拉完 1000+ ETF。

        Args:
            start_date: 起始日期
            end_date: 结束日期（默认今天）
            etf_codes: 指定 ETF 代码列表（None 则拉全部）
            include_hs300: 是否拉取沪深300指数
            batch_delay: 保留参数（限流由 AdaptiveRateLimiter 管理，pytdx 无限流）
            max_workers: 并行线程数（默认 16，pytdx 支持高并发）
            skip_existing: 是否跳过已拉取完成的 ETF（断点续传）

        Returns:
            统计信息
        """
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        stats = {
            "start_date": start_date,
            "end_date": end_date,
            "total_etfs": 0,
            "success_etfs": 0,
            "failed_etfs": 0,
            "skipped_etfs": 0,
            "total_records": 0,
            "hs300_records": 0,
            "errors": [],
        }

        logger.info("=" * 60)
        logger.info("开始全量拉取 ETF 数据（并行模式 + 断点续传）")
        logger.info(f"  日期范围: {start_date} ~ {end_date}")
        logger.info(f"  并行线程数: {max_workers}")
        logger.info(f"  断点续传: {'开启' if skip_existing else '关闭'}")
        logger.info("=" * 60)

        # 1. 获取 ETF 列表
        etf_list = self.fetch_etf_list()
        if etf_list.empty:
            logger.error("无法获取 ETF 列表，终止")
            stats["errors"].append("无法获取 ETF 列表")
            return stats

        # 保存基础信息
        self.save_basic_info(etf_list)
        logger.info(f"ETF 基础信息已保存: {len(etf_list)} 条")

        # 筛选指定代码
        if etf_codes:
            etf_list = etf_list[etf_list["symbol"].isin(etf_codes)]

        stats["total_etfs"] = len(etf_list)
        logger.info(f"待拉取 ETF 数量: {len(etf_list)}")

        # 2. 断点续传：跳过已完成的 ETF，部分完成的只拉增量
        pull_tasks = []  # [(symbol, ts_code, actual_start_date), ...]
        skipped = 0

        if skip_existing:
            # 批量查询所有已有数据的 ETF 最新日期（一次查询，避免 1500+ 次逐个查询）
            existing_dates = self._get_all_existing_latest_dates()

            for _, row in etf_list.iterrows():
                symbol = row["symbol"]
                ts_code = row.get("ts_code", f"{symbol}.SH")
                existing_date = existing_dates.get(symbol)

                if existing_date and existing_date >= end_date:
                    # 已有数据覆盖到结束日期，跳过
                    skipped += 1
                elif existing_date:
                    # 有部分数据，从已有日期往后拉（往前推 3 天确保衔接）
                    from datetime import timedelta as _td
                    d = datetime.strptime(existing_date, "%Y-%m-%d") - _td(days=3)
                    actual_start = d.strftime("%Y-%m-%d")
                    pull_tasks.append((symbol, ts_code, actual_start))
                else:
                    # 全新拉取
                    pull_tasks.append((symbol, ts_code, start_date))

            stats["skipped_etfs"] = skipped
            logger.info(f"断点续传: 跳过 {skipped} 只已完成, 实际拉取 {len(pull_tasks)} 只")
        else:
            for _, row in etf_list.iterrows():
                symbol = row["symbol"]
                ts_code = row.get("ts_code", f"{symbol}.SH")
                pull_tasks.append((symbol, ts_code, start_date))

        if not pull_tasks:
            logger.info("所有 ETF 已拉取完成，无需更新")

        # 3. 并行拉取日线
        from concurrent.futures import ThreadPoolExecutor, as_completed

        lock = threading.Lock()
        completed_count = 0

        def _pull_one_etf(symbol, ts_code, actual_start):
            """拉取单只 ETF 日线（线程池工作函数）"""
            try:
                df = self.fetch_etf_daily(symbol, ts_code, actual_start, end_date)
                if df is not None and not df.empty:
                    saved = self.save_to_mongodb(df)
                    return ("success", symbol, saved)
                else:
                    return ("no_data", symbol, 0)
            except Exception as e:
                return ("error", symbol, str(e))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for symbol, ts_code, actual_start in pull_tasks:
                futures.append(executor.submit(_pull_one_etf, symbol, ts_code, actual_start))

            for future in as_completed(futures):
                status, symbol, info = future.result()
                with lock:
                    completed_count += 1
                    if status == "success":
                        stats["success_etfs"] += 1
                        stats["total_records"] += info
                    elif status == "no_data":
                        stats["failed_etfs"] += 1
                        stats["errors"].append(f"{symbol}: 无数据")
                    else:
                        stats["failed_etfs"] += 1
                        stats["errors"].append(f"{symbol}: {info}")

                    if completed_count % 50 == 0:
                        total_done = stats["success_etfs"] + stats["failed_etfs"]
                        logger.info(
                            f"  进度: {total_done}/{len(pull_tasks)} "
                            f"(成功 {stats['success_etfs']}, "
                            f"记录 {stats['total_records']} 条, "
                            f"当前延迟 {_rate_limiter.current_delay:.1f}s)"
                        )

        # 3. 拉取沪深300指数
        if include_hs300:
            logger.info("拉取沪深300指数...")
            try:
                df = self.fetch_index_daily("000300", "000300.SH", start_date, end_date)
                if df is not None and not df.empty:
                    saved = self.save_to_mongodb(df)
                    stats["hs300_records"] = saved
                    logger.info(f"沪深300指数: {saved} 条")
            except Exception as e:
                stats["errors"].append(f"HS300: {str(e)}")
                logger.error(f"沪深300拉取失败: {e}")

        logger.info("=" * 60)
        logger.info("全量拉取完成")
        logger.info(f"  ETF: {stats['success_etfs']}/{stats['total_etfs']} 成功")
        logger.info(f"  总记录数: {stats['total_records'] + stats['hs300_records']}")
        logger.info("=" * 60)

        return stats

    # ==================== 增量更新（最近 10 个交易日）====================

    def incremental_update(
        self, days: int = 10, etf_codes: List[str] = None, max_workers: int = 16
    ) -> dict:
        """
        增量更新最近 N 个交易日的数据（覆盖修正，并行版）

        Args:
            days: 更新天数（默认 10）
            etf_codes: 指定 ETF 代码（None 则更新全部库中已有的）
            max_workers: 并行线程数

        Returns:
            统计信息
        """
        # 计算日期范围（多取 5 天日历日确保覆盖 10 个交易日）
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days * 2 + 10)).strftime("%Y-%m-%d")

        stats = {
            "update_date": end_date,
            "days": days,
            "updated_etfs": 0,
            "total_records": 0,
            "hs300_updated": False,
            "errors": [],
        }

        logger.info("=" * 50)
        logger.info(f"增量更新: 最近 {days} 个交易日（并行模式）")
        logger.info(f"  日期范围: {start_date} ~ {end_date}")
        logger.info(f"  并行线程数: {max_workers}")
        logger.info("=" * 50)

        # 获取需要更新的 ETF 列表
        if etf_codes is None:
            # 从数据库获取已有 ETF 代码
            db = self._get_db()
            symbols = db["stock_daily_quotes"].distinct(
                "symbol",
                {"symbol": {"$in": [
                    {"$regex": f"^{p}"} for p in ETF_CODE_PREFIXES
                ]}}
            )
            etf_codes = symbols

        logger.info(f"待更新 ETF 数量: {len(etf_codes)}")

        # 获取 ETF 基础信息（用于 ts_code）
        etf_list = self.fetch_etf_list()
        code_to_ts = {}
        if not etf_list.empty:
            code_to_ts = dict(zip(etf_list["symbol"], etf_list.get("ts_code", [])))

        # 并行更新（多线程 + 新浪API优先避免V8线程安全问题）
        from concurrent.futures import ThreadPoolExecutor, as_completed

        lock = threading.Lock()

        def _update_one_etf(symbol):
            ts_code = code_to_ts.get(symbol, f"{symbol}.SH")
            try:
                df = self.fetch_etf_daily(symbol, ts_code, start_date, end_date)
                if df is not None and not df.empty:
                    df = df.sort_values("trade_date").tail(days * 2)
                    saved = self.save_to_mongodb(df)
                    return ("success", symbol, saved)
                return ("no_data", symbol, 0)
            except Exception as e:
                return ("error", symbol, str(e))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_update_one_etf, s) for s in etf_codes]
            for future in as_completed(futures):
                status, symbol, info = future.result()
                with lock:
                    if status == "success":
                        stats["updated_etfs"] += 1
                        stats["total_records"] += info
                    elif status == "error":
                        stats["errors"].append(f"{symbol}: {info}")

        # 更新沪深300
        try:
            df = self.fetch_index_daily("000300", "000300.SH", start_date, end_date)
            if df is not None and not df.empty:
                df = df.sort_values("trade_date").tail(days * 2)
                saved = self.save_to_mongodb(df)
                stats["hs300_updated"] = True
                stats["total_records"] += saved
        except Exception as e:
            stats["errors"].append(f"HS300: {str(e)}")

        logger.info(f"增量更新完成: {stats['updated_etfs']} 只 ETF, {stats['total_records']} 条记录")
        return stats

    # ==================== 从 MongoDB 加载数据 ====================

    def load_from_mongodb(
        self,
        symbols: List[str] = None,
        start_date: str = "2017-01-01",
        end_date: str = None,
    ) -> Tuple[Dict[str, pd.DataFrame], pd.DataFrame]:
        """
        从 MongoDB 加载 ETF + 沪深300 日线数据

        Returns:
            etf_data: {symbol: DataFrame}
            hs300_df: 沪深 300 DataFrame
        """
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        db = self._get_db()
        collection = db["stock_daily_quotes"]

        # 加载沪深300
        query = {"symbol": "000300", "trade_date": {"$gte": start_date, "$lte": end_date}}
        cursor = collection.find(query).sort("trade_date", 1)
        hs300_records = list(cursor)
        if hs300_records:
            hs300_df = pd.DataFrame(hs300_records)
            base_cols = ["trade_date", "open", "high", "low", "close", "volume", "amount"]
            select_cols = base_cols + (["data_source"] if "data_source" in hs300_df.columns else [])
            hs300_df = hs300_df[select_cols]
            for col in ["open", "high", "low", "close", "volume", "amount"]:
                hs300_df[col] = hs300_df[col].astype(float)
            # 去重：同一日期保留一条（优先 pytdx > akshare > sina）
            if "data_source" in hs300_df.columns:
                source_priority = {"pytdx": 0, "akshare": 1, "sina": 2, "tencent": 3, "eastmoney": 4}
                hs300_df["_priority"] = hs300_df["data_source"].map(source_priority).fillna(9)
                hs300_df = hs300_df.sort_values(["trade_date", "_priority"])
                hs300_df = hs300_df.drop_duplicates(subset=["trade_date"], keep="first")
                hs300_df = hs300_df.drop(columns=["_priority", "data_source"])
            else:
                hs300_df = hs300_df.drop_duplicates(subset=["trade_date"], keep="last")
            hs300_df = hs300_df.reset_index(drop=True)
            logger.info(f"沪深300 加载: {len(hs300_df)} 条")
        else:
            hs300_df = pd.DataFrame()
            logger.warning("未找到沪深300数据")

        # 加载 ETF 数据
        etf_data = {}
        if symbols is None:
            # 获取所有 ETF 代码
            symbols = collection.distinct("symbol", {
                "symbol": {"$nin": ["000300"]},
                "trade_date": {"$gte": start_date, "$lte": end_date},
            })

        # 数据源优先级（数字越小优先级越高）
        source_priority = {"pytdx": 0, "akshare": 1, "sina": 2, "tencent": 3, "eastmoney": 4}

        for symbol in symbols:
            query = {
                "symbol": symbol,
                "trade_date": {"$gte": start_date, "$lte": end_date},
            }
            cursor = collection.find(query).sort("trade_date", 1)
            records = list(cursor)
            if records:
                df = pd.DataFrame(records)
                cols = ["trade_date", "open", "high", "low", "close", "volume", "amount"]
                extra_cols = ["data_source"] if "data_source" in df.columns else []
                df = df[cols + extra_cols]
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    if col in df.columns:
                        df[col] = df[col].astype(float)
                # 去重：同一日期保留一条（优先 pytdx > sina > 其他）
                if "data_source" in df.columns:
                    df["_priority"] = df["data_source"].map(source_priority).fillna(9)
                    df = df.sort_values(["trade_date", "_priority"])
                    df = df.drop_duplicates(subset=["trade_date"], keep="first")
                    df = df.drop(columns=["_priority", "data_source"])
                else:
                    df = df.drop_duplicates(subset=["trade_date"], keep="last")
                df = df.reset_index(drop=True)
                etf_data[symbol] = df

        logger.info(f"ETF 数据加载: {len(etf_data)} 只, 共 {sum(len(df) for df in etf_data.values())} 条")
        return etf_data, hs300_df

    def load_etf_basic_info(self) -> pd.DataFrame:
        """从 MongoDB 加载 ETF 基础信息"""
        db = self._get_db()
        collection = db["stock_basic_info"]

        # 查询 ETF 代码
        query = {"$or": [
            {"symbol": {"$regex": f"^{p}"}} for p in ETF_CODE_PREFIXES
        ]}
        cursor = collection.find(query)
        records = list(cursor)

        if records:
            df = pd.DataFrame(records)
            if "_id" in df.columns:
                df = df.drop(columns=["_id"])
            return df
        return pd.DataFrame()
