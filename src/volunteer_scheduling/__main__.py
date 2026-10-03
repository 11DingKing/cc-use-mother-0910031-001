"""启动排班服务：

    PYTHONPATH=src python3 -m volunteer_scheduling --db data.db --port 8080
"""
from __future__ import annotations

import argparse

from .api import make_server, start_sweeper
from .service import SchedulingService


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="volunteer_scheduling", description="未成年志愿者授权排班服务")
    parser.add_argument("--db", default="volunteer_scheduling.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--hold-ttl", type=int, default=900, help="暂占有效期（秒）")
    parser.add_argument("--sweep-interval", type=float, default=30.0,
                        help="过期暂占清理间隔（秒），0 表示关闭后台清理")
    args = parser.parse_args()

    service = SchedulingService(args.db, hold_ttl_seconds=args.hold_ttl)
    if args.sweep_interval > 0:
        start_sweeper(service, args.sweep_interval)
    server = make_server(service, args.host, args.port)
    print(f"未成年志愿者授权排班服务已启动：http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
