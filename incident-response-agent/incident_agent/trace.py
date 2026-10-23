"""The incident trace: every model turn, tool call and approval, per run.

The agent's observability collector hands each timeline entry to
:meth:`IncidentTrace.on_event` (a CALLBACK transporter). The approval policy
hands each decision to :meth:`IncidentTrace.on_decision`. When a run ends,
its entries are written to ``<trace_dir>/<run>.ndjson`` and a readable
``<run>.timeline.md``, so the team can replay what the agent did for one
alert without searching a shared log.
"""

from __future__ import annotations

import ast
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from promptise.observability import TimelineEntry

RUN_END = {"agent.output", "agent.error"}


def _arguments(value: Any) -> Any:
    """Tool arguments as a dict. The tool.call event carries them as the text LangChain
    passed to the callback (a Python repr), so parse that back when we can."""
    if not isinstance(value, str):
        return value
    for parse in (json.loads, ast.literal_eval):
        try:
            parsed = parse(value)
        except (ValueError, SyntaxError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return value


def _short(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")


@dataclass
class RunTrace:
    run_id: str
    started_at: float
    entries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def final_answer(self) -> str:
        for entry in reversed(self.entries):
            if entry["event_type"] == "agent.output":
                return entry["metadata"].get("output_preview") or ""
        return ""

    def tool_calls(self) -> list[dict[str, Any]]:
        """``{"tool", "arguments"}`` for every tool call, in the order the agent made them."""
        return [
            {"tool": e["metadata"].get("tool_name"), "arguments": e["metadata"].get("arguments")}
            for e in self.entries
            if e["event_type"] == "tool.call"
        ]

    def timeline(self) -> list[str]:
        lines = []
        for e in self.entries:
            meta = e["metadata"]
            kind = e["event_type"]
            if kind == "tool.call":
                raw = meta.get("arguments") or {}
                args = ", ".join(f"{k}={_short(v)}" for k, v in raw.items()) if isinstance(raw, dict) else _short(raw)
                text = f"call {meta.get('tool_name')}({args})"
            elif kind == "tool.result":
                preview = str(meta.get("result_preview") or "")
                outcome = "was denied" if preview.startswith("DENIED") else "returned"
                text = f"  -> {meta.get('tool_name')} {outcome} in {meta.get('latency_ms', 0):.0f} ms"
            elif kind == "tool.error":
                text = f"  -> {meta.get('tool_name')} failed: {meta.get('error')}"
            elif kind == "approval.decision":
                verdict = "approved" if meta["approved"] else "denied"
                text = f"approval {verdict} for {meta['tool_name']} by {meta['reviewer']}"
                if meta.get("reason"):
                    text += f" ({meta['reason']})"
            elif kind == "agent.input":
                text = "alert received, run started"
            elif kind == "agent.output":
                tools, turns = meta.get("tool_call_count", 0), meta.get("llm_call_count", 0)
                text = f"run finished: {tools} tool calls, {turns} model calls"
            elif kind == "agent.error":
                text = f"run failed: {meta.get('error_type')}: {meta.get('error')}"
            else:
                continue
            lines.append(f"{_clock(e['timestamp'])}  {text}")
        return lines


class IncidentTrace:
    """Collects timeline entries by run and exports each finished run."""

    def __init__(self, trace_dir: str | Path, on_run_end: Callable[[RunTrace, Path], None] | None = None) -> None:
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.on_run_end = on_run_end
        self.runs: list[RunTrace] = []
        self._by_id: dict[str, RunTrace] = {}
        self._lock = threading.Lock()

    # Called by the CALLBACK transporter for every timeline entry.
    def on_event(self, entry: TimelineEntry) -> None:
        data = entry.to_dict()
        if "arguments" in data["metadata"]:
            data["metadata"] = {**data["metadata"], "arguments": _arguments(data["metadata"]["arguments"])}
        with self._lock:
            if data["event_type"] == "agent.input":
                run = RunTrace(run_id=data["entry_id"], started_at=data["timestamp"])
                self.runs.append(run)
                self._by_id[run.run_id] = run
            else:
                run = self._by_id.get(data["parent_id"] or "") or (self.runs[-1] if self.runs else None)
            if run is None:
                return
            run.entries.append(data)
        if data["event_type"] in RUN_END:
            path = self.export(run)
            if self.on_run_end is not None:
                self.on_run_end(run, path)

    # Called by ApprovalPolicy(on_decision=...) for every decision, the gate's own included.
    def on_decision(self, request: Any, decision: Any) -> None:
        entry = {
            "entry_id": request.request_id,
            "timestamp": decision.timestamp,
            "event_type": "approval.decision",
            "metadata": {
                "tool_name": request.tool_name,
                "arguments": request.arguments,
                "approved": decision.approved,
                "reviewer": decision.reviewer_id or decision.decided_by,
                "decided_by": decision.decided_by,
                "reason": decision.reason,
            },
        }
        with self._lock:
            if self.runs:
                self.runs[-1].entries.append(entry)

    def export(self, run: RunTrace) -> Path:
        stamp = datetime.fromtimestamp(run.started_at, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = self.trace_dir / f"{stamp}-{run.run_id[:8]}"
        with open(base.with_suffix(".ndjson"), "w") as fh:
            for entry in run.entries:
                fh.write(json.dumps(entry, default=str) + "\n")
        base.with_suffix(".timeline.md").write_text("\n".join(run.timeline()) + "\n")
        return base.with_suffix(".ndjson")

    @property
    def last_run(self) -> RunTrace | None:
        return self.runs[-1] if self.runs else None
