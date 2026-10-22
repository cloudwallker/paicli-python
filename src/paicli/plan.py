"""Validated dependency DAG with bounded concurrency and cancellation."""
from __future__ import annotations

import asyncio
import re

from .errors import BudgetExceeded, PlanError


def validate_plan(plan: dict) -> list[dict]:
    if not isinstance(plan, dict) or set(plan) != {"tasks"} or not isinstance(plan["tasks"], list) or not 1 <= len(plan["tasks"]) <= 100:
        raise PlanError("Plan must contain 1–100 tasks.")
    tasks = []
    seen = set()
    for raw in plan["tasks"]:
        if not isinstance(raw, dict) or set(raw) - {"id", "goal", "depends_on", "script"}:
            raise PlanError("Task contains unknown fields.")
        task_id = raw.get("id")
        goal = raw.get("goal")
        dependencies = raw.get("depends_on", [])
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", task_id) or task_id in seen:
            raise PlanError("Task IDs must be unique safe identifiers.")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 10000:
            raise PlanError("Every task needs a goal of 1–10000 characters.")
        if not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies) or len(set(dependencies)) != len(dependencies):
            raise PlanError("Dependencies must be an array of distinct task IDs.")
        if "script" in raw and (not isinstance(raw["script"], str) or not raw["script"]):
            raise PlanError("Script path must be a nonempty string.")
        seen.add(task_id)
        tasks.append({**raw, "depends_on": dependencies})
    graph = {task["id"]: set(task["depends_on"]) for task in tasks}
    if any(dependencies - seen for dependencies in graph.values()):
        raise PlanError("Plan references a missing dependency.")
    finished = set()
    while len(finished) < len(graph):
        ready = {task_id for task_id, dependencies in graph.items() if task_id not in finished and dependencies <= finished}
        if not ready:
            raise PlanError("Plan dependency graph contains a cycle.")
        finished |= ready
    return tasks


class PlanRunner:
    def __init__(self, worker, *, concurrency: int = 2, timeout_seconds: float = 120):
        if type(concurrency) is not int or not 1 <= concurrency <= 8 or not 0 < timeout_seconds <= 3600:
            raise PlanError("Concurrency must be 1–8; deadline must be 0–3600 seconds.")
        self.worker = worker
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds

    async def run(self, plan: dict) -> dict:
        tasks = validate_plan(plan)
        pending = {task["id"]: task for task in tasks}
        running = {}
        results = {}
        try:
            async with asyncio.timeout(self.timeout_seconds):
                while pending or running:
                    for task_id, task in list(pending.items()):
                        if len(running) >= self.concurrency:
                            break
                        if set(task["depends_on"]) <= results.keys():
                            dependencies = {dep: results[dep] for dep in task["depends_on"]}
                            job = asyncio.create_task(self.worker(task, dependencies), name=task_id)
                            running[job] = task_id
                            del pending[task_id]
                    if not running:
                        raise PlanError("No runnable tasks remain.")
                    completed, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    for job in completed:
                        task_id = running.pop(job)
                        try:
                            results[task_id] = job.result()
                        except Exception:
                            raise PlanError(f"Task '{task_id}' failed; outstanding tasks were cancelled.") from None
                return results
        except TimeoutError:
            raise BudgetExceeded("Plan execution time budget exhausted.") from None
        finally:
            for job in running:
                job.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
