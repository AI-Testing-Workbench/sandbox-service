"""
进程入口（T10.4：REST API + Scheduler）。

- 环境变量由外部调用方注入（compose / shell 等，本进程不加载 .env）；
  导入 `config` 即触发必填环境变量校验，缺失即启动失败（v4 §5.1）。
- 启动时将 SQLite 迁移到最新架构，启动单实例 Scheduler 后台循环（v4 §13.1）。
- 以 uvicorn 承载 REST API（`0.0.0.0:{TA_SS_REST_API_PORT}`，默认 8080）；本进程仅装配 REST API 与 Scheduler。
"""

import logging
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# noinspection unused-imports
import config  # noqa: E402  导入即校验 TA_SS_* 环境变量
from config import Constants, settings  # noqa: E402


_LOG_TIMEZONE = ZoneInfo(Constants.TIMEZONE.value)


class _ProjectLocalTime:
    """将日志时间戳转换为项目固定的北京时间。"""

    def __call__(self, timestamp: float | int | None) -> time.struct_time:
        if timestamp is None:
            timestamp = time.time()
        return datetime.fromtimestamp(timestamp, _LOG_TIMEZONE).timetuple()


_PROJECT_LOCALTIME = _ProjectLocalTime()


def _configure_logging() -> None:
    # logging 和 Uvicorn 都基于 Formatter.converter；显式绑定项目时区，
    # 避免容器宿主机的本地时区影响日志时间。
    # 使用可调用对象，避免普通函数赋给类属性后被绑定为实例方法。
    logging.Formatter.converter = _PROJECT_LOCALTIME
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main() -> None:
    _configure_logging()
    logger = logging.getLogger("main")
    logger.info("REST API 启动于: http://127.0.0.1:%s", settings.rest_api_port)
    import uvicorn

    from interfaces.app import create_app  # noqa: PLC0415

    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=settings.rest_api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
