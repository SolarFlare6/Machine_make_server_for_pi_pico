"""
task_manager.py

Task Manager (Pico W port of the reference task_manager.py).

Long-running tool calls return a task_id immediately and report
progress / completion via task_* events. Finished tasks are pruned to a
small history to keep RAM usage bounded on the Pico.
"""

import asyncio
import util

TASK_STARTED = "started"
TASK_RUNNING = "running"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"

_MAX_FINISHED_TASKS = 10
_next_task = 1


class Task:
    def __init__(self, task_id, coro_factory, tool_name="unknown", parameters=None):
        self.task_id = task_id
        self.coro_factory = coro_factory
        self.tool_name = tool_name
        self.parameters = parameters or {}
        self.state = TASK_STARTED
        self.progress = 0.0
        self.result = None
        self.error = None
        self.created_ms = util.now_ms()
        self.asyncio_task = None


class TaskManager:
    def __init__(self, event_manager, safety_manager):
        self._events = event_manager
        self._safety = safety_manager
        self._tasks = {}

    def get_status(self, task_id):
        task = self._tasks.get(task_id)
        if task is None:
            return None
        return {
            "task_id": task.task_id,
            "tool": task.tool_name,
            "state": task.state,
            "progress": task.progress,
            "result": task.result,
            "error": task.error,
        }

    def start(self, coro_factory, tool_name="unknown", parameters=None):
        global _next_task
        task_id = "task-%d" % _next_task
        _next_task += 1
        task = Task(task_id, coro_factory, tool_name, parameters)
        self._tasks[task_id] = task
        self._prune()
        self._safety.mark_busy(True)
        task.asyncio_task = asyncio.create_task(self._run(task))
        return task_id

    def cancel(self, task_id):
        task = self._tasks.get(task_id)
        if task is None or task.asyncio_task is None or task.state not in (TASK_STARTED, TASK_RUNNING):
            return False
        task.asyncio_task.cancel()
        return True

    def cancel_all(self):
        for task in self._tasks.values():
            if task.asyncio_task is not None and task.state in (TASK_STARTED, TASK_RUNNING):
                task.asyncio_task.cancel()

    def _prune(self):
        finished = [t for t in self._tasks.values()
                    if t.state in (TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED)]
        if len(finished) > _MAX_FINISHED_TASKS:
            finished.sort(key=lambda t: t.created_ms)
            for t in finished[:len(finished) - _MAX_FINISHED_TASKS]:
                self._tasks.pop(t.task_id, None)

    def _data(self, task, state, **extra):
        d = {"task_id": task.task_id, "tool": task.tool_name, "state": state, "progress": task.progress}
        for k in extra:
            d[k] = extra[k]
        return d

    async def _run(self, task):
        await self._events.emit("task_started", self._data(task, TASK_STARTED), task_id=task.task_id)
        await self._events.emit("task_update", self._data(task, TASK_RUNNING), task_id=task.task_id)

        def progress_cb(fraction):
            task.progress = fraction
            task.state = TASK_RUNNING
            data = self._data(task, TASK_RUNNING)
            asyncio.create_task(self._events.emit("task_progress", data, task_id=task.task_id))
            asyncio.create_task(self._events.emit("task_update", data, task_id=task.task_id))

        try:
            res = await task.coro_factory(progress_cb)
            task.state = TASK_COMPLETED
            task.progress = 1.0
            task.result = res
            done = self._data(task, TASK_COMPLETED, result=res)
            await self._events.emit("task_completed", done, task_id=task.task_id)
            await self._events.emit("task_update", done, task_id=task.task_id)
        except asyncio.CancelledError:
            task.state = TASK_CANCELLED
            data = self._data(task, TASK_CANCELLED, error="CANCELLED")
            await self._events.emit("task_failed", data, task_id=task.task_id)
            await self._events.emit("task_update", data, task_id=task.task_id)
        except Exception as exc:
            task.state = TASK_FAILED
            task.error = str(exc)
            data = self._data(task, TASK_FAILED, error=str(exc))
            await self._events.emit("task_failed", data, task_id=task.task_id)
            await self._events.emit("task_update", data, task_id=task.task_id)
        finally:
            self._safety.mark_busy(False)
