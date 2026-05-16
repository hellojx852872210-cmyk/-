# -*- coding: utf-8 -*-
"""
后台调度器 — 线程安全的定时任务管理
"""
from __future__ import annotations
import threading
import time
import logging
import datetime
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ScheduledTask:
    def __init__(
        self,
        name: str,
        fn: Callable,
        interval_minutes: int,
        enabled: bool = True,
    ):
        self.name = name
        self.fn = fn
        self.interval_minutes = interval_minutes
        self.enabled = enabled
        self.last_run: Optional[datetime.datetime] = None
        self.last_result = None
        self.run_count = 0
        self.error_count = 0
        self.is_running = False
        self.last_error = ""
        self.last_started_at: Optional[datetime.datetime] = None
        self.last_finished_at: Optional[datetime.datetime] = None


class Scheduler:
    """简单的多任务定时调度器"""

    def __init__(self, tick_seconds: int = 60):
        self.tick_seconds = tick_seconds
        self._tasks: Dict[str, ScheduledTask] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def register(
        self,
        name: str,
        fn: Callable,
        interval_minutes: int,
        enabled: bool = True,
    ) -> ScheduledTask:
        task = ScheduledTask(name, fn, interval_minutes, enabled)
        with self._lock:
            self._tasks[name] = task
        return task

    def set_enabled(self, name: str, enabled: bool):
        with self._lock:
            if name in self._tasks:
                self._tasks[name].enabled = enabled

    def set_interval(self, name: str, minutes: int):
        with self._lock:
            if name in self._tasks:
                self._tasks[name].interval_minutes = minutes

    def get_task(self, name: str) -> Optional[dict]:
        with self._lock:
            task = self._tasks.get(name)
            if task is None:
                return None
            return self._serialize_task(task)

    def run_now(self, name: str) -> bool:
        """立即触发某个任务（在新线程中运行）"""
        with self._lock:
            task = self._tasks.get(name)
            if task is None or task.is_running:
                return False
            task.is_running = True
            task.last_started_at = datetime.datetime.now()
        threading.Thread(
            target=self._execute_task,
            args=(task,),
            daemon=True,
        ).start()
        return True

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("调度器已启动")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("调度器已停止")

    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> Dict[str, dict]:
        with self._lock:
            return {
                name: self._serialize_task(task)
                for name, task in self._tasks.items()
            }

    def _serialize_task(self, task: ScheduledTask) -> dict:
        now = datetime.datetime.now()
        next_run: Optional[datetime.datetime] = None
        countdown_seconds: Optional[int] = None

        if task.enabled and not task.is_running:
            if task.last_run is None:
                next_run = now
            else:
                next_run = task.last_run + datetime.timedelta(minutes=task.interval_minutes)
            countdown_seconds = max(0, int((next_run - now).total_seconds()))

        last_duration_seconds: Optional[float] = None
        if task.last_started_at and task.last_finished_at:
            duration = (task.last_finished_at - task.last_started_at).total_seconds()
            last_duration_seconds = round(max(0.0, duration), 3)

        if task.is_running:
            last_outcome = "running"
        elif task.last_error:
            last_outcome = "error"
        elif task.run_count > 0:
            last_outcome = "success"
        else:
            last_outcome = "never"

        return {
            "enabled": task.enabled,
            "interval_minutes": task.interval_minutes,
            "last_run": task.last_run.isoformat() if task.last_run else None,
            "last_started_at": task.last_started_at.isoformat() if task.last_started_at else None,
            "last_finished_at": task.last_finished_at.isoformat() if task.last_finished_at else None,
            "next_run": next_run.isoformat() if next_run else None,
            "countdown_seconds": countdown_seconds,
            "last_duration_seconds": last_duration_seconds,
            "last_outcome": last_outcome,
            "run_count": task.run_count,
            "error_count": task.error_count,
            "is_running": task.is_running,
            "last_error": task.last_error,
        }


    # ── 内部 ──────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            now = datetime.datetime.now()
            with self._lock:
                tasks_snapshot = list(self._tasks.values())

            for task in tasks_snapshot:
                if not task.enabled:
                    continue
                if task.is_running:
                    continue
                if task.last_run is None:
                    due = True
                else:
                    due = (now - task.last_run).total_seconds() >= task.interval_minutes * 60
                if due:
                    self.run_now(task.name)

            time.sleep(self.tick_seconds)

    def _execute_task(self, task: ScheduledTask):
        logger.info(f"[调度] 开始执行: {task.name}")
        try:
            result = task.fn()
            with self._lock:
                task.last_run = datetime.datetime.now()
                task.last_finished_at = task.last_run
                task.last_result = result
                task.run_count += 1
                task.last_error = ""
            logger.info(f"[调度] 完成: {task.name} → {result}")
        except Exception as e:
            with self._lock:
                task.last_run = datetime.datetime.now()
                task.last_finished_at = task.last_run
                task.error_count += 1
                task.last_error = str(e)
            logger.exception(f"[调度] 任务失败: {task.name}: {e}")
        finally:
            with self._lock:
                task.is_running = False
