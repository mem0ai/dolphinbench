"""Candidate tools that read, create, and change stored business records."""

from __future__ import annotations

import re
from typing import Literal

from mock_mcp.repair_support import (
    CandidateStore, amount_value, date_value, integer, nonblank, one_record, text_list,
)


CRM_STATUS = Literal["active", "warm_later", "passed"]
INVOICE_STATUS = Literal["pending", "paid", "flagged", "held"]
PR_STATUS = Literal["open", "closed", "merged"]
PR_DECISION = Literal["approve", "request_changes", "comment"]
MERGE_METHOD = Literal["squash", "merge", "rebase"]
DOC_UPDATE_MODE = Literal["replace", "append"]


def optional_text(value, field):
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field} must be text.")


def pr_records(state: dict) -> list[dict]:
    records = {}
    for row in state.get("prs", []) + state.get("open_prs", []):
        key = nonblank(row.get("id"), "Stored PR id")
        if key in records and records[key] != row:
            raise ValueError("Two stored PR records disagree for the same id.")
        records[key] = row
    return list(records.values())


def document_records(state: dict) -> list[tuple[dict, str]]:
    records = {}
    for collection in ("docs", "internal_docs"):
        rows = state.get(collection, [])
        if not isinstance(rows, list):
            raise ValueError(f"Stored {collection} must be a list of documents.")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Each stored document must be an object.")
            key = nonblank(row.get("id") or row.get("doc_id"), "Stored document id")
            if key in records and records[key][0] != row:
                raise ValueError("Two stored documents disagree for the same id.")
            records[key] = (row, key)
    return list(records.values())


