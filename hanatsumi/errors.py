"""预期错误类型。

``hanatsumi.cli.app.main()`` 捕获 ``HanatsumiError``，只打印 ``message`` 并按
``exit_code`` 退出，不打印调用栈；其余异常才打印调用栈。
"""

from __future__ import annotations


class HanatsumiError(Exception):
    """预期错误：message 面向用户，exit_code 决定进程退出码。"""

    exit_code: int = 1

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UsageError(HanatsumiError):
    """用法或前置条件不满足（例如全量抓取没做完就想 update）。"""

    exit_code = 2


class StateError(HanatsumiError):
    """状态文件缺失或损坏，需要用户按提示修复。"""


class ScanError(HanatsumiError):
    """服务端返回了无法安全续传的数据，宁可失败也不写脏数据。"""


class ApiError(HanatsumiError):
    """HTTP 请求最终失败（重试次数用尽或致命状态码）。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
