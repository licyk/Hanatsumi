"""入口：``get_app()`` 注册每一个命令；``main()`` 统一错误处理后运行它。"""

import sys

import typer
from typer import Abort, Exit

from hanatsumi.cli.commands.manage import reset, status, verify
from hanatsumi.cli.commands.sync import fetch, refresh, update
from hanatsumi.cli.commands.system import version
from hanatsumi.cli.factory import ClickException, typer_factory
from hanatsumi.errors import HanatsumiError
from hanatsumi.logger import setup_logging

logger = setup_logging()


def get_app() -> typer.Typer:
    """构建命令行。所有命令都在这里注册，别处不再注册。"""
    app = typer_factory("把 Danbooru 的全部 tag 抓到本地 CSV：可断点续传、可增量更新、可全量刷新")

    app.command(help="全量抓取 tag（可中断，重跑同一条命令续传）", name="fetch")(fetch)
    app.command(help="增量抓取上次之后新增的 tag", name="update")(update)
    app.command(help="全量刷新已有数据（暂存后原子替换）", name="refresh")(refresh)
    app.command(help="查看断点状态与 CSV 概况", name="status")(status)
    app.command(help="校验 CSV 与状态文件是否一致", name="verify")(verify)
    app.command(help="删除 CSV、状态文件与暂存文件", name="reset")(reset)
    app.command(help="显示版本", name="version")(version)
    return app


def main() -> None:
    """运行命令行：异常在这里被翻译成退出码。"""
    try:
        get_app()()
    except Exit as e:
        sys.exit(e.exit_code)
    except Abort:
        logger.error("已取消")
        sys.exit(1)
    except ClickException as e:
        e.show()
        sys.exit(e.exit_code)
    except HanatsumiError as e:
        logger.error("%s", e.message)
        sys.exit(e.exit_code)
    except KeyboardInterrupt:
        logger.error("已中断，进度已保存；重跑同一条命令即可继续。")
        sys.exit(130)
    except Exception:
        logger.exception("命令执行失败")
        sys.exit(1)
