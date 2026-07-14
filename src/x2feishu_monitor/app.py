"""服务命令行入口与常驻主循环。"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time

from x2feishu_monitor.clients import ExternalServiceError, FeishuClient, XClient
from x2feishu_monitor.config import ConfigError, Settings
from x2feishu_monitor.service import MonitorService, build_test_card
from x2feishu_monitor.state import StateStore
from x2feishu_monitor.translation import TranslationClient

LOGGER = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    """配置标准输出日志。"""
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def build_service(settings: Settings) -> tuple[MonitorService, StateStore]:
    """构建完整监控服务。

    功能说明：创建 SQLite 状态库、X 客户端、飞书客户端和业务服务实例。
    参数 settings：已经校验的运行配置。
    返回值：MonitorService 与 StateStore 组成的元组。
    """
    state_store = StateStore(settings.state_db_path)
    translation_client = (
        TranslationClient(settings) if settings.translation_enabled else None
    )
    service = MonitorService(
        settings=settings,
        x_client=XClient(settings),
        feishu_client=FeishuClient(settings),
        state_store=state_store,
        translation_client=translation_client,
    )
    return service, state_store


def run_forever(
    service: MonitorService,
    state_store: StateStore,
    interval_seconds: int,
    stop_event: threading.Event,
) -> None:
    """持续执行监控轮询。

    功能说明：按固定周期运行监控，记录每轮结果，并在异常后继续下一轮。
    参数 service：负责单轮查询和推送的监控服务。
    参数 state_store：用于更新容器健康检查心跳的状态库。
    参数 interval_seconds：两轮开始时间之间的目标间隔秒数。
    参数 stop_event：收到退出信号后用于终止等待和主循环的事件。
    返回值：无。
    """
    LOGGER.info("监控服务启动：poll_interval_seconds=%s", interval_seconds)
    while not stop_event.is_set():
        started_at = time.monotonic()
        try:
            pushed_count = service.run_cycle()
            LOGGER.info("轮询周期成功结束：pushed_count=%s", pushed_count)
        except ExternalServiceError as exc:
            LOGGER.error("外部服务调用失败，本轮将在下个周期重试：%s", exc)
        except Exception:
            LOGGER.exception("轮询周期发生未预期异常，本轮将在下个周期重试")
        finally:
            state_store.touch_heartbeat()

        elapsed_seconds = time.monotonic() - started_at
        wait_seconds = max(1.0, interval_seconds - elapsed_seconds)
        stop_event.wait(wait_seconds)
    LOGGER.info("监控服务已停止")


def run_healthcheck(settings: Settings) -> int:
    """执行容器健康检查。

    功能说明：检查主循环心跳是否存在且未超过三个轮询周期。
    参数 settings：包含数据库路径和轮询间隔的运行配置。
    返回值：健康返回 0，不健康返回 1。
    """
    state_store = StateStore(settings.state_db_path)
    heartbeat_age = state_store.heartbeat_age_seconds()
    maximum_age = max(180, settings.poll_interval_seconds * 3)
    if heartbeat_age is None or heartbeat_age > maximum_age:
        LOGGER.error(
            "健康检查失败：heartbeat_age=%s, maximum_age=%s",
            heartbeat_age,
            maximum_age,
        )
        return 1
    LOGGER.info("健康检查通过：heartbeat_age=%.1f", heartbeat_age)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="轮询 X 用户新帖子并推送到飞书")
    parser.add_argument("--once", action="store_true", help="只执行一轮后退出")
    parser.add_argument(
        "--healthcheck", action="store_true", help="检查主循环心跳后退出"
    )
    parser.add_argument(
        "--test-feishu", action="store_true", help="发送飞书连接测试消息后退出"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """启动监控服务。

    功能说明：解析命令行、加载配置，并按常驻、单轮或健康检查模式运行。
    参数 argv：可选命令行参数列表；为空时读取当前进程参数。
    返回值：进程退出码，0 表示成功。
    """
    args = _build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logging.basicConfig(level=logging.ERROR)
        LOGGER.error("配置加载失败：%s", exc)
        return 2

    configure_logging(settings.log_level)
    if args.healthcheck:
        return run_healthcheck(settings)
    if args.test_feishu:
        try:
            FeishuClient(settings).send_card(build_test_card(settings))
            LOGGER.info("飞书连接测试完成")
            return 0
        except ExternalServiceError as exc:
            LOGGER.error("飞书连接测试失败：%s", exc)
            return 1

    service, state_store = build_service(settings)
    if args.once:
        try:
            pushed_count = service.run_cycle()
            state_store.touch_heartbeat()
            LOGGER.info("单轮执行完成：pushed_count=%s", pushed_count)
            return 0
        except (ExternalServiceError, ValueError) as exc:
            LOGGER.error("单轮执行失败：%s", exc)
            return 1

    stop_event = threading.Event()

    def handle_signal(signum: int, _frame: object) -> None:
        """收到系统退出信号后通知主循环安全停止。"""
        LOGGER.info("收到退出信号：signal=%s", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    run_forever(service, state_store, settings.poll_interval_seconds, stop_event)
    return 0


if __name__ == "__main__":
    sys.exit(main())
