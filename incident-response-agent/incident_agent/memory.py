"""Memory of past incidents, kept per service.

A ``promptise.memory.InMemoryProvider`` with ``MemoryScope.PER_USER`` holds
the lessons. The scope key is the service name, so a search for
``checkout-service`` can never return a ``cart-service`` incident.

The provider lives in memory, so every lesson is also appended to
``memory.jsonl`` in the state directory and loaded again at start-up.
``memory_seed.jsonl`` holds older incidents, as if imported from the
incident tool. For semantic search across many incidents, swap in
``ChromaProvider(persist_directory=..., scope=MemoryScope.PER_USER)``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from promptise import tool
from promptise.memory import InMemoryProvider, MemoryScope
from promptise.runtime import HookContext

from incident_agent.trace import IncidentTrace

SEED = Path(__file__).parent / "memory_seed.jsonl"


def lesson_text(row: dict[str, Any]) -> str:
    follow_ups = "; ".join(row.get("follow_ups") or [])
    return (
        f"{row['incident_id']} ({row.get('date', '')}, {row.get('alert', '')}, {row['status']}): "
        f"{row['summary']} Root cause: {row['root_cause']} Follow-ups: {follow_ups}"
    )


class IncidentMemory:
    def __init__(self, state_dir: Path, seed: Path | None = SEED) -> None:
        self.path = Path(state_dir) / "memory.jsonl"
        self.provider = InMemoryProvider(scope=MemoryScope.PER_USER)
        self._seed = seed

    async def load(self) -> int:
        """Load the seed and every lesson learned so far. Returns how many entries were loaded."""
        rows: list[dict[str, Any]] = []
        for source in (self._seed, self.path):
            if source is not None and source.exists():
                rows += [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
        for row in rows:
            await self.provider.add(lesson_text(row), metadata=row, user_id=row["service"])
        return len(rows)

    async def recall(self, service: str, query: str = "", limit: int = 5) -> list[dict[str, Any]]:
        if query:
            results = await self.provider.search(query, limit=limit, user_id=service)
        else:
            # Newest first: the last lessons added are the most recent incidents.
            results = list(reversed(await self.provider.list_entries(user_id=service)))[:limit]
        return [{"incident": r.content, "score": round(r.score, 2)} for r in results]

    async def remember(self, row: dict[str, Any]) -> None:
        await self.provider.add(lesson_text(row), metadata=row, user_id=row["service"])
        with open(self.path, "a") as fh:
            fh.write(json.dumps(row) + "\n")

    def recall_tool(self):
        """The agent's tool for reading this memory."""
        memory = self

        @tool
        async def recall_past_incidents(service: str, query: str = "") -> list[dict]:
            """Past incidents on one service, with their root cause and follow-ups.

            Args:
                service: The service name, for example "checkout-service".
                query: Optional words to rank by, for example "deploy timeout". Empty lists the latest.
            """
            return await memory.recall(service, query)

        return recall_past_incidents

    def lesson_hook(self, trace: IncidentTrace):
        """A ``task_completed`` hook: store what the finished run wrote in its incident summary."""
        memory = self

        async def remember_finished_run(ctx: HookContext) -> None:
            run = trace.last_run
            if run is None:
                return
            calls = {c["tool"]: c["arguments"] for c in run.tool_calls() if isinstance(c["arguments"], dict)}
            summary, opened = calls.get("write_incident_summary"), calls.get("open_incident")
            if not summary or not opened:
                return  # nothing was written up; nothing to learn
            await memory.remember(
                {
                    "service": opened["service"],
                    "incident_id": summary["incident_id"],
                    "date": run.date,
                    "alert": opened.get("alert_name", ""),
                    "status": summary["status"],
                    "summary": summary["summary"],
                    "root_cause": summary["root_cause"],
                    "follow_ups": summary.get("follow_ups") or [],
                }
            )

        return remember_finished_run
