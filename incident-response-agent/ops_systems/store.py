"""The fake production world behind the ops MCP server.

Seed data lives in ``seed/`` with times given as "minutes ago". The world
anchors those times to the moment its state directory was created, so a log
line seeded at ``minutes_ago: 14`` reads as 14 minutes before the alert.

Everything the agent changes (incidents, status posts, restarts, rollbacks)
is written to ``world.json`` in the state directory. Tests and the demo read
that file to check what really happened, independent of what the agent says.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SEED_DIR = Path(__file__).parent / "seed"
FIRST_INCIDENT_NUMBER = 1042


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class OpsError(ValueError):
    """A request the fake systems refuse, with a message the agent can act on."""


@dataclass
class Runbook:
    alert: str
    title: str
    path: str
    body: str


def _load_runbooks(seed: Path) -> dict[str, Runbook]:
    books: dict[str, Runbook] = {}
    for path in sorted((seed / "runbooks").glob("*.md")):
        text = path.read_text()
        match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
        if not match:
            continue
        meta = dict(line.split(": ", 1) for line in match.group(1).splitlines() if ": " in line)
        books[meta["alert"]] = Runbook(
            alert=meta["alert"],
            title=meta.get("title", ""),
            path=f"runbooks/{path.name}",
            body=match.group(2).strip(),
        )
    return books


class OpsWorld:
    """Logs, metrics, deploys, runbooks, incidents and a status channel."""

    def __init__(self, state_dir: str | os.PathLike[str], seed_dir: Path = SEED_DIR) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "world.json"
        self.services: dict[str, Any] = json.loads((seed_dir / "services.json").read_text())
        self.deploys: list[dict[str, Any]] = json.loads((seed_dir / "deploys.json").read_text())
        self.logs = [json.loads(line) for line in (seed_dir / "logs.jsonl").read_text().splitlines() if line]
        self.runbooks = _load_runbooks(seed_dir)
        self.state = self._load_state()

    # ── state ────────────────────────────────────────────────────────────

    def _load_state(self) -> dict[str, Any]:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        state = {
            "anchor": iso(utcnow()),
            "versions": {name: svc["current_version"] for name, svc in self.services.items()},
            "replicas": {name: svc["replicas"] for name, svc in self.services.items()},
            "incidents": {},
            "status_updates": [],
            "actions": [],
            "seq": 0,
        }
        self._save(state)
        return state

    def _save(self, state: dict[str, Any] | None = None) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state or self.state, indent=2))
        tmp.replace(self.state_file)

    @property
    def anchor(self) -> datetime:
        return datetime.fromisoformat(self.state["anchor"].replace("Z", "+00:00"))

    def _at(self, minutes_ago: float) -> datetime:
        return self.anchor - timedelta(minutes=minutes_ago)

    def _next_seq(self) -> int:
        # Orders events recorded in the same second (status posts, changes).
        self.state["seq"] += 1
        return self.state["seq"]

    def _service(self, service: str) -> dict[str, Any]:
        if service not in self.services:
            known = ", ".join(sorted(self.services))
            raise OpsError(f"Unknown service '{service}'. Known services: {known}.")
        return self.services[service]

    # ── read-only systems ────────────────────────────────────────────────

    def search_logs(self, service: str, minutes: int, level: str, limit: int) -> dict[str, Any]:
        self._service(service)
        levels = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
        floor = levels.get(level.upper(), 3)
        lines = [
            {"time": iso(self._at(row["minutes_ago"])), **{k: v for k, v in row.items() if k != "minutes_ago"}}
            for row in self.logs
            if row["service"] == service and row["minutes_ago"] <= minutes and levels.get(row["level"], 1) >= floor
        ]
        top = Counter(line["message"] for line in lines).most_common(3)
        return {
            "service": service,
            "window_minutes": minutes,
            "min_level": level.upper(),
            "matches": len(lines),
            "top_messages": [{"message": m, "count": c} for m, c in top],
            "lines": lines[-limit:],
        }

    def _current_value(self, service: str, metric: str) -> float:
        spec = self._service(service)["metrics"][metric]
        fixed_by = spec.get("fixed_by_version")
        if "incident" not in spec or self.state["versions"][service] == fixed_by:
            return spec["baseline"]
        return spec["incident"]

    def query_metric(self, service: str, metric: str, minutes: int) -> dict[str, Any]:
        spec = self._service(service)["metrics"].get(metric)
        if spec is None:
            known = ", ".join(self.services[service]["metrics"])
            raise OpsError(f"Unknown metric '{metric}'. Metrics for {service}: {known}.")
        since = spec.get("incident_since_minutes_ago")
        points = []
        for ago in range(minutes, 0, -1):
            in_incident = since is not None and ago <= since
            base = spec["incident"] if in_incident else spec["baseline"]
            # Small deterministic wobble so the series looks like a real scrape.
            wobble = ((ago * 37) % 7 - 3) / 100 * base
            points.append({"time": iso(self._at(ago)), "value": round(base + wobble, 2)})
        current = self._current_value(service, metric)
        return {
            "service": service,
            "metric": metric,
            "unit": spec["unit"],
            "current": current,
            "baseline": spec["baseline"],
            "series": points,
            "note": "current is the live value now, after any change made during this incident",
        }

    def list_deploys(self, service: str, hours: int) -> dict[str, Any]:
        self._service(service)
        rows = [
            {"finished_at": iso(self._at(d["minutes_ago"])), **{k: v for k, v in d.items() if k != "minutes_ago"}}
            for d in self.deploys
            if d["service"] == service and d["minutes_ago"] <= hours * 60
        ]
        rollbacks = [a for a in self.state["actions"] if a["action"] == "rollback" and a["service"] == service]
        return {
            "service": service,
            "running_version": self.state["versions"][service],
            "deploys": rows,
            "rollbacks_during_incident": rollbacks,
        }

    def find_runbook(self, alert_name: str) -> dict[str, Any]:
        book = self.runbooks.get(alert_name)
        if book is None:
            known = ", ".join(sorted(self.runbooks))
            raise OpsError(f"No runbook for alert '{alert_name}'. Runbooks exist for: {known}.")
        return {"alert": book.alert, "title": book.title, "path": book.path, "runbook": book.body}

    # ── incident management and chat ─────────────────────────────────────

    def open_incident(self, alert_name: str, service: str, severity: str, fingerprint: str) -> dict[str, Any]:
        self._service(service)
        for incident in self.state["incidents"].values():
            # Alertmanager repeats a firing alert; the same fingerprint is the same incident.
            if incident["fingerprint"] == fingerprint and incident["status"] != "resolved":
                return {**incident, "already_open": True}
        number = FIRST_INCIDENT_NUMBER + len(self.state["incidents"])
        incident = {
            "incident_id": f"INC-{number}",
            "channel": f"#inc-{number}-{service}",
            "alert": alert_name,
            "service": service,
            "severity": severity,
            "fingerprint": fingerprint,
            "status": "investigating",
            "opened_at": iso(utcnow()),
        }
        self.state["incidents"][incident["incident_id"]] = incident
        self._save()
        return {**incident, "already_open": False}

    def _incident(self, incident_id: str) -> dict[str, Any]:
        incident = self.state["incidents"].get(incident_id)
        if incident is None:
            raise OpsError(f"Unknown incident '{incident_id}'. Open one with open_incident first.")
        return incident

    def post_status_update(self, incident_id: str, message: str) -> dict[str, Any]:
        incident = self._incident(incident_id)
        post = {
            "time": iso(utcnow()),
            "seq": self._next_seq(),
            "channel": incident["channel"],
            "incident_id": incident_id,
            "message": message,
        }
        self.state["status_updates"].append(post)
        self._save()
        return {"posted": True, **post}

    def _recorded_events(self, incident_id: str) -> list[dict[str, str]]:
        """What the incident tool saw itself: opening, status posts and changes to production."""
        incident = self.state["incidents"][incident_id]
        opened = f"Incident {incident_id} opened for {incident['alert']}"
        events = [{"time": incident["opened_at"], "seq": 0, "event": opened}]
        events += [
            {"time": post["time"], "seq": post["seq"], "event": f"Status update: {post['message']}"}
            for post in self.state["status_updates"]
            if post["incident_id"] == incident_id
        ]
        for action in self.state["actions"]:
            if action["service"] != incident["service"] or action["time"] < incident["opened_at"]:
                continue
            if action["action"] == "rollback":
                text = f"Rolled back {action['service']} from {action['from_version']} to {action['to_version']}"
            elif action["action"] == "restart":
                text = f"Restarted {action['service']}"
            else:
                text = f"Scaled {action['service']} from {action['from_replicas']} to {action['to_replicas']} replicas"
            events.append({"time": action["time"], "seq": action["seq"], "event": text})
        return events

    def write_incident_summary(
        self,
        incident_id: str,
        status: str,
        summary: str,
        root_cause: str,
        findings: list[dict[str, str]],
        follow_ups: list[str],
    ) -> dict[str, Any]:
        incident = self._incident(incident_id)
        incident["status"] = status
        incident["summary_written_at"] = iso(utcnow())
        # The agent's findings plus the tool's own record, so the timeline never depends
        # on the agent remembering every time correctly.
        timeline = sorted(
            [
                *({**f, "source": "agent"} for f in findings),
                *({**e, "source": "recorded"} for e in self._recorded_events(incident_id)),
            ],
            key=lambda row: (row.get("time", ""), row.get("seq", -1)),
        )
        for row in timeline:
            row.pop("seq", None)
        lines = [
            f"# {incident_id}: {incident['alert']} on {incident['service']}",
            "",
            f"- Status: {status}",
            f"- Severity: {incident['severity']}",
            f"- Opened: {incident['opened_at']}",
            f"- Channel: {incident['channel']}",
            "",
            "## Summary",
            "",
            summary,
            "",
            "## Root cause",
            "",
            root_cause,
            "",
            "## Timeline (UTC)",
            "",
            *[f"- {row.get('time', '')} {row.get('event', '')}" for row in timeline],
            "",
            "## Follow-ups",
            "",
            *[f"- {item}" for item in follow_ups],
            "",
        ]
        path = self.state_dir / "incidents" / f"{incident_id}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(lines))
        incident["summary_path"] = str(path.relative_to(self.state_dir))
        incident["timeline"] = timeline
        self._save()
        return {
            "saved": True,
            "incident_id": incident_id,
            "status": status,
            "path": incident["summary_path"],
            "timeline_events": len(timeline),
        }

    # ── risky remediation (gated by approval in the agent) ───────────────

    def _record(self, action: str, service: str, **details: Any) -> dict[str, Any]:
        entry = {"time": iso(utcnow()), "seq": self._next_seq(), "action": action, "service": service, **details}
        self.state["actions"].append(entry)
        self._save()
        return entry

    def restart_service(self, service: str, reason: str) -> dict[str, Any]:
        svc = self._service(service)
        entry = self._record("restart", service, reason=reason, replicas=self.state["replicas"][service])
        return {"restarted": True, "version": self.state["versions"][service], "team": svc["team"], **entry}

    def rollback_deploy(self, service: str, to_version: str, reason: str) -> dict[str, Any]:
        self._service(service)
        known = {d["version"] for d in self.deploys if d["service"] == service}
        known |= {d["previous_version"] for d in self.deploys if d["service"] == service}
        if to_version not in known:
            versions = ", ".join(sorted(known))
            raise OpsError(f"Version '{to_version}' of {service} was never deployed. Known: {versions}.")
        current = self.state["versions"][service]
        if to_version == current:
            raise OpsError(f"{service} already runs {to_version}.")
        self.state["versions"][service] = to_version
        entry = self._record("rollback", service, from_version=current, to_version=to_version, reason=reason)
        return {"rolled_back": True, **entry}

    def scale_service(self, service: str, replicas: int, reason: str) -> dict[str, Any]:
        self._service(service)
        before = self.state["replicas"][service]
        self.state["replicas"][service] = replicas
        entry = self._record("scale", service, from_replicas=before, to_replicas=replicas, reason=reason)
        return {"scaled": True, **entry}
