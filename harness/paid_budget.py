"""Opt-in, process-shared provider-token budget for explicitly approved runs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

from harness.durable_json import atomic_json as _atomic_json


class PaidBudgetError(RuntimeError):
    pass


def _money(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise PaidBudgetError("invalid budget amount") from exc
    if not result.is_finite() or result <= 0:
        raise PaidBudgetError("budget amounts and rates must be finite and positive")
    return result


def _positive_int(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise PaidBudgetError("budget token and request limits must be positive integers")
    return value


class PaidBudget:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        raw = (self.directory / "approval.json").read_bytes()
        self.approval_hash = hashlib.sha256(raw).hexdigest()
        self.approval = json.loads(raw)
        if (self.approval.get("version") != 1 or self.approval.get("approved") is not True
                or not self.approval.get("work_sha256") or not self.approval.get("rate_source")
                or not isinstance(self.approval.get("models"), dict) or not self.approval["models"]):
            raise PaidBudgetError("a bound work approval and explicit pricing source are required")
        self.limit = _money(self.approval["usd_limit"])
        self.request_limit = _positive_int(self.approval["max_provider_requests"])
        for rate in self.approval["models"].values():
            _money(rate["input_usd_per_million"])
            _money(rate["output_usd_per_million"])
            _positive_int(rate["max_input_tokens"])
            _positive_int(rate["max_output_tokens"])

    @contextmanager
    def _ledger(self) -> Iterator[dict[str, Any]]:
        with (self.directory / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if hashlib.sha256((self.directory / "approval.json").read_bytes()).hexdigest() != self.approval_hash:
                raise PaidBudgetError("paid approval changed during execution")
            path = self.directory / "ledger.json"
            state = json.loads(path.read_text()) if path.exists() else {
                "approval_sha256": self.approval_hash, "calls": [], "blocked": False}
            if state["approval_sha256"] != self.approval_hash:
                raise PaidBudgetError("paid budget cannot be reset by changing its approval")
            yield state
            _atomic_json(path, state)

    def rate(self, model: str) -> dict[str, Any]:
        rate = self.approval["models"].get(model)
        if rate is None:
            raise PaidBudgetError(f"no approved pricing for model {model}")
        return rate

    def reserve(self, model: str, request: dict[str, Any]) -> int:
        rate = self.rate(model)
        output = request.get("max_completion_tokens", request.get("max_output_tokens", request.get("max_tokens")))
        if type(output) is not int or not 0 < output <= rate["max_output_tokens"]:
            raise PaidBudgetError("provider request needs an approved explicit output-token limit")
        # Reject unusually large serialized inputs rather than assume their
        # token count fits. Charge the full approved input ceiling regardless.
        size = len(json.dumps(request, ensure_ascii=False).encode())
        framing = 4096 + 1024 * len(request.get("messages", request.get("input", [])))
        if size + framing > rate["max_input_tokens"]:
            raise PaidBudgetError("request exceeds the conservative approved input bound")
        reserved = (Decimal(rate["max_input_tokens"]) * _money(rate["input_usd_per_million"])
                    + Decimal(output) * _money(rate["output_usd_per_million"])) / 1_000_000
        with self._ledger() as state:
            spent = sum((Decimal(row["charged_usd"]) for row in state["calls"]), Decimal(0))
            if state["blocked"] or len(state["calls"]) >= self.request_limit or spent + reserved > self.limit:
                raise PaidBudgetError("approved provider budget exhausted; no request was sent")
            call_id = len(state["calls"])
            state["calls"].append({
                "model": model, "request_sha256": hashlib.sha256(
                    json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                "status": "reserved_or_uncertain", "charged_usd": str(reserved),
                "max_input_tokens": rate["max_input_tokens"], "max_output_tokens": output,
            })
        return call_id

    def settle(self, call_id: int, usage: dict[str, Any] | None) -> None:
        violation = False
        with self._ledger() as state:
            row = state["calls"][call_id]
            if row["status"] != "reserved_or_uncertain":
                raise PaidBudgetError("provider request was already accounted for")
            prompt = (usage or {}).get("prompt_tokens")
            completion = (usage or {}).get("completion_tokens")
            if any(type(value) is not int or value < 0 for value in (prompt, completion)):
                row["status"] = "usage_missing_full_reservation_retained"
                return
            rate = self.rate(row["model"])
            actual = (Decimal(prompt) * _money(rate["input_usd_per_million"])
                      + Decimal(completion) * _money(rate["output_usd_per_million"])) / 1_000_000
            row.update(status="accounted", charged_usd=str(actual), usage=usage)
            if prompt > row["max_input_tokens"] or completion > row["max_output_tokens"]:
                state["blocked"] = violation = True
        if violation:
            raise PaidBudgetError("provider exceeded the approved token bound; further calls are blocked")


def active_budget() -> PaidBudget | None:
    directory = os.environ.get("DOLPHINBENCH_PAID_BUDGET")
    return PaidBudget(Path(directory)) if directory else None


def budgeted_completion(client: Any, request: dict[str, Any], budget: PaidBudget) -> Any:
    request = dict(request)
    model = request["model"]
    request.setdefault("max_completion_tokens", budget.rate(model)["max_output_tokens"])
    bounded_client = client.with_options(max_retries=0)
    call_id = budget.reserve(model, request)
    response = bounded_client.chat.completions.create(**request)
    usage = getattr(response, "usage", None)
    budget.settle(call_id, usage.model_dump() if usage is not None else None)
    return response
