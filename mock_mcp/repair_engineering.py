"""Candidate engineering tools with exact identifiers and stored outcomes."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import re
from typing import Literal

from mock_mcp.repair_support import CandidateStore, date_value, integer, nonblank, one_record


INCIDENT_STATUS = Literal["open", "acknowledged", "resolved", "closed"]
DEPLOY_ENV = Literal["dev", "staging", "prod"]
DEPLOY_STRATEGY = Literal["direct", "canary"]
DEPLOY_STATUS = Literal["queued", "in_progress", "completed", "failed", "rolled_back"]


def engineering_services(state: dict) -> list[str]:
    names = set()
    supplied = state.get("services", [])
    if isinstance(supplied, dict):
        names.update(nonblank(key, "Stored service name") for key in supplied)
    elif isinstance(supplied, list):
        for row in supplied:
            names.add(nonblank(row.get("name") if isinstance(row, dict) else row, "Stored service name"))
    else:
        raise ValueError("Stored services must be a list or object.")
    metrics = state.get("service_metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError("Stored service_metrics must be an object keyed by service name.")
    names.update(nonblank(key, "Stored service name") for key in metrics)
    for collection in ("runbooks", "deploys", "deployments", "logs"):
        rows = state.get(collection, [])
        if not isinstance(rows, list):
            raise ValueError(f"Stored {collection} must be a list.")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"Each stored {collection} record must be an object.")
            if row.get("service") is not None:
                names.add(nonblank(row["service"], "Stored service name"))
    incidents = state.get("incidents", [])
    if not isinstance(incidents, list):
        raise ValueError("Stored incidents must be a list.")
    for incident in incidents:
        if not isinstance(incident, dict):
            raise ValueError("Each stored incident must be an object.")
        for service in incident.get("services_affected", []):
            names.add(nonblank(service, "Stored service name"))
    return sorted(names)


def incident_records(state: dict) -> list[dict]:
    rows = state.get("incidents", [])
    if not isinstance(rows, list):
        raise ValueError("Stored incidents must be a list.")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each stored incident must be an object.")
        key = nonblank(row.get("id") or row.get("incident_id"), "Stored incident id")
        normalized.append(row if row.get("id") == key else {**row, "id": key})
    return normalized


def deploy_records(state: dict) -> list[dict]:
    records = {}
    for collection in ("deploys", "deployments"):
        rows = state.get(collection, [])
        if not isinstance(rows, list):
            raise ValueError(f"Stored {collection} must be a list.")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Each stored deployment must be an object.")
            key = nonblank(row.get("deploy_id") or row.get("id"), "Stored deployment id")
            if key in records and records[key] != row:
                raise ValueError("Two stored deployments disagree for the same id.")
            records[key] = row
    return list(records.values())


def time_window(value: str) -> timedelta:
    raw = nonblank(value, "timerange").strip()
    match = re.fullmatch(r"([1-9][0-9]*)(m|h|d)", raw)
    if not match:
        raise ValueError("timerange must be a positive number followed by m, h, or d, such as 30m, 6h, or 7d.")
    amount = int(match.group(1))
    return {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[match.group(2)]


def owner_key(value: str, field: str) -> str:
    key = nonblank(value, field)
    if key != key.strip().casefold() or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", key):
        raise ValueError(f"{field} must be a lowercase person or team key using letters, digits, _ or -.")
    return key


class CandidateEngineering:
    def __init__(self, store: CandidateStore, directory: dict):
        self.store = store
        self.user_keys = {row["key"] for row in directory.get("users", [])}

    def list_services(self) -> str:
        """List the exact, case-sensitive service names known to the workspace."""
        return self.store.run("list_services", {}, lambda state: {"services": engineering_services(state)})

    def list_service_metrics(self, service: str) -> str:
        """List the exact metric names stored for one service from list_services.

        A service may exist without stored metrics; that returns an empty list.
        Metric names are service-specific and case-sensitive.
        """
        def read(state):
            if service not in engineering_services(state):
                raise ValueError("Unknown service. Use list_services for an exact name.")
            metrics = state.get("service_metrics", {}).get(service, {})
            if not isinstance(metrics, dict):
                raise ValueError("Stored metrics for the service must be an object keyed by metric name.")
            return {"service": service, "metrics": sorted(
                nonblank(name, "Stored metric name") for name in metrics
            )}
        return self.store.run("list_service_metrics", {"service": service}, read)

    def list_oncall_schedule(self, team: str | None = None, week_of: str | None = None) -> str:
        """List stored on-call rotations, optionally filtered by exact team and date.

        team is a lowercase key such as infra. week_of must be YYYY-MM-DD. Omit
        both filters to list every stored rotation. Returned person keys can be
        used when an incident must be escalated.
        """
        args = {"team": team, "week_of": week_of}
        def read(state):
            if team is not None:
                nonblank(team, "team")
            if week_of is not None:
                date_value(week_of)
            rows = state.get("oncall_schedule", [])
            if not isinstance(rows, list):
                raise ValueError("Stored oncall_schedule must be a list.")
            result = [row for row in rows if (team is None or row.get("team") == team)
                      and (week_of is None or row.get("week_of") == week_of)]
            return {"count": len(result), "rotations": result}
        return self.store.run("list_oncall_schedule", args, read)

    def list_incidents(self, status: INCIDENT_STATUS | None = None) -> str:
        """List incidents and their exact ids. status may be open, acknowledged,
        resolved, closed, or null for every status.
        """
        def read(state):
            if status is not None and status not in ("open", "acknowledged", "resolved", "closed"):
                raise ValueError("status must be open, acknowledged, resolved, closed, or null.")
            rows = incident_records(state)
            return [row for row in rows if status is None or row.get("status") == status]
        return self.store.run("list_incidents", {"status": status}, read)

    def acknowledge_incident(self, incident_id: str) -> str:
        """Acknowledge one existing open incident selected by id from list_incidents.

        An unknown, resolved, or closed incident is rejected and no incident is created.
        Returns the complete stored incident after acknowledgement.
        """
        def acknowledge(state):
            incident = one_record(incident_records(state), incident_id)
            if incident.get("status") != "open":
                raise ValueError("Only an open incident can be acknowledged.")
            target = next(row for row in state["incidents"]
                          if (row.get("id") or row.get("incident_id")) == incident_id)
            target.update(status="acknowledged", acknowledged_at=self.store.backend._now_iso())
            return {"ok": True, "incident": {**target, "id": incident_id}}
        return self.store.run("acknowledge_incident", {"incident_id": incident_id}, acknowledge, write=True)

    def escalate_incident(self, incident_id: str, to: str) -> str:
        """Escalate one open or acknowledged incident to an exact workspace user key.

        incident_id comes from list_incidents. to comes from list_workspace_users;
        use the named person, not a role such as secondary. Capitalization and outer
        whitespace in to are ignored. Returns the complete stored escalation record.
        """
        args = {"incident_id": incident_id, "to": to}
        def escalate(state):
            incident = one_record(incident_records(state), incident_id)
            if incident.get("status") not in ("open", "acknowledged"):
                raise ValueError("Only an open or acknowledged incident can be escalated.")
            recipient = nonblank(to, "to").strip().casefold()
            if recipient not in self.user_keys:
                raise ValueError("to must be an exact user key from list_workspace_users, not a role or display name.")
            row = self.store.new_record("escalation", {"incident_id": incident_id, "to": recipient,
                                                       "escalated_at": self.store.backend._now_iso()})
            state.setdefault("incident_escalations", []).append(row)
            return {"ok": True, "escalation": row}
        return self.store.run("escalate_incident", args, escalate, write=True)

    def create_runbook_entry(self, service: str, title: str, body: str) -> str:
        """Create a runbook entry for an exact service from list_services.

        title and body must be nonempty text. Returns the complete stored entry and
        its unique id. An unknown service or invalid text creates nothing.
        """
        args = {"service": service, "title": title, "body": body}
        def create(state):
            if service not in engineering_services(state):
                raise ValueError("Unknown service. Use list_services for an exact name.")
            nonblank(title, "title")
            nonblank(body, "body")
            row = self.store.new_record("runbook", args)
            state.setdefault("runbooks", []).append(row)
            return {"ok": True, "runbook": row}
        return self.store.run("create_runbook_entry", args, create, write=True)

    def update_runbook_entry(self, entry_id: str, body: str | None = None,
                             title: str | None = None, owner: str | None = None,
                             backup: str | None = None) -> str:
        """Change one existing runbook entry selected by exact entry_id from get_runbook.

        Supplied body replaces the complete body and may be empty to clear it. title
        must be nonempty. owner and backup use lowercase person or team keys. Omitted
        fields stay unchanged. An unknown id never creates an entry. Returns the full
        stored runbook after the change.
        """
        args = {"entry_id": entry_id, "body": body, "title": title, "owner": owner, "backup": backup}
        def update(state):
            if all(value is None for value in (body, title, owner, backup)):
                raise ValueError("Supply at least one field to change.")
            target = one_record(state.get("runbooks", []), entry_id)
            if body is not None and not isinstance(body, str):
                raise ValueError("body must be text or null.")
            if title is not None:
                nonblank(title, "title")
            if owner is not None:
                owner_key(owner, "owner")
            if backup is not None:
                owner_key(backup, "backup")
            for key, value in (("body", body), ("title", title), ("owner", owner), ("backup", backup)):
                if value is not None:
                    target[key] = value
            target["updated_at"] = self.store.backend._now_iso()
            return {"ok": True, "runbook": target}
        return self.store.run("update_runbook_entry", args, update, write=True)

    def get_runbook(self, service: str | None = None, entry_id: str | None = None) -> str:
        """Read runbooks by one exact service name or one exact entry id.

        Supply service or entry_id, but not both. Use list_services for service names.
        An unknown entry_id returns ok=false. A known service with no entries returns
        an empty entries list.
        """
        args = {"service": service, "entry_id": entry_id}
        def read(state):
            if (service is None) == (entry_id is None):
                raise ValueError("Supply exactly one of service or entry_id.")
            rows = state.get("runbooks", [])
            if entry_id is not None:
                return {"entries": [one_record(rows, entry_id)]}
            if service not in engineering_services(state):
                raise ValueError("Unknown service. Use list_services for an exact name.")
            return {"entries": [row for row in rows if row.get("service") == service]}
        return self.store.run("get_runbook", args, read)

    def list_deploys(self, service: str | None = None, env: DEPLOY_ENV | None = None,
                     status: DEPLOY_STATUS | None = None) -> str:
        """List stored deployments and exact deploy_id values.

        Optional service, env, and status filters use exact values. env may be dev,
        staging, or prod. status may be queued, in_progress, completed, failed, or
        rolled_back. Omit all filters to list every deployment.
        """
        args = {"service": service, "env": env, "status": status}
        def read(state):
            if service is not None:
                nonblank(service, "service")
            if env is not None and env not in ("dev", "staging", "prod"):
                raise ValueError("env must be dev, staging, prod, or null.")
            if status is not None and status not in ("queued", "in_progress", "completed", "failed", "rolled_back"):
                raise ValueError("status must be queued, in_progress, completed, failed, rolled_back, or null.")
            rows = deploy_records(state)
            return [row for row in rows if (service is None or row.get("service") == service)
                    and (env is None or row.get("env") == env)
                    and (status is None or row.get("status") == status)]
        return self.store.run("list_deploys", args, read)

    def deploy_service(self, service: str, version: str, env: DEPLOY_ENV,
                       strategy: DEPLOY_STRATEGY = "direct") -> str:
        """Deploy one version of an exact service from list_services.

        version must be nonempty. env must be dev, staging, or prod. strategy must
        be direct or canary; one canary call completes the whole rollout. Returns
        the complete stored deployment, including deploy_id and status=completed.
        """
        args = {"service": service, "version": version, "env": env, "strategy": strategy}
        def deploy(state):
            if service not in engineering_services(state):
                raise ValueError("Unknown service. Use list_services for an exact name.")
            nonblank(version, "version")
            if env not in ("dev", "staging", "prod"):
                raise ValueError("env must be dev, staging, or prod.")
            if strategy not in ("direct", "canary"):
                raise ValueError("strategy must be direct or canary.")
            created = self.store.new_record("deploy", {**args, "status": "completed",
                                                        "deployed_at": self.store.backend._now_iso()})
            created["deploy_id"] = created.pop("id")
            state.setdefault("deploys", []).append(created)
            return {"ok": True, "deployment": created}
        return self.store.run("deploy_service", args, deploy, write=True)

    def rollback_deploy(self, deploy_id: str) -> str:
        """Roll back one completed deployment selected by deploy_id from list_deploys.

        The service comes from the stored deployment and cannot be supplied separately.
        Unknown, failed, or already rolled-back deployments are rejected. Returns the
        stored rollback and the deployment now marked rolled_back.
        """
        def rollback(state):
            deployment = next((row for row in deploy_records(state)
                               if (row.get("deploy_id") or row.get("id")) == deploy_id), None)
            if deployment is None:
                raise ValueError("deploy_id must identify one existing deployment. Use list_deploys.")
            if deployment.get("status") != "completed":
                raise ValueError("Only a completed deployment can be rolled back.")
            rolled_back_at = self.store.backend._now_iso()
            for collection in ("deploys", "deployments"):
                for row in state.get(collection, []):
                    if (row.get("deploy_id") or row.get("id")) == deploy_id:
                        row.update(status="rolled_back", rolled_back_at=rolled_back_at)
                        deployment = row
            row = self.store.new_record("rollback", {"deploy_id": deploy_id,
                                                      "service": deployment["service"],
                                                      "rolled_back_at": rolled_back_at})
            state.setdefault("rollbacks", []).append(row)
            return {"ok": True, "rollback": row, "deployment": deployment}
        return self.store.run("rollback_deploy", {"deploy_id": deploy_id}, rollback, write=True)

    def get_service_metrics(self, service: str, metric: str, timerange: str) -> str:
        """Read one stored metric for an exact service.

        service comes from list_services. metric comes from list_service_metrics for
        that service and is case-sensitive. timerange is a positive number followed
        by m, h, or d. An unknown metric is rejected; this tool invents no readings.
        """
        args = {"service": service, "metric": metric, "timerange": timerange}
        def read(state):
            if service not in engineering_services(state):
                raise ValueError("Unknown service. Use list_services for an exact name.")
            metric_name = nonblank(metric, "metric")
            time_window(timerange)
            metrics = state.get("service_metrics", {}).get(service, {})
            if not isinstance(metrics, dict):
                raise ValueError("Stored metrics for the service must be an object keyed by metric name.")
            if metric_name not in metrics:
                raise ValueError("Unknown metric for this service. Use list_service_metrics for an exact name.")
            return {"ok": True, **args, "data": metrics[metric_name]}
        return self.store.run("get_service_metrics", args, read)

    def query_logs(self, service: str, query: str, timerange: str, limit: int = 20) -> str:
        """Search stored logs for one exact service from list_services.

        query is a nonempty case-insensitive text match over each log record. timerange
        is a positive number followed by m, h, or d. limit is an integer from 1 to 100.
        Returns only stored matching records; no log lines are invented.
        """
        args = {"service": service, "query": query, "timerange": timerange, "limit": limit}
        def read(state):
            if service not in engineering_services(state):
                raise ValueError("Unknown service. Use list_services for an exact name.")
            needle = nonblank(query, "query").strip().casefold()
            window = time_window(timerange)
            integer(limit, "limit")
            if limit > 100:
                raise ValueError("limit must be at most 100.")
            now = datetime.fromisoformat(self.store.backend._now_iso().replace("Z", "+00:00"))
            if now.utcoffset() is None:
                raise ValueError("The current time must include a timezone.")
            lower = now - window
            result = []
            for row in state.get("logs", []):
                if row.get("service") != service or needle not in json.dumps(row, sort_keys=True).casefold():
                    continue
                stamp = row.get("ts") or row.get("timestamp")
                try:
                    moment = datetime.fromisoformat(nonblank(stamp, "Stored log timestamp").replace("Z", "+00:00"))
                    if moment.utcoffset() is None:
                        raise ValueError("Stored log timestamps must include a timezone.")
                except ValueError as exc:
                    raise ValueError("Stored log timestamps must be ISO date-times with a timezone.") from exc
                if lower <= moment <= now:
                    result.append(row)
            result.sort(key=lambda row: row.get("ts") or row.get("timestamp"), reverse=True)
            return {"ok": True, **args, "count": min(len(result), limit), "lines": result[:limit]}
        return self.store.run("query_logs", args, read)
