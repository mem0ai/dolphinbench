"""Strict candidate tools for Riley's growth, billing, and experiment records."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Literal

from mock_mcp.repair_support import CandidateStore, date_value, integer, nonblank


FLAG_STATUS = Literal["draft", "running", "shipped", "rolled_back", "decommissioned"]
SUBSCRIPTION_STATUS = Literal["active", "paused", "canceled", "past_due"]
INVOICE_STATUS = Literal["open", "paid", "void", "uncollectible"]
TIER = Literal["starter", "mid_seg", "growth", "enterprise"]
EXPERIMENT_STATUS = Literal[
    "designed", "running", "significant", "shipped", "rolled_back", "inconclusive"
]
BRIEF_STATUS = Literal["open", "in_progress", "shipped", "closed"]


def records(state: dict, collection: str) -> list[dict]:
    rows = state.get(collection, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"Stored {collection} must be a list of records.")
    return rows


def exact_record(rows: list[dict], field: str, value: str, list_tool: str) -> dict:
    nonblank(value, field)
    matches = [row for row in rows if row.get(field) == value]
    if len(matches) != 1:
        raise ValueError(f"{field} must identify exactly one stored record. Use {list_tool}.")
    return matches[0]


def optional_text(value: str | None, field: str, *, allow_empty: bool = False) -> None:
    if value is None:
        return
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "text" if allow_empty else "nonempty text"
        raise ValueError(f"{field} must be {qualifier}.")


def bounded_integer(value: int, field: str, maximum: int) -> int:
    integer(value, field)
    if value > maximum:
        raise ValueError(f"{field} must be no greater than {maximum}.")
    return value


def stored_date(row: dict, field: str) -> str:
    raw = nonblank(row.get(field), f"Stored {field}")
    value = raw[:10]
    date_value(value)
    return value


class CandidateGrowth:
    def __init__(self, store: CandidateStore):
        self.store = store

    def list_feature_flags(self, status: FLAG_STATUS | None = None,
                           owner: str | None = None) -> str:
        """List feature flags and their exact keys.

        status may be draft, running, shipped, rolled_back, or decommissioned.
        owner must exactly match a stored owner key. Omit a filter to search
        across every value for that field.
        """
        args = {"status": status, "owner": owner}

        def read(state):
            if status is not None and status not in ("draft", "running", "shipped", "rolled_back", "decommissioned"):
                raise ValueError("status must be draft, running, shipped, rolled_back, decommissioned, or null.")
            optional_text(owner, "owner")
            rows = [row for row in records(state, "feature_flags")
                    if (status is None or row.get("status") == status)
                    and (owner is None or row.get("owner") == owner)]
            return {"ok": True, "count": len(rows), "flags": rows}

        return self.store.run("list_feature_flags", args, read)

    def get_feature_flag(self, key: str) -> str:
        """Get one feature flag by its exact key from list_feature_flags.

        IDs, display names, and partial matches are not accepted. Returns the
        complete stored flag so its current status and rollout are checkable.
        """
        def read(state):
            flag = exact_record(records(state, "feature_flags"), "key", key, "list_feature_flags")
            return {"ok": True, "flag": flag}

        return self.store.run("get_feature_flag", {"key": key}, read)

    def update_feature_flag(self, key: str, rollout_pct: int | None = None,
                            status: FLAG_STATUS | None = None, name: str | None = None,
                            notes: str | None = None) -> str:
        """Change one existing feature flag selected by exact key.

        rollout_pct must be an integer from 0 through 100. status must be draft,
        running, shipped, rolled_back, or decommissioned. Omitted fields remain unchanged. This
        update never creates a flag and returns the complete updated flag.
        """
        args = {"key": key, "rollout_pct": rollout_pct, "status": status,
                "name": name, "notes": notes}

        def update(state):
            if all(value is None for value in (rollout_pct, status, name, notes)):
                raise ValueError("Supply at least one field to change.")
            target = exact_record(records(state, "feature_flags"), "key", key, "list_feature_flags")
            if rollout_pct is not None and (type(rollout_pct) is not int or not 0 <= rollout_pct <= 100):
                raise ValueError("rollout_pct must be an integer from 0 through 100.")
            if status is not None and status not in ("draft", "running", "shipped", "rolled_back", "decommissioned"):
                raise ValueError("status must be draft, running, shipped, rolled_back, or decommissioned.")
            optional_text(name, "name")
            optional_text(notes, "notes", allow_empty=True)
            for field, value in (("rollout_pct", rollout_pct), ("status", status),
                                 ("name", name), ("notes", notes)):
                if value is not None:
                    target[field] = value
            target["updated_at"] = self.store.backend._now_iso()
            return {"ok": True, "flag": target}

        return self.store.run("update_feature_flag", args, update, write=True)

    def list_event_funnels(self) -> str:
        """List stored funnel names, cohorts, and window sizes accepted by query_event_funnel."""
        def read(state):
            available = [{key: row.get(key) for key in ("name", "cohort", "window_days")}
                         for row in records(state, "funnels")]
            return {"ok": True, "funnels": available}

        return self.store.run("list_event_funnels", {}, read)

    def query_event_funnel(self, funnel_name: str, cohort: str | None = None,
                           window_days: int | None = None) -> str:
        """Read one stored funnel result using its exact name and optional exact selectors.

        Use list_event_funnels to see available names, cohorts, and windows. If
        omitted selectors leave more than one result, the query is rejected rather
        than choosing one silently.
        """
        args = {"funnel_name": funnel_name, "cohort": cohort, "window_days": window_days}

        def read(state):
            nonblank(funnel_name, "funnel_name")
            optional_text(cohort, "cohort")
            if window_days is not None:
                bounded_integer(window_days, "window_days", 365)
            matches = [row for row in records(state, "funnels")
                       if row.get("name") == funnel_name
                       and (cohort is None or row.get("cohort") == cohort)
                       and (window_days is None or row.get("window_days") == window_days)]
            if len(matches) != 1:
                raise ValueError("The selectors must identify exactly one stored funnel. Use list_event_funnels.")
            return {"ok": True, "funnel": matches[0]}

        return self.store.run("query_event_funnel", args, read)

    def list_retention_cohorts(self) -> str:
        """List exact cohort IDs and snapshot dates accepted by query_retention_cohort."""
        def read(state):
            available = [{"cohort_id": row.get("cohort_id"), "asof_date": row.get("asof_date")}
                         for row in records(state, "retention_cohorts")]
            return {"ok": True, "cohorts": available}

        return self.store.run("list_retention_cohorts", {}, read)

    def query_retention_cohort(self, cohort_id: str, weeks: int | None = None,
                               asof_date: str | None = None) -> str:
        """Read a stored retention snapshot by exact cohort ID.

        asof_date must be YYYY-MM-DD; omit it for the latest snapshot. weeks
        limits the returned curve and cannot exceed the stored curve length.
        """
        args = {"cohort_id": cohort_id, "weeks": weeks, "asof_date": asof_date}

        def read(state):
            nonblank(cohort_id, "cohort_id")
            if asof_date is not None:
                date_value(asof_date)
            if weeks is not None:
                bounded_integer(weeks, "weeks", 260)
            matches = [row for row in records(state, "retention_cohorts")
                       if row.get("cohort_id") == cohort_id
                       and (asof_date is None or row.get("asof_date") == asof_date)]
            if asof_date is None and matches:
                latest = max(row.get("asof_date", "") for row in matches)
                matches = [row for row in matches if row.get("asof_date") == latest]
            if len(matches) != 1:
                raise ValueError("The selectors must identify exactly one retention snapshot. Use list_retention_cohorts.")
            result = copy.deepcopy(matches[0])
            curve = result.get("retention_curve", [])
            if not isinstance(curve, list):
                raise ValueError("Stored retention_curve must be a list.")
            if weeks is not None:
                if weeks > len(curve):
                    raise ValueError("weeks exceeds the stored retention curve length.")
                result["retention_curve"] = curve[:weeks]
                result["weeks"] = weeks
            return {"ok": True, "cohort": result}

        return self.store.run("query_retention_cohort", args, read)

    def list_user_paths(self) -> str:
        """List exact start, end, and cohort selectors accepted by query_user_path."""
        def read(state):
            available = [{key: row.get(key) for key in ("start_event", "end_event", "cohort")}
                         for row in records(state, "user_paths")]
            return {"ok": True, "paths": available}

        return self.store.run("list_user_paths", {}, read)

    def query_user_path(self, start_event: str, end_event: str | None = None,
                        cohort: str | None = None, max_steps: int | None = None) -> str:
        """Read one stored path analysis.

        start_event must exactly match a stored start event. end_event and cohort,
        when supplied, must also match exactly. Omit either one to search across
        every stored value for that field. The remaining arguments must identify
        one result; otherwise use list_user_paths to choose exact values. max_steps
        limits each returned path to 1 through 50 events.
        """
        args = {"start_event": start_event, "end_event": end_event,
                "cohort": cohort, "max_steps": max_steps}

        def read(state):
            nonblank(start_event, "start_event")
            optional_text(end_event, "end_event")
            optional_text(cohort, "cohort")
            if max_steps is not None:
                bounded_integer(max_steps, "max_steps", 50)
            matches = [row for row in records(state, "user_paths")
                       if row.get("start_event") == start_event
                       and (end_event is None or row.get("end_event") == end_event)
                       and (cohort is None or row.get("cohort") == cohort)]
            if len(matches) != 1:
                raise ValueError("The selectors must identify exactly one stored path result. Use list_user_paths.")
            result = copy.deepcopy(matches[0])
            if max_steps is not None:
                top_paths = result.get("top_paths", [])
                if not isinstance(top_paths, list):
                    raise ValueError("Stored top_paths must be a list.")
                for row in top_paths:
                    if not isinstance(row, dict) or not isinstance(row.get("path"), list):
                        raise ValueError("Each stored path must be a list of event names.")
                    row["path"] = row["path"][:max_steps]
                result["max_steps"] = max_steps
            return {"ok": True, "path_result": result}

        return self.store.run("query_user_path", args, read)

    def list_subscriptions(self, tier: TIER | None = None,
                           status: SUBSCRIPTION_STATUS | None = "active",
                           limit: int = 50) -> str:
        """List subscriptions and their exact subscription_id values.

        tier may be starter, mid_seg, growth, enterprise, or null. status may
        be active, paused, canceled, past_due, or null. limit must be 1-200.
        """
        args = {"tier": tier, "status": status, "limit": limit}

        def read(state):
            if tier is not None and tier not in ("starter", "mid_seg", "growth", "enterprise"):
                raise ValueError("tier must be starter, mid_seg, growth, enterprise, or null.")
            if status is not None and status not in ("active", "paused", "canceled", "past_due"):
                raise ValueError("status must be active, paused, canceled, past_due, or null.")
            bounded_integer(limit, "limit", 200)
            rows = [row for row in records(state, "subscriptions")
                    if (tier is None or row.get("tier") == tier)
                    and (status is None or row.get("status") == status)][:limit]
            return {"ok": True, "count": len(rows), "subscriptions": rows}

        return self.store.run("list_subscriptions", args, read)

    def get_subscription(self, subscription_id: str) -> str:
        """Get one subscription by exact subscription_id from list_subscriptions."""
        def read(state):
            row = exact_record(records(state, "subscriptions"), "subscription_id",
                               subscription_id, "list_subscriptions")
            return {"ok": True, "subscription": row}

        return self.store.run("get_subscription", {"subscription_id": subscription_id}, read)

    def list_stripe_invoices(self, customer_id: str | None = None,
                             status: INVOICE_STATUS | None = None,
                             limit: int = 50) -> str:
        """List customer invoices and their exact invoice IDs.

        customer_id is an exact stored customer ID. status may be open, paid,
        void, uncollectible, or null. limit must be 1-200.
        """
        args = {"customer_id": customer_id, "status": status, "limit": limit}

        def read(state):
            optional_text(customer_id, "customer_id")
            if status is not None and status not in ("open", "paid", "void", "uncollectible"):
                raise ValueError("status must be open, paid, void, uncollectible, or null.")
            bounded_integer(limit, "limit", 200)
            rows = [row for row in records(state, "stripe_invoices")
                    if (customer_id is None or row.get("customer_id") == customer_id)
                    and (status is None or row.get("status") == status)][:limit]
            return {"ok": True, "count": len(rows), "invoices": rows}

        return self.store.run("list_stripe_invoices", args, read)

    def list_customers(self) -> str:
        """List stored customers and their exact customer_id values."""
        def read(state):
            rows = records(state, "customers")
            return {"ok": True, "count": len(rows), "customers": rows}

        return self.store.run("list_customers", {}, read)

    def get_customer(self, customer_id: str) -> str:
        """Get one customer by exact customer_id from list_customers.

        Names, internal IDs, and partial matches are rejected.
        """
        def read(state):
            row = exact_record(records(state, "customers"), "customer_id",
                               customer_id, "list_customers")
            return {"ok": True, "customer": row}

        return self.store.run("get_customer", {"customer_id": customer_id}, read)

    def list_recent_churn(self, tier: TIER | None = None, days: int = 30,
                          asof_date: str | None = None, limit: int = 100) -> str:
        """List churn events inside an exact date window ending on asof_date.

        asof_date must be YYYY-MM-DD; omit it to use today's date. days must be
        from 1 through 3660. limit must be from 1 through 200. Events outside
        the window are excluded.
        """
        args = {"tier": tier, "days": days, "asof_date": asof_date, "limit": limit}

        def read(state):
            if tier is not None and tier not in ("starter", "mid_seg", "growth", "enterprise"):
                raise ValueError("tier must be starter, mid_seg, growth, enterprise, or null.")
            bounded_integer(days, "days", 3660)
            bounded_integer(limit, "limit", 200)
            anchor = date_value(asof_date) if asof_date is not None else date_value(
                self.store.backend._now_iso()[:10]
            )
            first = anchor - timedelta(days=days)
            rows = []
            for row in records(state, "churn_events"):
                churned = date_value(stored_date(row, "churned_at"))
                if first < churned <= anchor and (tier is None or row.get("tier") == tier):
                    rows.append(row)
            rows.sort(key=lambda row: stored_date(row, "churned_at"), reverse=True)
            rows = rows[:limit]
            return {"ok": True, "tier": tier, "asof_date": anchor.isoformat(),
                    "window_days": days, "count": len(rows), "events": rows}

        return self.store.run("list_recent_churn", args, read)

    def get_mrr_metrics(self, asof_date: str | None = None) -> str:
        """Get one MRR snapshot by YYYY-MM-DD, or the latest saved snapshot."""
        def read(state):
            if asof_date is not None:
                date_value(asof_date)
            rows = records(state, "mrr_snapshots")
            matches = [row for row in rows if asof_date is None or row.get("asof_date") == asof_date]
            if asof_date is None and matches:
                latest = max(row.get("asof_date", "") for row in matches)
                matches = [row for row in matches if row.get("asof_date") == latest]
            if len(matches) != 1:
                raise ValueError("No unique MRR snapshot matches that date.")
            return {"ok": True, "snapshot": matches[0]}

        return self.store.run("get_mrr_metrics", {"asof_date": asof_date}, read)

    def get_churn_metrics(self, tier: TIER | None = None,
                          asof_date: str | None = None) -> str:
        """Get one monthly churn snapshot for an exact tier and date.

        Omit tier for the all-tier total. asof_date must be YYYY-MM-DD; omit
        it for the latest saved snapshot for that tier.
        """
        args = {"tier": tier, "asof_date": asof_date}

        def read(state):
            if tier is not None and tier not in ("starter", "mid_seg", "growth", "enterprise"):
                raise ValueError("tier must be starter, mid_seg, growth, enterprise, or null.")
            if asof_date is not None:
                date_value(asof_date)
            saved = state.get("churn_metrics", {})
            if not isinstance(saved, dict):
                raise ValueError("Stored churn_metrics must be keyed by tier and date.")
            tier_key = tier or "all"
            matches = [row for key, row in saved.items()
                       if isinstance(row, dict) and key.startswith(f"{tier_key}|")
                       and (asof_date is None or row.get("asof_date") == asof_date)]
            if asof_date is None and matches:
                latest = max(row.get("asof_date", "") for row in matches)
                matches = [row for row in matches if row.get("asof_date") == latest]
            if len(matches) != 1:
                raise ValueError("No unique churn snapshot matches that tier and date.")
            return {"ok": True, "snapshot": matches[0]}

        return self.store.run("get_churn_metrics", args, read)

    def get_arr_segments(self, asof_date: str | None = None) -> str:
        """Get one ARR-segment snapshot by YYYY-MM-DD, or the latest saved snapshot."""
        def read(state):
            if asof_date is not None:
                date_value(asof_date)
            rows = records(state, "arr_segments")
            matches = [row for row in rows if asof_date is None or row.get("asof_date") == asof_date]
            if asof_date is None and matches:
                latest = max(row.get("asof_date", "") for row in matches)
                matches = [row for row in matches if row.get("asof_date") == latest]
            if len(matches) != 1:
                raise ValueError("No unique ARR-segment snapshot matches that date.")
            return {"ok": True, "snapshot": matches[0]}

        return self.store.run("get_arr_segments", {"asof_date": asof_date}, read)

    def list_experiments(self, status: EXPERIMENT_STATUS | None = None) -> str:
        """List experiments and their exact experiment_id values, optionally by status."""
        def read(state):
            allowed = ("designed", "running", "significant", "shipped", "rolled_back", "inconclusive")
            if status is not None and status not in allowed:
                raise ValueError("status must be designed, running, significant, shipped, rolled_back, inconclusive, or null.")
            rows = [row for row in records(state, "experiments")
                    if status is None or row.get("status") == status]
            return {"ok": True, "count": len(rows), "experiments": rows}

        return self.store.run("list_experiments", {"status": status}, read)

    def create_experiment(self, experiment_id: str, name: str, hypothesis: str,
                          metric_of_record: str, cohort_scope: str,
                          feature_flag_key: str | None = None,
                          designed_at: str | None = None) -> str:
        """Create one experiment with a new exact experiment_id.

        Text fields must be nonempty. feature_flag_key, when supplied, must match
        a key from list_feature_flags. designed_at is YYYY-MM-DD. An existing ID
        is rejected rather than overwritten. Returns the complete experiment.
        """
        args = {"experiment_id": experiment_id, "name": name, "hypothesis": hypothesis,
                "metric_of_record": metric_of_record, "cohort_scope": cohort_scope,
                "feature_flag_key": feature_flag_key, "designed_at": designed_at}

        def create(state):
            for field, value in (("experiment_id", experiment_id), ("name", name),
                                 ("hypothesis", hypothesis), ("metric_of_record", metric_of_record),
                                 ("cohort_scope", cohort_scope)):
                nonblank(value, field)
            experiments = records(state, "experiments")
            if any(row.get("experiment_id") == experiment_id for row in experiments):
                raise ValueError("experiment_id already exists. Use update_experiment.")
            if feature_flag_key is not None:
                exact_record(records(state, "feature_flags"), "key", feature_flag_key,
                             "list_feature_flags")
            date = designed_at or self.store.backend._now_iso()[:10]
            date_value(date)
            row = {**args, "designed_at": date, "status": "designed",
                   "created_at": self.store.backend._now_iso()}
            experiments.append(row)
            return {"ok": True, "experiment": row}

        return self.store.run("create_experiment", args, create, write=True)

    def update_experiment(self, experiment_id: str,
                          status: EXPERIMENT_STATUS | None = None,
                          result: str | None = None, landed_at: str | None = None,
                          learning_carryforward: str | None = None) -> str:
        """Change one existing experiment selected by exact experiment_id.

        status uses the documented experiment statuses. landed_at is YYYY-MM-DD.
        Omitted fields remain unchanged. Returns the complete updated experiment.
        """
        args = {"experiment_id": experiment_id, "status": status, "result": result,
                "landed_at": landed_at, "learning_carryforward": learning_carryforward}

        def update(state):
            if all(value is None for value in (status, result, landed_at, learning_carryforward)):
                raise ValueError("Supply at least one field to change.")
            target = exact_record(records(state, "experiments"), "experiment_id",
                                  experiment_id, "list_experiments")
            allowed = ("designed", "running", "significant", "shipped", "rolled_back", "inconclusive")
            if status is not None and status not in allowed:
                raise ValueError("status must be designed, running, significant, shipped, rolled_back, or inconclusive.")
            optional_text(result, "result")
            optional_text(learning_carryforward, "learning_carryforward")
            if landed_at is not None:
                date_value(landed_at)
            for field, value in (("status", status), ("result", result),
                                 ("landed_at", landed_at),
                                 ("learning_carryforward", learning_carryforward)):
                if value is not None:
                    target[field] = value
            target["updated_at"] = self.store.backend._now_iso()
            return {"ok": True, "experiment": target}

        return self.store.run("update_experiment", args, update, write=True)

    def query_experiment_results(self, experiment_id: str) -> str:
        """Get one saved result by exact experiment_id from list_experiments."""
        def read(state):
            row = exact_record(records(state, "experiment_results"), "experiment_id",
                               experiment_id, "list_experiments")
            return {"ok": True, "result": row}

        return self.store.run("query_experiment_results", {"experiment_id": experiment_id}, read)

    def list_growth_briefs(self, status: BRIEF_STATUS | None = None) -> str:
        """List growth briefs and their exact brief_id values, optionally by status."""
        def read(state):
            if status is not None and status not in ("open", "in_progress", "shipped", "closed"):
                raise ValueError("status must be open, in_progress, shipped, closed, or null.")
            rows = [row for row in records(state, "growth_briefs")
                    if status is None or row.get("status", "open") == status]
            return {"ok": True, "count": len(rows), "briefs": rows}

        return self.store.run("list_growth_briefs", {"status": status}, read)

    def create_growth_brief(self, brief_id: str, title: str, hypothesis: str,
                            metric_of_record: str, cohort_scope: str | None = None,
                            timeline: str | None = None, body: str | None = None) -> str:
        """Create one growth brief with a new exact brief_id.

        The required text fields must be nonempty. Optional cohort and timeline,
        when supplied, must also be nonempty. An existing ID is rejected instead
        of overwritten. Returns the complete stored brief and its URL.
        """
        args = {"brief_id": brief_id, "title": title, "hypothesis": hypothesis,
                "metric_of_record": metric_of_record, "cohort_scope": cohort_scope,
                "timeline": timeline, "body": body}

        def create(state):
            for field, value in (("brief_id", brief_id), ("title", title),
                                 ("hypothesis", hypothesis),
                                 ("metric_of_record", metric_of_record)):
                nonblank(value, field)
            optional_text(cohort_scope, "cohort_scope")
            optional_text(timeline, "timeline")
            optional_text(body, "body", allow_empty=True)
            briefs = records(state, "growth_briefs")
            if any(row.get("brief_id") == brief_id for row in briefs):
                raise ValueError("brief_id already exists. Use update_growth_brief.")
            row = {**args, "body": body or "", "status": "open", "owner": "riley",
                   "created_at": self.store.backend._now_iso()}
            briefs.append(row)
            return {"ok": True, "brief": row,
                    "url": f"https://notion.mock/growth-briefs/{brief_id}"}

        return self.store.run("create_growth_brief", args, create, write=True)

    def update_growth_brief(self, brief_id: str, hypothesis: str | None = None,
                            metric_of_record: str | None = None,
                            cohort_scope: str | None = None,
                            timeline: str | None = None, body: str | None = None,
                            status: BRIEF_STATUS | None = None) -> str:
        """Change one existing growth brief selected by exact brief_id.

        status may be open, in_progress, shipped, or closed. Omitted fields stay
        unchanged; body may be empty to clear it. Returns the complete updated brief.
        """
        args = {"brief_id": brief_id, "hypothesis": hypothesis,
                "metric_of_record": metric_of_record, "cohort_scope": cohort_scope,
                "timeline": timeline, "body": body, "status": status}

        def update(state):
            values = (hypothesis, metric_of_record, cohort_scope, timeline, body, status)
            if all(value is None for value in values):
                raise ValueError("Supply at least one field to change.")
            target = exact_record(records(state, "growth_briefs"), "brief_id",
                                  brief_id, "list_growth_briefs")
            for field, value in (("hypothesis", hypothesis),
                                 ("metric_of_record", metric_of_record),
                                 ("cohort_scope", cohort_scope), ("timeline", timeline)):
                optional_text(value, field)
            optional_text(body, "body", allow_empty=True)
            if status is not None and status not in ("open", "in_progress", "shipped", "closed"):
                raise ValueError("status must be open, in_progress, shipped, or closed.")
            for field, value in (("hypothesis", hypothesis),
                                 ("metric_of_record", metric_of_record),
                                 ("cohort_scope", cohort_scope), ("timeline", timeline),
                                 ("body", body), ("status", status)):
                if value is not None:
                    target[field] = value
            target["updated_at"] = self.store.backend._now_iso()
            return {"ok": True, "brief": target}

        return self.store.run("update_growth_brief", args, update, write=True)
