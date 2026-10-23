"""In-memory data for the Taskboard API, with realistic seed data.

This stands in for the product database. Every query is scoped to one
workspace, the same way a multi-tenant SaaS scopes rows by tenant id.
Due dates are relative to today, so "overdue" always means something.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

WORKSPACES = {
    "acme": "Acme Robotics",
    "globex": "Globex Logistics",
}

MEMBERS = {
    "acme": [
        {"handle": "maya", "name": "Maya Chen", "role": "Marketing lead"},
        {"handle": "leo", "name": "Leo Novak", "role": "Web developer"},
        {"handle": "priya", "name": "Priya Raman", "role": "Customer success"},
        {"handle": "sam", "name": "Sam Okafor", "role": "Product designer"},
    ],
    "globex": [
        {"handle": "ines", "name": "Ines Duarte", "role": "Operations manager"},
        {"handle": "tom", "name": "Tom Berg", "role": "Warehouse lead"},
    ],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Store:
    """All projects, tasks and comments, for every workspace."""

    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    comments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(5000))

    def next_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids)}"

    # -- projects ------------------------------------------------------------

    def list_projects(self, workspace: str, status: str | None) -> list[dict[str, Any]]:
        rows = [p for p in self.projects.values() if p["workspace"] == workspace]
        if status:
            rows = [p for p in rows if p["status"] == status]
        return [self._with_counts(p) for p in rows]

    def get_project(self, workspace: str, project_id: str) -> dict[str, Any] | None:
        project = self.projects.get(project_id)
        if project is None or project["workspace"] != workspace:
            return None
        return self._with_counts(project)

    def _with_counts(self, project: dict[str, Any]) -> dict[str, Any]:
        tasks = [t for t in self.tasks.values() if t["project_id"] == project["id"]]
        return {
            **project,
            "open_tasks": sum(1 for t in tasks if t["status"] != "done"),
            "overdue_tasks": sum(1 for t in tasks if is_overdue(t)),
        }

    def delete_project(self, workspace: str, project_id: str) -> int:
        """Delete a project with its tasks and comments; returns the tasks removed."""
        if self.get_project(workspace, project_id) is None:
            return -1
        task_ids = [t["id"] for t in self.tasks.values() if t["project_id"] == project_id]
        for task_id in task_ids:
            self.tasks.pop(task_id)
            self.comments.pop(task_id, None)
        self.projects.pop(project_id)
        return len(task_ids)

    # -- tasks ---------------------------------------------------------------

    def get_task(self, workspace: str, task_id: str) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        if task is None or task["workspace"] != workspace:
            return None
        return {**task, "overdue": is_overdue(task)}

    def find_tasks(
        self,
        workspace: str,
        *,
        project_id: str | None = None,
        status: str | None = None,
        assignee: str | None = None,
        overdue: bool = False,
    ) -> list[dict[str, Any]]:
        rows = [t for t in self.tasks.values() if t["workspace"] == workspace]
        if project_id:
            rows = [t for t in rows if t["project_id"] == project_id]
        if status:
            rows = [t for t in rows if t["status"] == status]
        if assignee:
            rows = [t for t in rows if t["assignee"] == assignee]
        if overdue:
            rows = [t for t in rows if is_overdue(t)]
        rows.sort(key=lambda t: (t["due_date"] or "9999-12-31", t["id"]))
        return [{**t, "overdue": is_overdue(t)} for t in rows]


def is_overdue(task: dict[str, Any]) -> bool:
    due = task.get("due_date")
    return bool(due) and task["status"] != "done" and date.fromisoformat(due) < date.today()


def seeded_store() -> Store:
    """A fresh store with two customer workspaces in it."""
    store = Store()
    today = date.today()

    def day(offset: int) -> str:
        return (today + timedelta(days=offset)).isoformat()

    projects = [
        ("prj_101", "acme", "Website relaunch", "New marketing site before the spring launch.", "active"),
        ("prj_102", "acme", "Customer onboarding revamp", "Shorter time to first value.", "active"),
        ("prj_103", "acme", "2024 trade show", "Booth, travel and follow-ups.", "archived"),
        ("prj_201", "globex", "Warehouse scanner rollout", "Handheld scanners for all docks.", "active"),
    ]
    for pid, ws, name, desc, status in projects:
        store.projects[pid] = {
            "id": pid,
            "workspace": ws,
            "name": name,
            "description": desc,
            "status": status,
            "created_at": "2025-01-15T09:00:00+00:00",
        }

    tasks = [
        ("tsk_1001", "prj_101", "Write homepage copy", "maya", "in_progress", day(-3)),
        ("tsk_1002", "prj_101", "Migrate blog posts", "leo", "todo", day(-1)),
        ("tsk_1003", "prj_101", "Set up redirects from old URLs", "leo", "todo", day(5)),
        ("tsk_1004", "prj_101", "Choose a hosting plan", "maya", "done", day(-10)),
        ("tsk_1005", "prj_102", "Draft the welcome email sequence", "priya", "in_progress", day(2)),
        ("tsk_1006", "prj_102", "Record a product tour video", "sam", "todo", day(9)),
        ("tsk_1007", "prj_103", "Book the booth", "maya", "done", day(-200)),
        ("tsk_2001", "prj_201", "Order 40 scanners", "ines", "todo", day(-2)),
        ("tsk_2002", "prj_201", "Train the night shift", "tom", "todo", day(12)),
    ]
    for tid, pid, title, assignee, status, due in tasks:
        store.tasks[tid] = {
            "id": tid,
            "project_id": pid,
            "workspace": store.projects[pid]["workspace"],
            "title": title,
            "description": "",
            "assignee": assignee,
            "status": status,
            "due_date": due,
            "created_at": "2025-02-01T09:00:00+00:00",
        }

    store.comments["tsk_1001"] = [
        {
            "id": "cmt_1",
            "task_id": "tsk_1001",
            "author": "maya",
            "body": "First draft is in the shared doc.",
            "created_at": "2025-02-03T10:12:00+00:00",
        },
    ]
    store.comments["tsk_1002"] = [
        {
            "id": "cmt_2",
            "task_id": "tsk_1002",
            "author": "leo",
            "body": "Blocked until the CMS export works.",
            "created_at": "2025-02-04T16:40:00+00:00",
        },
    ]
    return store


def new_comment(store: Store, task_id: str, author: str, body: str) -> dict[str, Any]:
    comment = {
        "id": store.next_id("cmt"),
        "task_id": task_id,
        "author": author,
        "body": body,
        "created_at": _now(),
    }
    store.comments.setdefault(task_id, []).append(comment)
    return comment


def new_task(store: Store, workspace: str, project_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    task = {
        "id": store.next_id("tsk"),
        "project_id": project_id,
        "workspace": workspace,
        "description": "",
        "assignee": None,
        "status": "todo",
        "due_date": None,
        "created_at": _now(),
        **fields,
    }
    store.tasks[task["id"]] = task
    return {**task, "overdue": is_overdue(task)}


def new_project(store: Store, workspace: str, name: str, description: str) -> dict[str, Any]:
    project = {
        "id": store.next_id("prj"),
        "workspace": workspace,
        "name": name,
        "description": description,
        "status": "active",
        "created_at": _now(),
    }
    store.projects[project["id"]] = project
    return store._with_counts(project)
