"""板块趋势：核心计算 + 个股分析快照。"""
from .snapshot import (
    format_sector_snapshot_for_symbol,
    get_sector_snapshot_safe,
    inject_sector_section,
)

__all__ = [
    "format_sector_snapshot_for_symbol",
    "get_sector_snapshot_safe",
    "inject_sector_section",
]