class CandidateRecords:
    def __init__(self, store: CandidateStore):
        self.store = store

    def list_inbox(self, folder: Literal["inbox", "archive", "trash"] = "inbox", limit: int = 20) -> str:
        """List stored emails in one folder, newest first.

        Args:
            folder: inbox, archive, or trash. Defaults to inbox.
            limit: Positive maximum number of emails to return.

        Returns:
            The stored emails, including ids usable by archive_email and
            delete_email. A valid folder with no messages returns an empty list.
        """
        def read(state):
            if folder not in ("inbox", "archive", "trash"):
                raise ValueError("folder must be inbox, archive, or trash.")
            integer(limit, "limit")
            rows = [row for row in state.get("inbox", []) if row.get("folder", "inbox") == folder]
            return sorted(rows, key=lambda row: row.get("date", ""), reverse=True)[:limit]
        return self.store.run("list_inbox", {"folder": folder, "limit": limit}, read)

    def archive_email(self, email_id: str) -> str:
        """Move an existing email to archive. email_id comes from list_inbox.

        Returns ok=true, the email_id, and the stored archived email. An already
        archived email stays archived. Unknown ids return ok=false without changes.
        """
        def archive(state):
            email = one_record(state.get("inbox", []), email_id)
            email["folder"] = "archive"
            return {"ok": True, "email_id": email_id, "email": email}
        return self.store.run("archive_email", {"email_id": email_id}, archive, write=True)

    def delete_email(self, email_id: str) -> str:
        """Permanently remove the email identified by email_id from list_inbox.

        Returns ok=true, email_id, and deleted=true only after removing that
        record. Unknown or already deleted ids return ok=false without changes.
        """
        def delete(state):
            row = one_record(state.get("inbox", []), email_id)
            state["inbox"].remove(row)
            return {"ok": True, "email_id": email_id, "deleted": True}
        return self.store.run("delete_email", {"email_id": email_id}, delete, write=True)

    def list_invoices(self, status: INVOICE_STATUS | None = "pending") -> str:
        """List stored invoices with their ids, vendors, amounts, and statuses.

        status may be pending, paid, flagged, held, or null for all invoices.
        Defaults to pending. No payment or approval occurs in this lookup.
        """
        def read(state):
            if status is not None and status not in ("pending", "paid", "flagged", "held"):
                raise ValueError("status must be pending, paid, flagged, held, or null.")
            return [row for row in state.get("invoices", []) if status is None or row.get("status") == status]
        return self.store.run("list_invoices", {"status": status}, read)

    def process_payment(self, invoice_id: str) -> str:
        """Pay one pending invoice in full using its stored vendor and USD amount.

        invoice_id is an existing id from list_invoices. The amount and vendor
        cannot be overridden. This tool does not grant approval or release a hold.
        Paid, held, and flagged invoices cannot be paid through this operation.

        Returns ok=true with the recorded payment and invoice marked paid.
        Missing ids, invalid amounts, or repeat payments return ok=false without
        transferring funds in the simulation or changing records.
        """
        def pay(state):
            invoice = one_record(state.get("invoices", []), invoice_id)
            if invoice.get("status") != "pending":
                raise ValueError("Only a pending invoice can be paid. This operation does not approve or release invoices.")
            if any(row.get("invoice_id") == invoice_id for row in state.get("payments", [])):
                raise ValueError("This invoice already has a recorded payment.")
            if invoice.get("currency", "USD") != "USD":
                raise ValueError("This payment tool accepts USD invoices only; it does not convert currencies.")
            amount_value(invoice.get("amount"))
            nonblank(invoice.get("vendor"), "Stored invoice vendor")
            payment = self.store.new_record("pay", {"invoice_id": invoice_id, "vendor": invoice["vendor"],
                                                    "amount": invoice["amount"], "currency": "USD", "status": "completed"})
            state.setdefault("payments", []).append(payment)
            invoice.update(status="paid", payment_id=payment["id"])
            return {"ok": True, "payment": payment, "invoice": invoice}
        return self.store.run("process_payment", {"invoice_id": invoice_id}, pay, write=True)

    def flag_for_review(self, invoice_id: str, reason: str) -> str:
        """Flag an existing unpaid invoice for human review; do not pay it.

        invoice_id comes from list_invoices. reason is nonempty explanatory text.
        Vendor, amount, and any currency come from that invoice. Pending invoices become flagged;
        a held invoice stays held, so flagging cannot release a hold. Repeating
        this operation replaces the review reason without creating duplicate flags.
        Returns ok=true with the stored review flag and invoice, or ok=false.
        """
        def flag(state):
            invoice = one_record(state.get("invoices", []), invoice_id)
            if invoice.get("status") not in ("pending", "flagged", "held"):
                raise ValueError("Only pending, flagged, or held invoices can be sent for review.")
            nonblank(reason, "reason")
            amount_value(invoice.get("amount"))
            nonblank(invoice.get("vendor"), "Stored invoice vendor")
            flags = state.setdefault("review_flags", [])
            matches = [row for row in flags if row.get("invoice_id") == invoice_id]
            if len(matches) > 1:
                raise ValueError("This invoice has conflicting review records.")
            fields = {"invoice_id": invoice_id, "vendor": invoice["vendor"], "amount": invoice["amount"], "reason": reason}
            if "currency" in invoice:
                fields["currency"] = invoice["currency"]
            if matches:
                record = matches[0]
                record.update(fields)
            else:
                record = self.store.new_record("flag", fields)
                flags.append(record)
            if invoice["status"] != "held":
                invoice["status"] = "flagged"
            return {"ok": True, "review": record, "invoice": invoice}
        return self.store.run("flag_for_review", {"invoice_id": invoice_id, "reason": reason}, flag, write=True)

    def list_crm_rows(self, name: str | None = None) -> str:
        """List existing CRM rows and their ids. Optional name filters by full name,
        ignoring case and surrounding whitespace. Duplicate names remain separate
        rows: select the correct id using the returned company and other fields.
        """
        def read(state):
            key = nonblank(name, "name").strip().casefold() if name is not None else None
            return [row for row in state.get("crm", []) if key is None or row.get("name", "").strip().casefold() == key]
        return self.store.run("list_crm_rows", {"name": name}, read)

    def add_crm_row(self, name: str, company: str, notes: str = "", tags: list[str] | None = None) -> str:
        """Create a new CRM row and return its stored fields and unique id.

        name is the full person or organization name; company is its affiliation.
        Both must be nonempty. notes is free text; tags is an optional list of
        nonempty labels. This creates a row; use update_crm_row to change one.
        """
        args = {"name": name, "company": company, "notes": notes, "tags": tags}
        def create(state):
            nonblank(name, "name")
            nonblank(company, "company")
            optional_text(notes, "notes")
            labels = text_list(tags, "tags") if tags is not None else []
            row = self.store.new_record("crm", {"name": name, "company": company, "notes": notes, "tags": labels})
            state.setdefault("crm", []).append(row)
            return {"ok": True, "row": row}
        return self.store.run("add_crm_row", args, create, write=True)

    def update_crm_row(self, row_id: str, status: CRM_STATUS | None = None, next_touch_date: str | None = None,
                       trigger_condition: str | None = None, notes: str | None = None, tags: list[str] | None = None) -> str:
        """Update exactly one existing CRM row, selected by row_id from list_crm_rows.

        Args:
            row_id: Existing row id, not a name. Missing ids never create a row.
            status: active means currently pursuing; warm_later means keeping
                for later consideration; passed means not pursuing. Null keeps the old status.
            next_touch_date: YYYY-MM-DD, or an empty string to clear the date.
                Null leaves it unchanged. Put conditions, not dates, in trigger_condition.
            trigger_condition: Replacement condition text; empty string clears it.
            notes: Complete replacement notes; empty string clears them.
            tags: Complete replacement list of nonempty labels; [] clears them.

        Omitted or null fields stay unchanged. Returns ok=true and the stored row;
        invalid input returns ok=false without changing any fields.
        """
        args = {"row_id": row_id, "status": status, "next_touch_date": next_touch_date,
                "trigger_condition": trigger_condition, "notes": notes, "tags": tags}
        def update(state):
            row = one_record(state.get("crm", []), row_id)
            if status is not None and status not in ("active", "warm_later", "passed"):
                raise ValueError("status must be active, warm_later, or passed.")
            if next_touch_date not in (None, ""):
                date_value(next_touch_date)
            for field, value in (("trigger_condition", trigger_condition), ("notes", notes)):
                optional_text(value, field)
            if tags is not None:
                text_list(tags, "tags")
            for key, value in args.items():
                if key != "row_id" and value is not None:
                    row[key] = None if key == "next_touch_date" and value == "" else value
            row["updated_at"] = self.store.backend._now_iso()
            return {"ok": True, "row": row}
        return self.store.run("update_crm_row", args, update, write=True)

    def list_open_prs(self, repo: str | None = None, author: str | None = None,
                      assignee: str | None = None, status: PR_STATUS | None = "open", reviewer: str | None = None) -> str:
        """List identifying pull-request summaries. Defaults to open PRs.

        status may be open, closed, merged, or null for every status. repo and
        author filter by exact repository name and author key. assignee matches
        the assignees list; reviewer matches the reviewers list. These are separate
        relationships. Omitted filters do not restrict results. The summaries omit
        each pull request's body; use get_pr with an exact id to inspect the body.
        """
        args = {"repo": repo, "author": author, "assignee": assignee, "status": status, "reviewer": reviewer}
        def read(state):
            if status is not None and status not in ("open", "closed", "merged"):
                raise ValueError("status must be open, closed, merged, or null.")
            rows = pr_records(state)
            for field, value in (("repo", repo), ("author", author), ("status", status)):
                if value is not None:
                    nonblank(value, field)
                    rows = [row for row in rows if row.get(field) == value]
            for field, value in (("assignees", assignee), ("reviewers", reviewer)):
                if value is not None:
                    nonblank(value, field)
                    rows = [row for row in rows if value in (row.get(field) or [])]
            fields = ("id", "repo", "title", "author", "assignees", "reviewers", "status", "url")
            return [{field: row[field] for field in fields if field in row} for row in rows]
        return self.store.run("list_open_prs", args, read)

    def get_pr(self, pr_id: str) -> str:
        """Return one pull request selected by its exact id from list_open_prs.

        The result includes the complete stored pull-request record. A title, URL,
        repository name, or unknown id is rejected rather than guessed.
        """
        def read(state):
            return {"pr": one_record(pr_records(state), pr_id)}
        return self.store.run("get_pr", {"pr_id": pr_id}, read)

    def review_pr(self, pr_id: str, decision: PR_DECISION, body: str | None = None) -> str:
        """Submit one review on an open pull request selected by exact pr_id.

        decision must be approve, request_changes, or comment. body may be omitted
        for approve. request_changes and comment require nonempty body text. The PR
        must already exist and be open. Returns the complete stored review record.
        """
        args = {"pr_id": pr_id, "decision": decision, "body": body}
        def review(state):
            pull_request = one_record(pr_records(state), pr_id)
            if pull_request.get("status") != "open":
                raise ValueError("Only an open pull request can be reviewed.")
            if decision not in ("approve", "request_changes", "comment"):
                raise ValueError("decision must be approve, request_changes, or comment.")
            if body is not None and not isinstance(body, str):
                raise ValueError("body must be text or null.")
            if decision in ("request_changes", "comment"):
                nonblank(body, "body")
            row = self.store.new_record("review", {"pr_id": pr_id, "decision": decision,
                                                    "body": body or ""})
            state.setdefault("pr_reviews", []).append(row)
            return {"ok": True, "review": row}
        return self.store.run("review_pr", args, review, write=True)

    def merge_pr(self, pr_id: str, method: MERGE_METHOD = "squash") -> str:
        """Merge one open pull request selected by exact pr_id from list_open_prs.

        method must be squash, merge, or rebase. Missing, closed, or already merged
        pull requests are rejected. Returns the stored merge record and updated PR.
        """
        args = {"pr_id": pr_id, "method": method}
        def merge_pull_request(state):
            pull_request = one_record(pr_records(state), pr_id)
            if pull_request.get("status") != "open":
                raise ValueError("Only an open pull request can be merged.")
            if method not in ("squash", "merge", "rebase"):
                raise ValueError("method must be squash, merge, or rebase.")
            merged_at = self.store.backend._now_iso()
            for collection in ("prs", "open_prs"):
                for row in state.get(collection, []):
                    if row.get("id") == pr_id:
                        row.update(status="merged", merged_at=merged_at)
            pull_request.update(status="merged", merged_at=merged_at)
            row = self.store.new_record("merge", {"pr_id": pr_id, "method": method,
                                                   "merged_at": merged_at})
            state.setdefault("pr_merges", []).append(row)
            return {"ok": True, "merge": row, "pr": pull_request}
        return self.store.run("merge_pr", args, merge_pull_request, write=True)

    def list_repositories(self) -> str:
        """List repository names supplied in workspace data or existing PR records.
        Use a returned name as repo when creating a PR. No repositories are guessed.
        """
        def read(state):
            names = text_list(state.get("repositories", []), "repositories")
            names += [nonblank(row.get("repo"), "Stored PR repo") for row in pr_records(state)]
            return {"repositories": sorted(set(names))}
        return self.store.run("list_repositories", {}, read)

    def gh_pr_create(self, repo: str, title: str, body: str, branch: str, base: str = "main") -> str:
        """Create and store a pull request in a repository from list_repositories.

        repo is an exact listed name. title and branch must be nonempty. branch
        is the source branch; base is the target branch and defaults to main.
        They must differ. The repository's existing body rule requires an ENG-
        ticket number and a '## Test plan' heading. body is the full Markdown text.
        Returns ok=true and the stored PR with a unique id and mock URL, or ok=false.
        """
        args = {"repo": repo, "title": title, "body": body, "branch": branch, "base": base}
        def create(state):
            for key, value in args.items():
                nonblank(value, key)
            names = set(text_list(state.get("repositories", []), "repositories")) | {r.get("repo") for r in pr_records(state)}
            if repo not in names:
                raise ValueError("Unknown repository. Use list_repositories.")
            if branch == base:
                raise ValueError("branch and base must differ.")
            if not re.search(r"\bENG-[0-9]+\b", body) or not re.search(r"(?m)^## Test plan\s*$", body):
                raise ValueError("The PR body requires an ENG- ticket number and a '## Test plan' heading.")
            row = self.store.new_record("pr", {**args, "status": "open"})
            row["url"] = f"https://github.mock/{repo}/pull/{row['id']}"
            state.setdefault("prs", []).append(row)
            return {"ok": True, "pr": row}
        return self.store.run("gh_pr_create", args, create, write=True)

    def post_pr_comment(self, pr_id: str, body: str) -> str:
        """Post nonempty body text on the existing pr_id from list_open_prs.

        pr_id is the returned id, not a title or an invented name. Markdown is
        stored exactly as supplied. Returns ok=true and the stored comment,
        including its unique id and pr_id. Missing PRs return ok=false.
        """
        args = {"pr_id": pr_id, "body": body}
        def comment(state):
            one_record(pr_records(state), pr_id)
            nonblank(body, "body")
            row = self.store.new_record("prc", args)
            state.setdefault("pr_comments", []).append(row)
            return {"ok": True, "comment": row}
        return self.store.run("post_pr_comment", args, comment, write=True)

    def list_decks(self) -> str:
        """List stored decks, their identifiers, and their slide content."""
        return self.store.run("list_decks", {}, lambda state: list(state.get("decks", {}).values()))

    def update_deck_slide(self, deck: str, slide: int, content: str) -> str:
        """Replace one slide's entire text with content; do not supply editing instructions.

        deck is an existing identifier from list_decks or a nonempty name for a
        new deck. slide is a positive, one-based slide number. Missing decks or
        slides are created. content is literal text; an empty string clears it.
        Returns ok=true with the stored deck identifier and resulting slide.
        """
        args = {"deck": deck, "slide": slide, "content": content}
        def replace(state):
            nonblank(deck, "deck")
            integer(slide, "slide")
            if not isinstance(content, str):
                raise ValueError("content must be text.")
            target = state.setdefault("decks", {}).setdefault(deck, {"id": deck, "slides": {}})
            target["slides"][str(slide)] = {"number": slide, "content": content}
            return {"ok": True, "deck": deck, "slide": target["slides"][str(slide)]}
        return self.store.run("update_deck_slide", args, replace, write=True)

    def create_doc(self, title: str, body: str, folder: str | None = None) -> str:
        """Create a document with a nonempty title and literal body text.

        body may be empty. folder is an optional nonempty organizational label;
        omit it to use 'default'. A new label is allowed. Markdown and plain text
        are stored unchanged. Returns ok=true and the stored document, including
        its unique id, mock URL, title, body, and folder.

        For formatted lists, use CommonMark Markdown with -, *, or + bullet
        markers. Word limits count whitespace-separated visible text, excluding
        formatting markers and link destinations.
        """
        args = {"title": title, "body": body, "folder": folder}
        def create(state):
            nonblank(title, "title")
            if not isinstance(body, str):
                raise ValueError("body must be text.")
            location = nonblank(folder, "folder") if folder is not None else "default"
            row = self.store.new_record("doc", {"title": title, "body": body, "folder": location})
            row["url"] = f"https://docs.mock/{row['id']}"
            state.setdefault("docs", []).append(row)
            return {"ok": True, "document": row}
        return self.store.run("create_doc", args, create, write=True)

    def list_docs(self) -> str:
        """List stored documents and the exact ids used by document tools."""
        def read(state):
            return [{**row, "id": key} for row, key in document_records(state)]
        return self.store.run("list_docs", {}, read)

    def update_doc(self, doc_id: str, body: str, title: str | None = None,
                   folder: str | None = None, mode: DOC_UPDATE_MODE = "replace") -> str:
        """Change an existing document selected by exact doc_id from list_docs.

        body is literal text. replace stores body as the complete new document body;
        append adds it after one blank line when existing text is present. title and
        folder are optional replacements. mode must be replace or append. An unknown
        id or invalid field returns ok=false without creating or changing a document.
        Returns the complete stored document after the change.
        """
        args = {"doc_id": doc_id, "body": body, "title": title, "folder": folder, "mode": mode}
        def update(state):
            matches = [(row, key) for row, key in document_records(state) if key == doc_id]
            if len(matches) != 1:
                raise ValueError("doc_id must identify exactly one existing document. Use list_docs.")
            target, key = matches[0]
            if not isinstance(body, str):
                raise ValueError("body must be text.")
            if mode not in ("replace", "append"):
                raise ValueError("mode must be replace or append.")
            if title is not None:
                nonblank(title, "title")
            if folder is not None:
                nonblank(folder, "folder")
            if mode == "append" and target.get("body"):
                target["body"] = f"{str(target['body']).rstrip()}\n\n{body.lstrip()}"
            else:
                target["body"] = body
            if title is not None:
                target["title"] = title
            if folder is not None:
                target["folder"] = folder
            target["updated_at"] = self.store.backend._now_iso()
            return {"ok": True, "document": {**target, "id": key}}
        return self.store.run("update_doc", args, update, write=True)

    def post_doc_comment(self, doc_id: str, body: str) -> str:
        """Post nonempty body text on an existing document selected by exact doc_id.

        doc_id comes from list_docs. Titles and new thread names are not accepted.
        Returns the complete stored comment. Unknown ids change nothing.
        """
        args = {"doc_id": doc_id, "body": body}
        def comment(state):
            if len([(row, key) for row, key in document_records(state) if key == doc_id]) != 1:
                raise ValueError("doc_id must identify exactly one existing document. Use list_docs.")
            nonblank(body, "body")
            row = self.store.new_record("docc", args)
            state.setdefault("doc_comments", []).append(row)
            return {"ok": True, "comment": row}
        return self.store.run("post_doc_comment", args, comment, write=True)

    def list_headcount_plans(self) -> str:
        """List stored headcount plans and their plan_id values."""
        return self.store.run("list_headcount_plans", {}, lambda state: list(state.get("headcount_plans", {}).values()))

    def update_headcount_plan(self, plan_id: str, next_two_hires: list[str | None] | None = None,
                              revisit_after: str | None = None, rationale_tags: list[str] | None = None,
                              notes: str | None = None) -> str:
        """Create or update a headcount plan identified by plan_id.

        plan_id is a nonempty new identifier or one from list_headcount_plans.
        next_two_hires replaces exactly two ordered positions; each is a nonempty
        role description or null for an unfilled position. revisit_after replaces
        a milestone description; rationale_tags replaces a list of nonempty labels;
        notes replaces free text. Use [null, null] to clear both hiring positions,
        [] to clear rationale_tags, and empty text to clear revisit_after or notes.
        Omitted or null arguments leave existing fields unchanged.
        Returns ok=true and the stored plan. Invalid inputs change nothing.
        """
        args = {"plan_id": plan_id, "next_two_hires": next_two_hires, "revisit_after": revisit_after,
                "rationale_tags": rationale_tags, "notes": notes}
        def update(state):
            nonblank(plan_id, "plan_id")
            if next_two_hires is not None:
                if not isinstance(next_two_hires, list) or len(next_two_hires) != 2:
                    raise ValueError("next_two_hires must contain exactly two positions.")
                for item in next_two_hires:
                    if item is not None:
                        nonblank(item, "Role description")
            if rationale_tags is not None:
                text_list(rationale_tags, "rationale_tags")
            optional_text(revisit_after, "revisit_after")
            optional_text(notes, "notes")
            row = state.setdefault("headcount_plans", {}).setdefault(plan_id, {"plan_id": plan_id})
            row.update({key: value for key, value in args.items() if value is not None})
            return {"ok": True, "plan": row}
        return self.store.run("update_headcount_plan", args, update, write=True)
