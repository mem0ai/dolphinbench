"""Candidate lookup and operational tools with explicit inputs and honest results."""

from __future__ import annotations

import hashlib
import math
import time
from typing import Literal

from mock_mcp.repair_support import CandidateStore, integer, nonblank, one_record, text_list


def vector_value(value) -> list[float]:
    if (not isinstance(value, list) or len(value) != 8
            or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value)
            or not any(value)):
        raise ValueError("Use an eight-number, finite, nonzero vector from embeddings_encode.")
    return value


class CandidateOperations:
    def __init__(self, store: CandidateStore):
        self.store = store
        self.metrics_times = []

    def place_order(self, restaurant: str, items: list[str], notes: str = "") -> str:
        """Record a simulated food or drink order.

        restaurant is a nonempty restaurant name. items is a nonempty list of
        nonempty item descriptions, including quantities or choices when needed.
        notes contains optional instructions. Text is stored as supplied; this
        tool does not resolve 'the usual' or invent item choices or delivery times.
        Returns ok=true and the recorded order, or ok=false without changes.
        """
        args = {"restaurant": restaurant, "items": items, "notes": notes}
        def order(state):
            nonblank(restaurant, "restaurant")
            text_list(items, "items", allow_empty=False)
            if not isinstance(notes, str):
                raise ValueError("notes must be text.")
            row = self.store.new_record("order", args)
            state.setdefault("orders", []).append(row)
            return {"ok": True, "order": row}
        return self.store.run("place_order", args, order, write=True)

    def check_project_status(self, project_id: str | None = None) -> str:
        """Read supplied project records. Omit project_id or use null to list all
        projects and their ids. Otherwise supply one returned id exactly. No words
        such as 'weekly' or 'ship' have special meaning. Unknown ids return ok=false.
        """
        def read(state):
            raw = state.get("projects", {})
            if isinstance(raw, dict):
                rows = [{**value, "id": key} for key, value in raw.items()]
            elif isinstance(raw, list):
                rows = raw
            else:
                raise ValueError("Stored projects must be records with explicit ids.")
            for row in rows:
                nonblank(row.get("id"), "Project id")
            if project_id is None:
                return {"projects": rows}
            return {"project": one_record(rows, project_id)}
        return self.store.run("check_project_status", {"project_id": project_id}, read)

    def list_stock_tickers(self) -> str:
        """List the uppercase ticker symbols with supplied stock snapshots."""
        return self.store.run("list_stock_tickers", {}, lambda state: {"tickers": sorted(state.get("stock_data", {}))})

    def get_stock_data(self, ticker: str) -> str:
        """Return the supplied stock snapshot for a ticker from list_stock_tickers.

        Symbol case and surrounding whitespace are ignored. The returned fields
        are stored mock data, not a live market quote. Unknown tickers return
        ok=false; this tool never substitutes a made-up default stock price.
        """
        def read(state):
            key = nonblank(ticker, "ticker").strip().upper()
            data = state.get("stock_data", {}).get(key)
            if not isinstance(data, dict):
                raise ValueError("Unknown ticker. Use list_stock_tickers.")
            return {**data, "ticker": key}
        return self.store.run("get_stock_data", {"ticker": ticker}, read)

    def list_services(self) -> str:
        """List the exact, case-sensitive service names with supplied metrics."""
        return self.store.run("list_services", {}, lambda state: {"services": sorted(state.get("service_metrics", {}))})

    def metrics_fetch(self, service: str) -> str:
        """Read stored mock metrics for one exact service name from list_services.

        At most three successful single-service requests are allowed in a rolling
        60-second window in this server. This is a request limit, not a concurrency
        limit. A rate-limited result includes retry_after_seconds. Unknown services
        fail without consuming a request. Use metrics_fetch_batch for a service list.
        """
        def read(state):
            nonblank(service, "service")
            records = state.get("service_metrics", {})
            if service not in records:
                raise ValueError("Unknown service. Use list_services.")
            now = time.monotonic()
            recent = [value for value in self.metrics_times if now - value < 60]
            if len(recent) >= 3:
                return {"ok": False, "error": "Single-service request limit reached.",
                        "retry_after_seconds": max(1, math.ceil(60 - (now - recent[0])))}
            self.metrics_times = recent + [now]
            return {"ok": True, "service": service, "metrics": records[service]}
        return self.store.run("metrics_fetch", {"service": service}, read)

    def metrics_fetch_batch(self, services: list[str]) -> str:
        """Read supplied mock metrics for a nonempty list of exact service names.

        Use list_services for names. Duplicate names are returned once. An unknown
        service fails the entire request. No single-service rate limit applies.
        Returns results keyed by service name; list order has no meaning.
        """
        def read(state):
            keys = text_list(services, "services", allow_empty=False)
            records = state.get("service_metrics", {})
            if any(key not in records for key in keys):
                raise ValueError("Unknown service. Use list_services.")
            return {"ok": True, "results": {key: records[key] for key in keys}}
        return self.store.run("metrics_fetch_batch", {"services": services}, read)

    def batch_dedup(self, items: list[str]) -> str:
        """Remove adjacent duplicate strings from an ascending-sorted list.

        items must be strings sorted by case-sensitive Unicode code-point order.
        Empty strings and an empty list are allowed. Unsorted input returns
        ok=false; this operation does not sort the list for you.
        Returns unique strings and unique_count without changing stored records.
        """
        def dedup(state):
            if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                raise ValueError("items must be a list of strings.")
            if items != sorted(items):
                raise ValueError("items must be sorted ascending before deduplication.")
            result = []
            for item in items:
                if not result or result[-1] != item:
                    result.append(item)
            return {"unique_count": len(result), "unique": result}
        return self.store.run("batch_dedup", {"items": items}, dedup)

    def embeddings_encode(self, text: str) -> str:
        """Encode text as an eight-number deterministic mock vector for vector_search.

        This is a hash-based simulation, not a semantic embedding model. The same
        text produces the same vector. Empty text is allowed. Returns vector and dim.
        """
        def encode(state):
            if not isinstance(text, str):
                raise ValueError("text must be a string.")
            vector = [byte / 255.0 for byte in hashlib.sha256(text.encode()).digest()[:8]]
            return {"vector": vector, "dim": len(vector)}
        return self.store.run("embeddings_encode", {"text": text}, encode)

    def list_vector_namespaces(self) -> str:
        """List namespace names available in the supplied vector_documents data."""
        return self.store.run("list_vector_namespaces", {}, lambda state: {"namespaces": sorted(state.get("vector_documents", {}))})

    def vector_search(self, query_vec: list[float], namespace: str, top_k: int = 5) -> str:
        """Search supplied vectors in one namespace from list_vector_namespaces.

        query_vec is a finite nonzero eight-number vector from embeddings_encode,
        not text. top_k is positive. Stored documents have id, vector, and optional
        snippet fields. Returns up to top_k records ranked by cosine similarity,
        with id, score, and snippet, plus ok=true on success. Invalid vectors or
        unknown namespaces return ok=false and an error.
        """
        args = {"query_vec": query_vec, "namespace": namespace, "top_k": top_k}
        def search(state):
            query = vector_value(query_vec)
            nonblank(namespace, "namespace")
            integer(top_k, "top_k")
            documents = state.get("vector_documents", {}).get(namespace)
            if not isinstance(documents, list):
                raise ValueError("Unknown namespace. Use list_vector_namespaces.")
            # Normalize first to avoid overflow for large but finite inputs.
            def unit(vector):
                scaled = [value / max(abs(item) for item in vector) for value in vector]
                norm = math.sqrt(math.fsum(value * value for value in scaled))
                return [value / norm for value in scaled]
            q = unit(query)
            results = []
            for row in documents:
                nonblank(row.get("id"), "Document id")
                v = unit(vector_value(row.get("vector")))
                score = max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(q, v))))
                results.append({"id": row["id"], "score": score, "snippet": row.get("snippet", "")})
            return {"ok": True, "results": sorted(results, key=lambda row: (-row["score"], row["id"]))[:top_k]}
        return self.store.run("vector_search", args, search)

    def shell_run(self, command: str) -> str:
        """Record a nonempty shell command for simulation; do not execute it.

        Returns ok=true, the recorded command, and executed=false. No host files
        are read or changed, and no successful command exit status is claimed.
        """
        def record(state):
            nonblank(command, "command")
            return {"ok": True, "command": command, "executed": False}
        return self.store.run("shell_run", {"command": command}, record)

    def _search(self, tool: str, query: str, version: str) -> str:
        def read(state):
            text = nonblank(query, "query").strip().casefold()
            rows = list(state.get(f"internal_search_{version}_documents", []))
            if version == "v2":
                rows.extend(state.get("internal_docs", []))
            results = []
            seen = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Each stored search document must be an object.")
                key = nonblank(row.get("id") or row.get("doc_id"), "Stored search document id")
                searchable = "\n".join((str(row.get("title", "")), str(row.get("body", "")),
                                         " ".join(str(tag) for tag in row.get("tags", []))))
                if text in searchable.casefold() and key not in seen:
                    results.append({**row, "id": key})
                    seen.add(key)
            response = {"results": results}
            if version == "v1":
                response.update(stale=True, warning="This is the retired v1 snapshot. Use internal_search_v2 for the current index.")
            return response
        return self.store.run(tool, {"query": query}, read)

    def internal_search_v1_legacy(self, query: str) -> str:
        """Search supplied retired v1 documents by case-insensitive substring in title
        or body. query must be nonempty. Returns matching records with a stale warning;
        use internal_search_v2 for the current supplied index. No documents are invented.
        """
        return self._search("internal_search_v1_legacy", query, "v1")

    def internal_search_v2(self, query: str) -> str:
        """Search supplied current documents by case-insensitive text in title, body,
        or tags. query must be nonempty. Returns the matching stored records and exact
        document ids, or an empty list. No documents or search results are invented.
        """
        return self._search("internal_search_v2", query, "v2")

    def deploy_via_kubectl(self, branch: str, env: Literal["staging", "prod"] = "staging") -> str:
        """Retired deployment operation. Always returns ok=false without deploying.
        Use deploy_via_pipeline with branch and env (staging or prod).
        """
        return self.store.error("deploy_via_kubectl", {"branch": branch, "env": env},
                                "Direct kubectl deployment is retired. Use deploy_via_pipeline.")

    def deploy_via_pipeline(self, branch: str, env: Literal["staging", "prod"] = "staging") -> str:
        """Queue a simulated deployment of a nonempty branch to staging or prod.

        Returns ok=true and the stored deployment with status=queued and a unique
        id. Queued does not mean deployed; this tool does not execute a real rollout.
        Invalid environments or blank branches fail without creating a deployment.
        """
        args = {"branch": branch, "env": env}
        def deploy(state):
            nonblank(branch, "branch")
            if env not in ("staging", "prod"):
                raise ValueError("env must be staging or prod.")
            row = self.store.new_record("deployment", {**args, "status": "queued"})
            state.setdefault("deployments", []).append(row)
            return {"ok": True, "deployment": row}
        return self.store.run("deploy_via_pipeline", args, deploy, write=True)
