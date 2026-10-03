"""命令行入口：``python -m volsched --db ./volsched.db --port 8080``。

启动时立即执行一次过期暂占清理（含重启恢复），随后后台线程周期清理。
"""
from __future__ import annotations

import argparse

from .app import AppState, build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="未成年志愿者授权排班服务端")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="volsched.db", help="SQLite 文件路径（默认 volsched.db）")
    parser.add_argument("--hold-seconds", type=int, default=120, help="暂占默认有效期秒数")
    parser.add_argument("--sweep-interval", type=float, default=30.0,
                        help="后台清理间隔秒数；0 表示关闭后台清理")
    args = parser.parse_args()

    state = AppState(
        db_path=args.db, hold_seconds=args.hold_seconds,
        sweep_interval=args.sweep_interval,
    )
    state.startup_sweep()
    state.start_background_sweeper()
    httpd = build_server(args.host, args.port, state)
    try:
        print(f"volsched listening on http://{args.host}:{args.port} (db={args.db})")
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        state.close()


if __name__ == "__main__":
    main()
