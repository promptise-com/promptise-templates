"""Taskboard REST API: the product's existing public API.

This is the starting point: an ordinary FastAPI service with projects, tasks
and comments, authenticated with one API token per customer workspace.
Nothing in it knows about MCP or agents. The MCP server in ``mcp_server/``
sits in front of it and calls it over HTTP, the way it would call your API.

Run it on its own:  uvicorn taskboard_api.app:app --port 8000
Its OpenAPI document is served at /openapi.json (and exported to openapi.json).
"""

# No `from __future__ import annotations` here: FastAPI resolves the
# dependency aliases defined inside create_app() at runtime.
import json
import os
import secrets
from datetime import date
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .store import MEMBERS, WORKSPACES, Store, new_comment, new_project, new_task, seeded_store

TaskStatus = Literal["todo", "in_progress", "done"]


# -- models: descriptions here end up in openapi.json, and mcpcast reads them --


class Project(BaseModel):
    id: str = Field(description="Project id, for example 'prj_101'.")
    name: str
    description: str
    status: Literal["active", "archived"]
    open_tasks: int = Field(description="Tasks that are not done.")
    overdue_tasks: int = Field(description="Open tasks whose due date has passed.")
    created_at: str


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, description="Project name.")
    description: str = Field(default="", max_length=2000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    status: Literal["active", "archived"] | None = Field(
        default=None, description="Archive a finished project, or reactivate it."
    )


class Task(BaseModel):
    id: str = Field(description="Task id, for example 'tsk_1001'.")
    project_id: str
    title: str
    description: str
    assignee: str | None = Field(description="Member handle, for example 'maya'.")
    status: TaskStatus
    due_date: str | None = Field(description="ISO date, for example '2025-03-01'.")
    overdue: bool
    created_at: str


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    assignee: str | None = Field(default=None, description="Member handle.")
    due_date: date | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: TaskStatus | None = None
    assignee: str | None = None
    due_date: date | None = None


class Comment(BaseModel):
    id: str
    task_id: str
    author: str
    body: str
    created_at: str


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    author: str = Field(description="Who wrote the comment: a member handle or an integration name.")


class Member(BaseModel):
    handle: str
    name: str
    role: str


# -- auth: one bearer token per workspace ---------------------------------------


def _tokens_from_env() -> dict[str, str]:
    """TASKBOARD_API_TOKENS is JSON: {"acme": "<token>", "globex": "<token>"}."""
    raw = os.environ.get("TASKBOARD_API_TOKENS", "")
    return json.loads(raw) if raw else {}


def create_app(tokens: dict[str, str] | None = None, store: Store | None = None) -> FastAPI:
    """Build the API. ``tokens`` maps workspace id to its API token."""
    tokens = _tokens_from_env() if tokens is None else tokens
    unknown = set(tokens) - set(WORKSPACES)
    if unknown:
        raise ValueError(f"tokens for unknown workspaces: {sorted(unknown)}")

    app = FastAPI(
        title="Taskboard API",
        version="2.3.0",
        description="Projects, tasks and comments for Taskboard workspaces.",
    )
    app.state.store = store or seeded_store()
    bearer = HTTPBearer(auto_error=False)

    def workspace(
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        if creds is not None:
            for ws, token in tokens.items():
                if secrets.compare_digest(creds.credentials, token):
                    return ws
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API token")

    Workspace = Annotated[str, Depends(workspace)]

    def db(request: Request) -> Store:
        return request.app.state.store

    DB = Annotated[Store, Depends(db)]

    def project_or_404(store: Store, ws: str, project_id: str) -> dict:
        project = store.get_project(ws, project_id)
        if project is None:
            raise HTTPException(404, f"Project {project_id} not found")
        return project

    def task_or_404(store: Store, ws: str, task_id: str) -> dict:
        task = store.get_task(ws, task_id)
        if task is None:
            raise HTTPException(404, f"Task {task_id} not found")
        return task

    @app.get("/health", operation_id="health_check", summary="Liveness probe", tags=["ops"])
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/members", operation_id="list_members", summary="List workspace members", tags=["members"])
    def list_members(ws: Workspace) -> list[Member]:
        return [Member(**m) for m in MEMBERS[ws]]

    @app.get("/projects", operation_id="list_projects", summary="List projects", tags=["projects"])
    def list_projects(
        ws: Workspace,
        store: DB,
        status_: Annotated[
            Literal["active", "archived"] | None,
            Query(alias="status", description="Only projects with this status."),
        ] = None,
    ) -> list[Project]:
        return [Project(**p) for p in store.list_projects(ws, status_)]

    @app.post(
        "/projects", operation_id="create_project", summary="Create a project", status_code=201, tags=["projects"]
    )
    def create_project(ws: Workspace, store: DB, body: ProjectCreate) -> Project:
        return Project(**new_project(store, ws, body.name, body.description))

    @app.get("/projects/{project_id}", operation_id="get_project", summary="Get a project", tags=["projects"])
    def get_project(ws: Workspace, store: DB, project_id: str) -> Project:
        return Project(**project_or_404(store, ws, project_id))

    @app.patch(
        "/projects/{project_id}",
        operation_id="update_project",
        summary="Rename, archive or reactivate a project",
        tags=["projects"],
    )
    def update_project(ws: Workspace, store: DB, project_id: str, body: ProjectUpdate) -> Project:
        project_or_404(store, ws, project_id)
        store.projects[project_id].update(body.model_dump(exclude_none=True))
        return Project(**project_or_404(store, ws, project_id))

    @app.delete(
        "/projects/{project_id}",
        operation_id="delete_project",
        summary="Delete a project with all its tasks and comments",
        description="Permanent. There is no undo.",
        status_code=204,
        tags=["projects"],
    )
    def delete_project(ws: Workspace, store: DB, project_id: str) -> Response:
        if store.delete_project(ws, project_id) < 0:
            raise HTTPException(404, f"Project {project_id} not found")
        return Response(status_code=204)

    @app.get("/tasks", operation_id="list_tasks", summary="Find tasks across projects", tags=["tasks"])
    def list_tasks(
        ws: Workspace,
        store: DB,
        project_id: Annotated[str | None, Query(description="Only tasks in this project.")] = None,
        status_: Annotated[TaskStatus | None, Query(alias="status")] = None,
        assignee: Annotated[str | None, Query(description="Member handle.")] = None,
        overdue: Annotated[bool, Query(description="Only open tasks past their due date.")] = False,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[Task]:
        rows = store.find_tasks(ws, project_id=project_id, status=status_, assignee=assignee, overdue=overdue)
        return [Task(**t) for t in rows[:limit]]

    @app.post(
        "/projects/{project_id}/tasks",
        operation_id="create_task",
        summary="Create a task in a project",
        status_code=201,
        tags=["tasks"],
    )
    def create_task(ws: Workspace, store: DB, project_id: str, body: TaskCreate) -> Task:
        project_or_404(store, ws, project_id)
        fields = body.model_dump(exclude_none=True)
        if "due_date" in fields:
            fields["due_date"] = fields["due_date"].isoformat()
        _check_assignee(ws, fields.get("assignee"))
        return Task(**new_task(store, ws, project_id, fields))

    @app.get("/tasks/{task_id}", operation_id="get_task", summary="Get a task", tags=["tasks"])
    def get_task(ws: Workspace, store: DB, task_id: str) -> Task:
        return Task(**task_or_404(store, ws, task_id))

    @app.patch("/tasks/{task_id}", operation_id="update_task", summary="Update a task", tags=["tasks"])
    def update_task(ws: Workspace, store: DB, task_id: str, body: TaskUpdate) -> Task:
        task_or_404(store, ws, task_id)
        fields = body.model_dump(exclude_none=True)
        if "due_date" in fields:
            fields["due_date"] = fields["due_date"].isoformat()
        _check_assignee(ws, fields.get("assignee"))
        store.tasks[task_id].update(fields)
        return Task(**task_or_404(store, ws, task_id))

    @app.delete(
        "/tasks/{task_id}", operation_id="delete_task", summary="Delete a task", status_code=204, tags=["tasks"]
    )
    def delete_task(ws: Workspace, store: DB, task_id: str) -> Response:
        task_or_404(store, ws, task_id)
        store.tasks.pop(task_id)
        store.comments.pop(task_id, None)
        return Response(status_code=204)

    @app.get(
        "/tasks/{task_id}/comments", operation_id="list_comments", summary="List a task's comments", tags=["comments"]
    )
    def list_comments(ws: Workspace, store: DB, task_id: str) -> list[Comment]:
        task_or_404(store, ws, task_id)
        return [Comment(**c) for c in store.comments.get(task_id, [])]

    @app.post(
        "/tasks/{task_id}/comments",
        operation_id="add_comment",
        summary="Comment on a task",
        status_code=201,
        tags=["comments"],
    )
    def add_comment(ws: Workspace, store: DB, task_id: str, body: CommentCreate) -> Comment:
        task_or_404(store, ws, task_id)
        return Comment(**new_comment(store, task_id, body.author, body.body))

    return app


def _check_assignee(ws: str, handle: str | None) -> None:
    if handle is not None and handle not in {m["handle"] for m in MEMBERS[ws]}:
        raise HTTPException(422, f"Unknown member '{handle}'")


# uvicorn taskboard_api.app:app reads the tokens from the environment.
app = create_app()
