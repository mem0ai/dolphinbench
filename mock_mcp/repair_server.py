"""Evaluated test tools, using an isolated app directory for each test."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.environment import get_setting
from mock_mcp.repair_calendar import CandidateCalendar
from mock_mcp.repair_engineering import CandidateEngineering
from mock_mcp.repair_growth import CandidateGrowth
from mock_mcp.repair_operations import CandidateOperations
from mock_mcp.repair_records import CandidateRecords
from mock_mcp.repair_support import CandidateStore, email_address, nonblank, phone_number
from mock_mcp.repair_travel import CandidateTravel


def validate_directory(data: dict[str, Any]) -> None:
    if (not isinstance(data, dict) or not {"users", "channels"} <= set(data)
            or set(data) - {"users", "channels", "discord_channels", "contacts"}):
        raise ValueError("workspace directory needs users and Slack channels; only discord_channels and contacts are optional")
    # Existing candidate directories used 'channels' for Slack. Discord has its
    # own list; an old Slack channel must never silently become a Discord channel.
    for group, fields in (("users", {"key", "name"}), ("channels", {"key"}), ("discord_channels", {"key"})):
        rows = data.get(group, [])
        if not isinstance(rows, list):
            raise ValueError(f"workspace {group} must be a list")
        keys = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError(f"invalid workspace {group} entry")
            if any(not isinstance(value, str) or not value.strip() for value in row.values()):
                raise ValueError("directory values must be nonempty strings")
            key = row["key"]
            if key != key.strip().casefold() or any(c.isspace() for c in key):
                raise ValueError("directory keys must be lowercase with no whitespace")
            if group != "users" and (not key.startswith("#") or len(key) == 1):
                raise ValueError("channel keys must include #")
            keys.append(key)
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate workspace {group} key")
    contacts = data.get("contacts", [])
    if not isinstance(contacts, list):
        raise ValueError("contacts must be a list")
    for row in contacts:
        if (not isinstance(row, dict) or "name" not in row or not {"email", "phone"}.intersection(row)
                or set(row) - {"name", "email", "phone"}):
            raise ValueError("contacts need a name and an email address or phone number, without roles or responsibilities")
        nonblank(row["name"], "Contact name")
        if "email" in row:
            email_address(row["email"])
        if "phone" in row:
            phone_number(row["phone"])


class CandidateSlack:
    def __init__(self, backend: Any, directory: dict[str, Any], store: CandidateStore | None = None):
        validate_directory(directory)
        self.backend = backend
        self.directory = copy.deepcopy(directory)
        self.store = store or CandidateStore(backend)

    def _lookup(self, tool: str, result: dict) -> dict:
        self.backend._log_call(tool, {}, copy.deepcopy(result))
        return copy.deepcopy(result)

    def list_workspace_users(self) -> dict:
        """List names and recipient keys for Slack and Discord DMs. No roles or responsibilities are included."""
        return self._lookup("list_workspace_users", {"users": self.directory["users"]})

    def list_slack_channels(self) -> dict:
        """List the exact channel keys available for posting in this workspace."""
        return self._lookup("list_slack_channels", {"channels": self.directory["channels"]})

    def list_discord_channels(self) -> dict:
        """List the exact channel keys available for posting in Discord. Slack channels are not included."""
        return self._lookup("list_discord_channels", {"channels": self.directory.get("discord_channels", [])})

    def list_contacts(self) -> dict:
        """List supplied contact names, email addresses, and international phone numbers.

        Missing addresses or numbers are omitted, not guessed. No roles,
        responsibilities, or messaging instructions are included. A supplied
        address can be used directly without calling this lookup first.
        """
        contacts = []
        for source in self.directory.get("contacts", []):
            row = {"name": source["name"]}
            if "email" in source:
                row["email"] = email_address(source["email"])
            if "phone" in source:
                row["phone"] = phone_number(source["phone"])
            contacts.append(row)
        return self._lookup("list_contacts", {"contacts": contacts})

    def _send(self, tool: str, field: str, target: str, message: str, group: str) -> str:
        args = {field: target, "message": message}
        try:
            normalized = nonblank(target, field).strip().casefold()
            nonblank(message, "message")
        except ValueError as exc:
            return self.store.error(tool, args, str(exc))
        known = {row["key"] for row in self.directory.get(group, [])}
        if normalized not in known:
            lookup = {"users": "list_workspace_users", "channels": "list_slack_channels",
                      "discord_channels": "list_discord_channels"}[group]
            return self.store.error(tool, args, f"Unknown {field}. Use {lookup} for an exact key.")
        collection = {"send_slack_dm": "slack_dm_log", "send_slack_message": "slack_log",
                      "send_discord_dm": "discord_dm_log", "send_discord_message": "discord_log"}[tool]
        return self.store.deliver(tool, args, collection, {field: normalized, "message": message})

    def send_slack_dm(self, user: str, message: str) -> str:
        """Send a private Slack message to one workspace person.

        Args:
            user: Recipient key from list_workspace_users. Use the lowercase first
                name when unique; otherwise use the directory's distinct key.
                Surrounding whitespace and capitalization are ignored. Any value
                that is not a listed key is rejected.
            message: The nonempty message to deliver.

        Returns:
            A success record with the actual recipient key and delivered message,
            or an error with no message delivered.
        """
        return self._send("send_slack_dm", "user", user, message, "users")

    def send_discord_dm(self, user: str, message: str) -> str:
        """Send a private Discord message to one workspace person.

        Args:
            user: Recipient key from list_workspace_users. Use the lowercase first
                name when unique; otherwise use the directory's distinct key.
                Surrounding whitespace and capitalization are ignored. Any value
                that is not a listed key is rejected.
            message: The nonempty message to deliver.

        Returns:
            A success record with the actual recipient key and delivered message,
            or an error with no message delivered.
        """
        return self._send("send_discord_dm", "user", user, message, "users")

    def send_slack_message(self, channel: str, message: str) -> str:
        """Post a message to a workspace Slack channel.

        Args:
            channel: Exact key from list_slack_channels, including the leading #.
                Surrounding whitespace and capitalization are ignored. Unknown
                channels are rejected; this tool never creates channels.
            message: The nonempty message to post.

        Returns:
            A success record with the actual channel key and posted message,
            or an error with no message posted.
        """
        return self._send("send_slack_message", "channel", channel, message, "channels")

    def send_discord_message(self, channel: str, message: str) -> str:
        """Post a message to an existing Discord channel.

        Args:
            channel: Exact key from list_discord_channels, including #.
                Capitalization and surrounding whitespace are ignored.
                Unknown channels are rejected, not created. A Slack channel
                is not a Discord destination unless it is also listed here.
            message: Nonempty text to post.

        Returns:
            ok=true, a unique id, the actual channel, and the posted message.
            Invalid input returns ok=false and an error without posting.
        """
        return self._send("send_discord_message", "channel", channel, message, "discord_channels")

    def send_email(self, to: str, subject: str, body: str, cc: list[str] | None = None) -> str:
        """Send a new email from the user's account. This does not reply in an existing thread.

        Args:
            to: One email address with a mailbox and domain name. Names and
                display-name forms are not accepted. Use list_contacts if needed.
                Surrounding whitespace is removed; domain names are normalized.
            subject: Subject text, which may be empty. No line breaks.
            body: Nonempty email text, delivered exactly as written.
            cc: Optional list of email addresses, validated by the same rule as to.

        Returns:
            ok=true, a unique id, and the actual to, cc, subject, body, and sent_at.
            Any invalid recipient or input returns ok=false and an error;
            no email is delivered to anyone on the list.
        """
        args = {"to": to, "subject": subject, "body": body, "cc": cc}
        try:
            recipient = email_address(to)
            if not isinstance(subject, str) or any(char in subject for char in "\r\n"):
                raise ValueError("subject must be text without line breaks.")
            nonblank(body, "body")
            if cc is not None and not isinstance(cc, list):
                raise ValueError("cc must be a list of email addresses.")
            recipients = [email_address(value) for value in (cc or [])]
        except ValueError as exc:
            return self.store.error("send_email", args, str(exc))
        return self.store.deliver("send_email", args, "sent_emails",
                                  {"to": recipient, "subject": subject, "body": body, "cc": recipients})

    def send_sms(self, to: str, body: str) -> str:
        """Send a text message to an international phone number.

        Args:
            to: + followed by the country code and subscriber number, digits
                only, at most 15 digits. Surrounding whitespace is ignored.
                Names and local numbers are not accepted. Use list_contacts if needed.
            body: Nonempty message text. It is delivered without truncation.

        Returns:
            ok=true, a unique id, the actual phone number, body, and sent_at.
            Invalid input returns ok=false and an error without sending.
        """
        args = {"to": to, "body": body}
        try:
            recipient = phone_number(to)
            nonblank(body, "body")
        except ValueError as exc:
            return self.store.error("send_sms", args, str(exc))
        return self.store.deliver("send_sms", args, "sms_log", {"to": recipient, "body": body})


def build_server(backend: Any, directory: dict[str, Any]) -> FastMCP:
    store = CandidateStore(backend)
    slack = CandidateSlack(backend, directory, store)
    calendar = CandidateCalendar(store)
    engineering = CandidateEngineering(store, directory)
    owners = (slack, calendar, CandidateRecords(store), CandidateTravel(store),
              engineering, CandidateOperations(store), CandidateGrowth(store))

    def implementation(name):
        return next((getattr(owner, name) for owner in owners if hasattr(owner, name)), None)

    server = FastMCP("dolphinbench-apps")
    allowed = set(backend._MANIFEST_TOOLS)
    if backend._ACTIVE_TOOLS is not None:
        allowed &= backend._ACTIVE_TOOLS
    for name in sorted(allowed):
        function = implementation(name) or getattr(backend, name)
        server.add_tool(function, name=name)
    # Directory reads remain available whenever the corresponding action is available.
    registered = set(allowed)
    for action, lookup in (("send_slack_dm", "list_workspace_users"),
                           ("send_discord_dm", "list_workspace_users"),
                           ("send_slack_message", "list_slack_channels"),
                           ("send_discord_message", "list_discord_channels"),
                           ("send_email", "list_contacts"), ("send_sms", "list_contacts"),
                           ("create_calendar_event", "list_contacts"),
                           ("update_calendar_event", "list_contacts"),
                           ("update_calendar_event", "list_calendar_events"),
                           ("archive_email", "list_inbox"), ("delete_email", "list_inbox"),
                           ("process_payment", "list_invoices"), ("flag_for_review", "list_invoices"),
                           ("update_crm_row", "list_crm_rows"), ("add_crm_row", "list_crm_rows"),
                           ("post_pr_comment", "list_open_prs"), ("gh_pr_create", "list_repositories"),
                           ("gh_pr_create", "list_open_prs"), ("update_deck_slide", "list_decks"),
                           ("get_pr", "list_open_prs"), ("review_pr", "list_open_prs"),
                           ("merge_pr", "list_open_prs"), ("update_doc", "list_docs"),
                           ("post_doc_comment", "list_docs"),
                           ("acknowledge_incident", "list_incidents"),
                           ("escalate_incident", "list_incidents"),
                           ("escalate_incident", "list_workspace_users"),
                           ("create_runbook_entry", "list_services"),
                           ("update_runbook_entry", "get_runbook"),
                           ("get_runbook", "list_services"),
                           ("deploy_service", "list_services"),
                           ("rollback_deploy", "list_deploys"),
                           ("get_service_metrics", "list_services"),
                           ("get_service_metrics", "list_service_metrics"),
                           ("query_logs", "list_services"),
                           ("update_headcount_plan", "list_headcount_plans"),
                           ("book_flight", "list_flight_quotes"), ("book_hotel", "list_hotel_quotes"),
                           ("get_stock_data", "list_stock_tickers"),
                           ("metrics_fetch", "list_services"), ("metrics_fetch_batch", "list_services"),
                           ("vector_search", "list_vector_namespaces"), ("vector_search", "embeddings_encode"),
                           ("get_feature_flag", "list_feature_flags"),
                           ("update_feature_flag", "list_feature_flags"),
                           ("create_experiment", "list_feature_flags"),
                           ("query_event_funnel", "list_event_funnels"),
                           ("query_retention_cohort", "list_retention_cohorts"),
                           ("query_user_path", "list_user_paths"),
                           ("get_subscription", "list_subscriptions"),
                           ("get_customer", "list_customers"),
                           ("update_experiment", "list_experiments"),
                           ("query_experiment_results", "list_experiments"),
                           ("update_growth_brief", "list_growth_briefs")):
        if action in allowed and lookup not in registered:
            server.add_tool(implementation(lookup), name=lookup)
            registered.add(lookup)
    return server


def main() -> None:
    directory_path = get_setting("DOLPHINBENCH_WORKSPACE_DIRECTORY")
    if not directory_path:
        raise SystemExit("Test tools require DOLPHINBENCH_WORKSPACE_DIRECTORY")
    directory = json.loads(Path(directory_path).read_text())
    validate_directory(directory)
    from mock_mcp import server as backend

    build_server(backend, directory).run(transport="stdio")


if __name__ == "__main__":
    main()
