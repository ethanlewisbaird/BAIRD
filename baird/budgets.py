"""Per-task + global daily budget ledger — Phase 4 design (#7+#8).

Backed by the hub's `/budgets/usage` endpoint, which sums `cost_usd` /
`input_tokens` / `output_tokens` on completed Action rows over a configurable
window (default 24h).

Used by the scheduler before firing a task:

  - per-task: today's spend on this `task_id` vs `task.budget.max_cost_usd`
  - global:   today's total spend vs `hub_cfg.daily_total_usd`

When either ceiling is hit, the task is skipped — an inbox notification
records the reason; already-running firings are not interrupted.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import HubConfig
from .memory_client import HubClient
from .tasks import Task


@dataclass
class BudgetCheck:
    ok: bool
    reason: str
    today_total_usd: float = 0.0
    task_today_usd: float = 0.0


def check_task_budget(
    *, hub: HubClient, task: Task, hub_cfg: HubConfig
) -> BudgetCheck:
    """Decide whether `task` may fire now.

    Side-effect-free: the orchestrator is responsible for emitting an inbox
    notification if `ok=False`.
    """
    if not task.enabled:
        return BudgetCheck(False, "task disabled")

    global_total = hub.budgets_usage(since_hours=24)["cost_usd"]
    task_total = hub.budgets_usage(since_hours=24, task_id=task.id)["cost_usd"]

    if global_total >= hub_cfg.daily_total_usd:
        return BudgetCheck(
            False,
            f"global daily ceiling reached (${global_total:.4f} >= ${hub_cfg.daily_total_usd})",
            today_total_usd=global_total,
            task_today_usd=task_total,
        )

    per_task_cap = (
        task.budget.max_cost_usd
        if task.budget.max_cost_usd is not None
        else hub_cfg.daily_per_task_default_usd
    )
    if per_task_cap is not None and task_total >= per_task_cap:
        return BudgetCheck(
            False,
            f"per-task daily cap reached (${task_total:.4f} >= ${per_task_cap})",
            today_total_usd=global_total,
            task_today_usd=task_total,
        )

    return BudgetCheck(
        True,
        f"under budget (task today=${task_total:.4f}, global today=${global_total:.4f})",
        today_total_usd=global_total,
        task_today_usd=task_total,
    )
