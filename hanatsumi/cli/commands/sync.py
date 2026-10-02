"""fetch / update / refresh：三个抓取动作，只做参数转发与结果渲染。"""

from hanatsumi import service
from hanatsumi.cli.options import (
    DEFAULT_DELAY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_OUT,
    DEFAULT_PAGE_SIZE,
    DEFAULT_TIMEOUT,
    DelayOption,
    MaxPagesOption,
    OutOption,
    PageOption,
    QuietFlag,
    RetriesOption,
    StateOption,
    TimeoutOption,
)
from hanatsumi.cli.output import report_scan, scan_progress


def fetch(
    out: OutOption = DEFAULT_OUT,
    state: StateOption = None,
    delay: DelayOption = DEFAULT_DELAY,
    page_size: PageOption = DEFAULT_PAGE_SIZE,
    timeout: TimeoutOption = DEFAULT_TIMEOUT,
    max_retries: RetriesOption = DEFAULT_MAX_RETRIES,
    max_pages: MaxPagesOption = 0,
    quiet: QuietFlag = False,
) -> None:
    """全量抓取 Danbooru 的 tag；可随时中断，重跑同一条命令继续。"""
    paths = service.Paths.build(out, state)
    with scan_progress(not quiet) as progress:
        result = service.fetch(paths, page_size=page_size, max_pages=max_pages, delay=delay, timeout=timeout, max_retries=max_retries, progress=progress)
    report_scan(result)


def update(
    out: OutOption = DEFAULT_OUT,
    state: StateOption = None,
    delay: DelayOption = DEFAULT_DELAY,
    page_size: PageOption = DEFAULT_PAGE_SIZE,
    timeout: TimeoutOption = DEFAULT_TIMEOUT,
    max_retries: RetriesOption = DEFAULT_MAX_RETRIES,
    max_pages: MaxPagesOption = 0,
    quiet: QuietFlag = False,
) -> None:
    """只抓上次之后新增的 tag；需要先完成一次全量抓取。"""
    paths = service.Paths.build(out, state)
    with scan_progress(not quiet) as progress:
        result = service.update(paths, page_size=page_size, max_pages=max_pages, delay=delay, timeout=timeout, max_retries=max_retries, progress=progress)
    report_scan(result)


def refresh(
    out: OutOption = DEFAULT_OUT,
    state: StateOption = None,
    delay: DelayOption = DEFAULT_DELAY,
    page_size: PageOption = DEFAULT_PAGE_SIZE,
    timeout: TimeoutOption = DEFAULT_TIMEOUT,
    max_retries: RetriesOption = DEFAULT_MAX_RETRIES,
    max_pages: MaxPagesOption = 0,
    quiet: QuietFlag = False,
) -> None:
    """全量重扫到暂存文件，成功后原子替换正式文件（期间旧文件可正常读取）。"""
    paths = service.Paths.build(out, state)
    with scan_progress(not quiet) as progress:
        result = service.refresh(paths, page_size=page_size, max_pages=max_pages, delay=delay, timeout=timeout, max_retries=max_retries, progress=progress)
    report_scan(result)
