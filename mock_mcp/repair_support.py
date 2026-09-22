"""Validation and result recording shared by the candidate tools."""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, InvalidOperation
from email.headerregistry import Address
import json
import re
from threading import RLock
from typing import Any
from uuid import uuid4


def nonblank(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string.")
    return value


def email_address(value: str) -> str:
    raw = nonblank(value, "Email address").strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("Email address must not contain control characters.")
    try:
        address = Address(addr_spec=raw)
        domain = address.domain.encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Use one email address, not a name or a list of recipients.") from exc
    if (not address.username or not domain or len(domain) > 253
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                   for label in domain.split("."))):
        raise ValueError("Use an email address with a mailbox and a valid domain name.")
    return Address(username=address.username, domain=domain).addr_spec


def phone_number(value: str) -> str:
    raw = nonblank(value, "Phone number").strip()
    if not re.fullmatch(r"\+[1-9][0-9]{1,14}", raw):
        raise ValueError("Use an international phone number: +, country code, and digits only (at most 15 digits).")
    return raw


def date_value(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Use a calendar date in YYYY-MM-DD format.")
    return date.fromisoformat(value)


def amount_value(value: Any, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("Amount must be a finite number.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Amount must be a finite number.") from exc
    if not amount.is_finite() or amount < 0 or (not allow_zero and amount == 0):
        raise ValueError("Amount must be positive." if not allow_zero else "Amount must be nonnegative.")
    fractional_digits = max(-amount.as_tuple().exponent - 2, 0)
    if fractional_digits and any(amount.as_tuple().digits[-fractional_digits:]):
        raise ValueError("USD amounts must have at most two decimal places.")
    return amount


def integer(value: int, field: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer of at least {minimum}.")
    return value


def text_list(value: list[str], field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValueError(f"{field} must be a {'nonempty ' if not allow_empty else ''}list of strings.")
    return [nonblank(item, field) for item in value]


def one_record(rows: list[dict], record_id: str) -> dict:
    nonblank(record_id, "id")
    matches = [row for row in rows if row.get("id") == record_id]
    if len(matches) != 1:
        raise ValueError("The id must identify exactly one existing record. Use the corresponding list tool.")
    return matches[0]


class CandidateStore:
    def __init__(self, backend: Any):
        self.backend = backend
        self.lock = RLock()

    def read(self) -> dict:
        return copy.deepcopy(self.backend._load_state())

    def reply(self, tool: str, args: dict, result: Any) -> str:
        self.backend._log_call(tool, copy.deepcopy(args), copy.deepcopy(result))
        return json.dumps(result)

    def error(self, tool: str, args: dict, message: str) -> str:
        return self.reply(tool, args, {"ok": False, "error": message})

    def new_record(self, prefix: str, fields: dict) -> dict:
        return {"id": f"{prefix}_{uuid4().hex}", **fields, "created_at": self.backend._now_iso()}

    def save(self, state: dict) -> None:
        self.backend._save_state(state)

    def run(self, tool: str, args: dict, operation, *, write: bool = False) -> str:
        with self.lock:
            state = self.read()
            try:
                result = operation(state)
            except ValueError as exc:
                return self.error(tool, args, str(exc))
            if write:
                self.save(state)
            return self.reply(tool, args, result)

    def deliver(self, tool: str, args: dict, collection: str, fields: dict) -> str:
        with self.lock:
            entry = {"id": f"{tool}_{uuid4().hex}", **fields,
                     "sent_at": self.backend._now_iso()}
            state = self.read()
            state.setdefault(collection, []).append(entry)
            self.save(state)
            return self.reply(tool, args, {"ok": True, **entry})
