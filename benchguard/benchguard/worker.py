"""裁决守护线程：并发申请逐份串行裁决。

HTTP 请求只登记 pending；本线程 FIFO 逐份处理，避免并发事务交叉。
进程中断后重启时 repo.recover_interrupted 把 processing 重置为 pending。
"""
from __future__ import annotations

import logging
import threading
import time

from .db import Database
from .repo import adjudicate_next, recover_interrupted

log = logging.getLogger("benchguard.worker")


class AdjudicationWorker(threading.Thread):
    daemon = True

    def __init__(self, db: Database, poll_interval: float = 0.25):
        super().__init__(name="adjudication-worker")
        self.db = db
        self.poll_interval = poll_interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        recovered = recover_interrupted(self.db)
        if recovered:
            log.warning("中断恢复：%d 份 processing 申请已回到 pending 重排", recovered)
        while not self._stop.is_set():
            try:
                did_work = adjudicate_next(self.db)
            except Exception:  # 单份失败不拖垮线程
                log.exception("裁决异常，等待下轮重试")
                did_work = False
            if not did_work:
                self._stop.wait(self.poll_interval)
