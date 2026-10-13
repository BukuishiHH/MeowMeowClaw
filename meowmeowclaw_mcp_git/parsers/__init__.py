"""git 输出的结构化解析器(§5.3).

GM1 提供 ``status``; ``diff`` / ``log`` / ``branch`` 等解析器随 GM2 加入。
"""

from .status import RenameEntry, StatusSummary, parse_porcelain_v2

__all__ = ["RenameEntry", "StatusSummary", "parse_porcelain_v2"]
