"""允许通过 python -m x2feishu_monitor 启动服务。"""

from x2feishu_monitor.app import main


if __name__ == "__main__":
    raise SystemExit(main())
