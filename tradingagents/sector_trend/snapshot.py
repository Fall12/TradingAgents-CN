"""
个股所属板块趋势快照 —— 供智能分析注入上下文。
"""

from __future__ import annotations

from typing import Optional

from tradingagents.utils.logging_init import get_logger

logger = get_logger("default")


def _fmt_num(v, suffix: str = "", signed: bool = False) -> str:
    if v is None:
        return "N/A"
    try:
        if v != v:  # NaN
            return "N/A"
        x = float(v)
    except Exception:
        return str(v)
    if signed:
        return f"{x:+.1f}{suffix}" if suffix else f"{x:+.1f}"
    if suffix == "%":
        return f"{x:.1f}%"
    if isinstance(v, float) and not x.is_integer():
        return f"{x:.1f}{suffix}"
    return f"{x:.0f}{suffix}"


def format_sector_snapshot_for_symbol(symbol: str, freq: str = "D") -> str:
    """
    生成可直接注入分析师 prompt 的板块快照文本。
    非 A 股或数据不可用时返回空字符串。
    """
    try:
        from tradingagents.utils.stock_utils import StockUtils
        from tradingagents.sector_trend.core import (
            normalize_ashare_code,
            get_sector_row_for_symbol,
            stock_vs_sector_excess,
        )
    except Exception as e:
        logger.warning(f"板块快照依赖导入失败: {e}")
        return ""

    code = normalize_ashare_code(symbol)
    if not code or not StockUtils.is_china_stock(code):
        return ""

    try:
        row = get_sector_row_for_symbol(code, freq=freq)
    except Exception as e:
        logger.warning(f"获取板块快照失败 {code}: {e}")
        return (
            f"【所属板块趋势快照】暂不可用（计算失败: {type(e).__name__}）。"
            "请勿编造板块强弱，仅基于个股数据进行分析。"
        )

    if not row:
        return (
            "【所属板块趋势快照】未匹配到东财行业/板块指标。"
            "请勿编造板块强弱，仅基于个股数据进行分析。"
        )

    industry = row.get("行业", "未知")
    if row.get("_missing_metrics"):
        return (
            f"【所属板块趋势快照】行业={industry}，但该行业暂无完整趋势指标"
            "（成分不足或数据不足）。请勿编造板块强弱。"
        )

    rel = None
    try:
        rel = stock_vs_sector_excess(code, freq=freq)
    except Exception:
        rel = None

    freq_lab = "日线" if freq == "D" else "周线"
    lines = [
        f"【所属板块趋势快照｜{freq_lab}｜与板块趋势页同口径，进程内约1小时缓存】",
        (
            f"行业: {industry} | 状态: {row.get('状态', 'N/A')} | "
            f"阶段: {row.get('阶段', 'N/A')} | "
            f"段方向: {row.get('段方向', 'N/A')} 持续{row.get('持续', 'N/A')}日"
            f"（段收益{_fmt_num(row.get('段收益%'), '%', signed=True)}）"
        ),
        (
            f"相对沪深300: 超额20日 {_fmt_num(row.get('超额20日'), '%', signed=True)} / "
            f"超额60日 {_fmt_num(row.get('超额60日'), '%', signed=True)} | "
            f"广度 {_fmt_num(row.get('广度'), '%')} | 起势分 {_fmt_num(row.get('起势分'))}"
        ),
        (
            f"动能: {row.get('动能形态', 'N/A')} | 趋势结构: {row.get('趋势结构', 'N/A')} | "
            f"量价: {row.get('量价关系') or 'N/A'} | 扩散: {row.get('扩散') or 'N/A'}"
        ),
    ]
    if rel:
        lines.append(
            f"个股相对板块: 20日 {_fmt_num(rel.get('相对板块20日'), '%', signed=True)} / "
            f"60日 {_fmt_num(rel.get('相对板块60日'), '%', signed=True)}"
            "（正=强于板块，负=弱于板块）"
        )
    risk_bits = []
    fake = row.get("假反转风险")
    if fake is not None and fake == fake:
        risk_bits.append(f"假反转风险{_fmt_num(fake)}")
    confirm = row.get("反转确认度")
    if confirm is not None and confirm == confirm:
        risk_bits.append(
            f"反转确认度{_fmt_num(confirm)}"
            + (f"({row.get('反转类型')})" if row.get("反转类型") else "")
        )
    if risk_bits:
        lines.append("风险辅助: " + " | ".join(risk_bits))
    lines.append(
        "使用要求: 必须结合上述板块环境解读个股；"
        "若阶段为③衰竭/④反转，应降低追高倾向并写明失效条件；"
        "若个股显著弱于板块，需单独解释（基本面/事件/资金）而非简单跟随板块。"
    )
    return "\n".join(lines)


def get_sector_snapshot_safe(symbol: str) -> str:
    """对外安全入口：任何异常都转为空/提示，不阻断分析。"""
    try:
        return format_sector_snapshot_for_symbol(symbol) or ""
    except Exception as e:
        logger.warning(f"板块快照异常 {symbol}: {e}")
        return ""


def inject_sector_section(report: str, symbol: str) -> str:
    """
    把板块快照硬性写入报告正文（不依赖 LLM 是否遵从提示）。
    优先插在「股票基本信息」之后、「技术指标/下一节」之前。
    """
    import re

    snap = get_sector_snapshot_safe(symbol)
    if not snap:
        return report or ""
    report = report or ""
    if "板块环境（系统数据）" in report:
        return report

    block = (
        "\n\n## 板块环境（系统数据）\n\n"
        f"{snap}\n\n"
        "> 以上为系统根据东财行业 + 市值加权板块指数计算，与「板块趋势」页同口径，非模型编造。\n"
    )

    # 插在第二节之前（二、技术指标 / 技术指标分析 / emoji 版）
    m = re.search(r"\n##\s*(二[、．.]|📈\s*技术|技术指标)", report)
    if m:
        return report[: m.start()] + block + report[m.start() :]

    # 或紧跟基本信息小节
    m2 = re.search(
        r"(##\s*(?:一[、．.]?\s*)?股票基本信息[\s\S]*?)(?=\n##\s)",
        report,
    )
    if m2:
        return report[: m2.end(1)] + block + report[m2.end(1) :]

    # 标题后插入
    m3 = re.search(r"(#\s*\*?.+?\n)", report)
    if m3:
        return report[: m3.end(1)] + block + report[m3.end(1) :]

    return block + "\n" + report
