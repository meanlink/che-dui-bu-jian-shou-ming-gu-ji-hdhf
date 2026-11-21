"""启动入口：python -m benchguard

环境变量：
  BENCHGUARD_DB   数据库文件路径（默认 ./data/benchguard.db）
  BENCHGUARD_HOST 监听地址（默认 127.0.0.1）
  BENCHGUARD_PORT 监听端口（默认 18216）
  BENCHGUARD_SEED 启动时幂等种子（默认 true）
"""
from __future__ import annotations

import logging
import os

from .db import Database
from .seed import seed
from .server import build_server


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    db_path = os.environ.get("BENCHGUARD_DB", os.path.join("data", "benchguard.db"))
    host = os.environ.get("BENCHGUARD_HOST", "127.0.0.1")
    port = int(os.environ.get("BENCHGUARD_PORT", "18216"))

    db = Database(db_path)
    if os.environ.get("BENCHGUARD_SEED", "true").lower() == "true":
        result = seed(db)
        logging.getLogger("benchguard").info("种子结果: %s", result)

    server = build_server(db, host, port)
    logging.getLogger("benchguard").info("管控系统已启动: http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.worker.stop()
        server.server_close()


if __name__ == "__main__":
    main()
