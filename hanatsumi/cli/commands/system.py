"""version：环境与版本信息。"""

import platform

from hanatsumi.cli.output import print_table
from hanatsumi.version import VERSION

__all__ = ["version"]


def version() -> None:
    """显示 Hanatsumi 的版本与运行环境。"""
    print_table(None, ["项", "值"], [("hanatsumi", VERSION), ("python", platform.python_version())])
