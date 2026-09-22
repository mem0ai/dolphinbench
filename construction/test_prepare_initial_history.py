from __future__ import annotations

import ast
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from construction import prepare_initial_history as subject


class PrepareInitialHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.persona = "alex"
        self.sessions = [
            {
                "id": f"alex-s{number}",
                "label": f"Session {number}",
                "session_type": "check-in",
                "narrative_date": f"2026-01-0{number}T09:00:00+00:00",
                "purpose": {"kind": "check-in", "sequence": number},
                "channel": "chat",
                "participants": ["alex", "assistant"],
                "messages": [f"original {number}a", f"original {number}b"],
            }
            for number in range(1, 6)
        ]
        self.facts = [
            {
                "id": 1,
                "statement": "Alex has a protected first fact.",
                "applies_when": "always",
                "source_session_ids": ["alex-s1"],
                "related_history_session_ids": ["alex-s3"],
            },
            {
                "id": 2,
                "statement": "Alex has a protected fifth fact.",
                "applies_when": "sometimes",
                "source_session_ids": ["alex-s5"],
                "related_history_session_ids": ["alex-s4"],
            },
        ]
        self.source_document = {
            "schema_version": 7,
            "persona": "alex",
            "simulation": {"timezone": "UTC", "flags": ["preserve-me"]},
            "sessions": self.sessions,
            "top_level_state": {"active_channel": "chat", "counter": 12},
        }
        directory = self.root / "registry" / "personas" / self.persona
        directory.mkdir(parents=True)
        (directory / "life_sim.yaml").write_text(
            yaml.safe_dump(self.source_document, sort_keys=False), encoding="utf-8"
        )
        (directory / "facts.yaml").write_text(
            yaml.safe_dump({"version": 3, "facts": self.facts}, sort_keys=False), encoding="utf-8"
        )
        (directory / "persona_sheet.md").write_text(
            "# Alex\nPersona context that must be supplied to every stage.\n", encoding="utf-8"
        )
        manifest_directory = self.root / "mock_mcp" / "manifests"
        manifest_directory.mkdir(parents=True)
        (manifest_directory / "alex.yaml").write_text(
            yaml.safe_dump(
                {
                    "persona_id": "alex",
                    "tools": ["list_calendar_events", "send_email", "get_pr"],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (self.root / "mock_mcp" / "server.py").write_text(
            """\
def list_calendar_events(date: str) -> str:
    return date

def send_email(to: str, subject: str, body: str, cc: list[str] | None = None) -> str:
    return to

def get_pr(pr_id: str) -> str:
    return pr_id
""",
            encoding="utf-8",
        )
        contracts_directory = self.root / "mock_mcp"
        (contracts_directory / "tool_contracts.yaml").write_text(
            yaml.safe_dump(
                {
                    "tools": {
                        "list_calendar_events": {"reads_state_keys": ["calendar"]},
                        "send_email": {"writes_state_keys": ["sent_emails"]},
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.patch_root = patch.object(subject, "ROOT", self.root)
        self.patch_root.start()

    def tearDown(self) -> None:
        self.patch_root.stop()
        self.temporary.cleanup()

    @staticmethod
    def read(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def source_path(self, name: str) -> Path:
        return self.root / "registry" / "personas" / self.persona / name

    def prepare_run(self, name: str = "run") -> Path:
        run = self.root / name
        subject.prepare(persona=self.persona, out=run)
        return run

    def write_json(self, path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def problem(
        self,
        *,
        session_id: str = "alex-s3",
        message_index: int = 0,
        evidence_session_ids: list[str] | None = None,
        exact_text: str | None = None,
    ) -> dict:
        if exact_text is None:
            session_number = int(session_id.rsplit("s", 1)[1])
            exact_text = f"original {session_number}{'a' if message_index == 0 else 'b'}"
        return {
            "session_id": session_id,
            "message_index": message_index,
            "exact_text": exact_text,
            "evidence_session_ids": evidence_session_ids or ["alex-s1", "alex-s5"],
            "what_is_wrong": "The message has a demonstrated problem.",
            "information_that_must_remain": "The underlying user fact must remain.",
        }

    def checked_messages(self) -> list[dict]:
        return [
            {"session_id": session["id"], "message_index": index}
            for session in self.sessions
            for index, _ in enumerate(session["messages"])
        ]

    def accept_problems(
        self,
        run: Path,
        problems: list[dict],
        *,
        messages_checked: list[dict] | None = None,
    ) -> None:
        self.write_json(
            run / "accepted" / "01_find_message_problems.json",
            {
                "problems": problems,
                "messages_checked": self.checked_messages() if messages_checked is None else messages_checked,
            },
        )

    def advance_to_rewrite(self, *, target: str = "alex-s3", index: int = 0, name: str = "run") -> Path:
        run = self.prepare_run(name=name)
        self.accept_problems(run, [self.problem(session_id=target, message_index=index)])
        result = subject.advance(persona=self.persona, run=run)
        self.assertEqual(result["status"], "request_written")
        return run

    def accept_rewrite_for(self, run: Path, session_id: str, messages: list[str]) -> None:
        self.write_json(
            run / "accepted" / "02_rewrite_messages.json",
            {"sessions": [{"session_id": session_id, "messages": messages}]},
        )

    def write_approved_corrections(
        self,
        run: Path,
        *,
        message_corrections: list[dict] | None = None,
        fact_statement_corrections: list[dict] | None = None,
    ) -> None:
        self.write_json(
            run / "corrections" / "approved_text_corrections.json",
            {
                "version": 1,
                "persona": self.persona,
                "message_corrections": message_corrections or [],
                "fact_statement_corrections": fact_statement_corrections or [],
            },
        )

    def write_entity_corrections(self, run: Path, **overrides: list[dict]) -> None:
        value = {
            "version": 1,
            "persona": self.persona,
            "introduced_in_corrections": [],
            "alias_removals": [],
            "entity_removals": [],
            "entity_additions": [],
        }
        value.update(overrides)
        self.write_json(run / "corrections" / "approved_entity_inventory_corrections.json", value)

    def write_fact_subject_corrections(self, run: Path, corrections: list[dict]) -> None:
        self.write_json(
            run / "corrections" / "approved_fact_subject_corrections.json",
            {
                "version": 1,
                "persona": self.persona,
                "source_response": "responses/existing_fact_subjects.json",
                "corrections": corrections,
            },
        )

    def advance_to_app_effects(self, *, name: str = "app-effects") -> Path:
        run = self.prepare_run(name=name)
        self.accept_problems(run, [self.problem(session_id="alex-s1", evidence_session_ids=["alex-s2"])])
        subject.advance(persona=self.persona, run=run)
        self.accept_rewrite_for(run, "alex-s1", ["repaired 1a", "original 1b"])
        subject.advance(persona=self.persona, run=run)
        self.write_json(
            run / "accepted" / "03_entity_inventory.json",
            {"entities": [{"id": "alex", "name": "Alex", "kind": "person", "aliases": [], "introduced_in": "alex-s1", "reason": "user"}]},
        )
        subject.advance(persona=self.persona, run=run)
        self.write_json(
            run / "accepted" / "04_existing_fact_subjects.json",
            {"fact_subjects": [{"fact_id": 1, "subjects": ["alex"]}, {"fact_id": 2, "subjects": ["alex"]}]},
        )
        subject.advance(persona=self.persona, run=run)
        self.write_json(
            run / "accepted" / "05_missing_facts.json",
            {"facts": [
                {"id": 3, "statement": "New earlier fact", "applies_when": "until changed", "source_session_ids": ["alex-s1"], "related_history_session_ids": [], "subjects": ["alex"], "supersedes": []},
                {"id": 4, "statement": "New later fact", "applies_when": "always", "source_session_ids": ["alex-s5"], "related_history_session_ids": ["alex-s1"], "subjects": ["alex"], "supersedes": [3]},
            ]},
        )
        result = subject.advance(persona=self.persona, run=run)
        self.assertEqual(result["status"], "request_written")
        self.assertEqual(Path(result["request"]).name, "06_identify_app_effects.json")
        return run

    def valid_app_effect_rows(self) -> list[dict]:
        rows = self.checked_messages()
        for row in rows:
            row["effects"] = []
        rows[0]["effects"] = [{
            "category": "requested_action",
            "evidence_quote": "repaired 1a",
            "reason": "The message asks the assistant to send an email.",
            "tool": "send_email",
        }]
        return rows

    def test_prepare_writes_repair_request_full_context_and_manifest(self) -> None:
        run = self.prepare_run()
        request_dir = run / "requests"
        request = self.read(request_dir / "01_find_message_problems.json")
        manifest = self.read(run / "source_manifest.json")

        self.assertEqual(sorted(path.name for path in run.iterdir()), ["requests", "source_manifest.json"])
        self.assertEqual(sorted(path.name for path in request_dir.iterdir()), ["01_find_message_problems.json"])
        self.assertEqual(request["stage"], "find_message_problems")
        self.assertEqual(request["response_schema"]["required"], ["problems", "messages_checked"])
        self.assertIn(
            "Include every session/message pair in messages_checked, including clean messages.",
            request["system"],
        )
        self.assertIn("A message speaks as if a later event or outcome has already happened.", request["system"])
        self.assertIn("Legitimate future plans and deadlines are not problems.", request["system"])
        self.assertEqual(request["input"]["persona"], self.persona)
        self.assertEqual(
            request["input"]["persona_sheet"],
            self.source_path("persona_sheet.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(request["input"]["protected_existing_facts"], self.facts)
        self.assertEqual(
            request["input"]["sessions"],
            [
                {
                    "id": session["id"],
                    "narrative_date": session["narrative_date"],
                    "label": session["label"],
                    "session_type": session["session_type"],
                    "messages": session["messages"],
                    "linked_existing_fact_ids": [
                        fact["id"]
                        for fact in self.facts
                        if session["id"] in fact["source_session_ids"]
                        or session["id"] in fact["related_history_session_ids"]
                    ],
                }
                for session in self.sessions
            ],
        )
        self.assertEqual(manifest["persona"], self.persona)
        self.assertNotIn("metadata_batch_size", manifest)
        self.assertEqual(set(manifest["source_sha256"]), {"life_sim", "facts", "persona_sheet"})
        source_names = {"life_sim": "life_sim.yaml", "facts": "facts.yaml", "persona_sheet": "persona_sheet.md"}
        for name, digest in manifest["source_sha256"].items():
            self.assertEqual(digest, hashlib.sha256(self.source_path(source_names[name]).read_bytes()).hexdigest())
        self.assertNotIn("entity_inventory", request["stage"])
        self.assertNotIn("existing_fact_subjects", request["stage"])

    def test_accepted_problem_list_rejects_invalid_entries(self) -> None:
        cases = [
            ("unknown session", lambda item: item.update(session_id="alex-s99"), "not allowed"),
            ("bad message index", lambda item: item.update(message_index=99), "invalid session or message index"),
            ("exact text mismatch", lambda item: item.update(exact_text="not the source text"), "exact_text"),
            ("duplicate session and index", lambda item: None, "duplicates"),
            ("invalid evidence session", lambda item: item.update(evidence_session_ids=["alex-s99"]), "not allowed"),
            ("duplicate evidence session", lambda item: item.update(evidence_session_ids=["alex-s1", "alex-s1"]), "invalid evidence"),
        ]
        for label, mutate, message in cases:
            with self.subTest(label=label):
                run = self.prepare_run(name=f"run-{label.replace(' ', '-')}")
                first = self.problem()
                mutate(first)
                problems = [first, copy.deepcopy(first)] if label == "duplicate session and index" else [first]
                self.accept_problems(run, problems)
                with self.assertRaisesRegex(ValueError, message):
                    subject.advance(persona=self.persona, run=run)

    def test_messages_checked_rejects_missing_duplicate_and_invalid_rows(self) -> None:
        cases = [
            (
                "missing row",
                lambda rows: rows.pop(),
                "messages_checked must contain every source session/message pair exactly once",
            ),
            (
                "duplicate row",
                lambda rows: rows.__setitem__(-1, copy.deepcopy(rows[0])),
                "messages_checked duplicates alex-s1",
            ),
            (
                "invalid row",
                lambda rows: rows[0].update(message_index=99),
                "messages_checked has an invalid session or message index",
            ),
        ]
        for label, mutate, message in cases:
            with self.subTest(label=label):
                run = self.prepare_run(name=f"run-{label.replace(' ', '-')}")
                checked = self.checked_messages()
                mutate(checked)
                self.accept_problems(run, [self.problem()], messages_checked=checked)
                with self.assertRaisesRegex(ValueError, message):
                    subject.advance(persona=self.persona, run=run)

    def test_legacy_problem_response_without_messages_checked_is_still_accepted(self) -> None:
        response = {"problems": [self.problem()]}
        self.assertEqual(
            subject._validate_problems(response, self.sessions), response["problems"]
        )

    def test_valid_problem_acceptance_writes_only_rewrite_and_repeat_is_byte_stable(self) -> None:
        run = self.advance_to_rewrite()
        request_path = run / "requests" / "02_rewrite_messages.json"
        request = self.read(request_path)
        target = request["input"]["targets"][0]

        self.assertEqual(
            sorted(path.name for path in (run / "requests").iterdir()),
            ["01_find_message_problems.json", "02_rewrite_messages.json"],
        )
        self.assertEqual(request["stage"], "rewrite_messages")
        self.assertEqual(sorted(request["input"]), ["persona", "persona_sheet", "targets"])
        self.assertIn("Each item in targets contains", request["system"])
        self.assertIn("write only what the user could know at the session's date", request["system"])
        self.assertIn("Copy every unflagged message exactly", request["system"])
        self.assertIn("Do not invent an event, fact, person, request, action, or explanation", request["system"])
        self.assertEqual(target["target_session"]["id"], "alex-s3")
        self.assertEqual([row["id"] for row in target["previous_two_sessions"]], ["alex-s1", "alex-s2"])
        self.assertEqual([row["id"] for row in target["next_two_sessions"]], ["alex-s4", "alex-s5"])
        self.assertEqual([row["id"] for row in target["evidence_sessions"]], ["alex-s1", "alex-s5"])
        self.assertEqual([fact["id"] for fact in target["linked_protected_existing_facts"]], [1, 2])
        self.assertEqual(
            request["response_schema"]["properties"]["sessions"]["items"]["properties"]["session_id"]["enum"],
            ["alex-s3"],
        )

        before = request_path.read_bytes()
        result = subject.advance(persona=self.persona, run=run)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(request_path.read_bytes(), before)

    def test_accepted_rewrite_rejects_missing_extra_duplicate_bad_count_empty_and_unflagged_changes(self) -> None:
        cases = [
            ("missing target", [], "every flagged session"),
            (
                "extra target",
                [
                    {"session_id": "alex-s3", "messages": ["repaired", "original 3b"]},
                    {"session_id": "alex-s2", "messages": ["original 2a", "original 2b"]},
                ],
                "not allowed",
            ),
            (
                "duplicate target",
                [
                    {"session_id": "alex-s3", "messages": ["repaired", "original 3b"]},
                    {"session_id": "alex-s3", "messages": ["repaired", "original 3b"]},
                ],
                "every flagged session",
            ),
            ("wrong message count", [{"session_id": "alex-s3", "messages": ["repaired"]}], "required message list"),
            ("empty message", [{"session_id": "alex-s3", "messages": ["", "original 3b"]}], "shorter than"),
            (
                "unflagged change",
                [{"session_id": "alex-s3", "messages": ["repaired", "changed unflagged message"]}],
                "unflagged content",
            ),
        ]
        for label, rows, message in cases:
            with self.subTest(label=label):
                run = self.advance_to_rewrite(name=f"run-{label.replace(' ', '-')}")
                self.write_json(run / "accepted" / "02_rewrite_messages.json", {"sessions": rows})
                with self.assertRaisesRegex(ValueError, message):
                    subject.advance(persona=self.persona, run=run)

    def test_valid_rewrite_materializes_only_approved_message_change(self) -> None:
        run = self.advance_to_rewrite()
        self.accept_rewrite_for(run, "alex-s3", ["repaired 3a", "original 3b"])
        result = subject.advance(persona=self.persona, run=run)
        self.assertEqual(result["status"], "request_written")

        expected = copy.deepcopy(self.source_document)
        expected["sessions"][2]["messages"][0] = "repaired 3a"
        repaired_path = run / "repaired" / "life_sim.yaml"
        self.assertTrue(repaired_path.is_file())
        self.assertEqual(yaml.safe_load(repaired_path.read_text(encoding="utf-8")), expected)
        self.assertEqual(
            [session["id"] for session in expected["sessions"]],
            [session["id"] for session in self.source_document["sessions"]],
        )
        self.assertEqual(
            [session["narrative_date"] for session in expected["sessions"]],
            [session["narrative_date"] for session in self.source_document["sessions"]],
        )
        self.assertEqual(expected["sessions"][2]["messages"][1], "original 3b")

    def test_exact_human_corrections_apply_after_rewrites_without_changing_sources(self) -> None:
        run = self.advance_to_rewrite()
        self.accept_rewrite_for(run, "alex-s3", ["repaired 3a", "original 3b"])
        self.write_approved_corrections(
            run,
            message_corrections=[{
                "session_id": "alex-s3",
                "message_index": 0,
                "exact_text": "repaired 3a",
                "replacement_text": "final 3a",
                "reason": "Correct a demonstrated error in the reviewed candidate.",
            }],
            fact_statement_corrections=[{
                "fact_id": 1,
                "exact_statement": "Alex has a protected first fact.",
                "replacement_statement": "Alex has a corrected first fact.",
                "reason": "Correct a demonstrably false statement while preserving its links.",
            }],
        )

        result = subject.advance(persona=self.persona, run=run)
        self.assertEqual(result["status"], "request_written")
        repaired = yaml.safe_load((run / "repaired" / "life_sim.yaml").read_text(encoding="utf-8"))
        self.assertEqual(repaired["sessions"][2]["messages"], ["final 3a", "original 3b"])
        entity_request = self.read(run / "requests" / "03_entity_inventory.json")
        corrected = entity_request["input"]["protected_existing_facts"][0]
        self.assertEqual(corrected["statement"], "Alex has a corrected first fact.")
        self.assertEqual(corrected["source_session_ids"], ["alex-s1"])
        self.assertEqual(corrected["related_history_session_ids"], ["alex-s3"])
        self.assertEqual(
            yaml.safe_load(self.source_path("life_sim.yaml").read_text(encoding="utf-8")),
            self.source_document,
        )
        self.assertEqual(
            yaml.safe_load(self.source_path("facts.yaml").read_text(encoding="utf-8"))["facts"],
            self.facts,
        )

    def test_human_corrections_reject_stale_message_and_fact_text(self) -> None:
        run = self.advance_to_rewrite()
        self.accept_rewrite_for(run, "alex-s3", ["repaired 3a", "original 3b"])
        self.write_approved_corrections(
            run,
            message_corrections=[{
                "session_id": "alex-s3",
                "message_index": 0,
                "exact_text": "not the repaired message",
                "replacement_text": "final 3a",
                "reason": "Test stale text rejection.",
            }],
        )
        with self.assertRaisesRegex(ValueError, "does not match alex-s3\\[0\\]"):
            subject.advance(persona=self.persona, run=run)

        other = self.prepare_run(name="stale-fact")
        self.write_approved_corrections(
            other,
            fact_statement_corrections=[{
                "fact_id": 1,
                "exact_statement": "not the protected statement",
                "replacement_statement": "replacement",
                "reason": "Test stale text rejection.",
            }],
        )
        with self.assertRaisesRegex(ValueError, "does not match fact 1"):
            subject.advance(persona=self.persona, run=other)

    def test_entity_corrections_verify_prior_values_and_ground_aliases(self) -> None:
        run = self.prepare_run(name="entity-corrections")
        entities = [{
            "id": "first_message",
            "name": "first message subject",
            "kind": "other",
            "aliases": ["original 1a", "unsupported alias"],
            "introduced_in": "alex-s3",
            "reason": "The subject recurs in protected history.",
        }]
        self.write_entity_corrections(
            run,
            introduced_in_corrections=[{
                "entity_id": "first_message",
                "exact_introduced_in": "alex-s3",
                "replacement_introduced_in": "alex-s1",
                "reason": "The exact alias appears in the first session.",
            }],
            alias_removals=[{
                "entity_id": "first_message",
                "aliases": ["unsupported alias"],
                "reason": "The alias is absent from the source.",
            }],
            entity_additions=[{
                "entity": {
                    "id": "fifth_message",
                    "name": "fifth message subject",
                    "kind": "other",
                    "aliases": ["original 5a"],
                    "introduced_in": "alex-s5",
                    "reason": "The fifth message establishes this subject.",
                },
                "reason": "A protected subject was missing.",
            }],
        )
        corrected = subject._apply_entity_inventory_corrections(run, self.persona, entities)
        self.assertEqual([entity["id"] for entity in corrected], ["first_message", "fifth_message"])
        self.assertEqual(corrected[0]["introduced_in"], "alex-s1")
        self.assertEqual(corrected[0]["aliases"], ["original 1a"])
        subject._validate_entity_source_grounding(
            corrected,
            self.sessions,
            self.facts,
            self.source_path("persona_sheet.md").read_text(encoding="utf-8"),
        )

        value = self.read(run / "corrections" / "approved_entity_inventory_corrections.json")
        value["introduced_in_corrections"][0]["exact_introduced_in"] = "alex-s2"
        self.write_json(run / "corrections" / "approved_entity_inventory_corrections.json", value)
        with self.assertRaisesRegex(ValueError, "does not match first_message"):
            subject._apply_entity_inventory_corrections(run, self.persona, entities)

    def test_entity_grounding_rejects_unsupported_alias_and_late_introduction(self) -> None:
        base = {
            "id": "subject",
            "name": "descriptive subject",
            "kind": "other",
            "aliases": ["not in the source"],
            "introduced_in": "alex-s1",
            "reason": "test",
        }
        with self.assertRaisesRegex(ValueError, "aliases absent"):
            subject._validate_entity_source_grounding(
                [base], self.sessions, self.facts, "persona",
            )
        late = copy.deepcopy(base)
        late["aliases"] = ["original 1a"]
        late["introduced_in"] = "alex-s3"
        with self.assertRaisesRegex(ValueError, "exact alias before introduced_in"):
            subject._validate_entity_source_grounding(
                [late], self.sessions, self.facts, "persona",
            )

    def test_fact_subject_correction_applies_to_downstream_missing_facts_input(self) -> None:
        run = self.prepare_run(name="fact-subject-correction")
        self.accept_problems(run, [self.problem(session_id="alex-s1", evidence_session_ids=["alex-s2"])])
        subject.advance(persona=self.persona, run=run)
        self.accept_rewrite_for(run, "alex-s1", ["repaired 1a", "original 1b"])
        subject.advance(persona=self.persona, run=run)
        self.write_json(
            run / "accepted" / "03_entity_inventory.json",
            {"entities": [
                {"id": "alex", "name": "Alex", "kind": "person", "aliases": [], "introduced_in": "alex-s1", "reason": "user"},
                {"id": "project", "name": "Project", "kind": "project", "aliases": [], "introduced_in": "alex-s1", "reason": "fact subject"},
            ]},
        )
        subject.advance(persona=self.persona, run=run)
        self.write_json(
            run / "accepted" / "04_existing_fact_subjects.json",
            {"fact_subjects": [{"fact_id": 1, "subjects": ["alex"]}, {"fact_id": 2, "subjects": ["alex"]}]},
        )
        self.write_fact_subject_corrections(run, [{
            "fact_id": 1,
            "exact_subjects": ["alex"],
            "replacement_subjects": ["project"],
            "reason": "The protected fact is about the project, not Alex.",
        }])
        result = subject.advance(persona=self.persona, run=run)
        self.assertEqual(result["status"], "request_written")
        missing = self.read(run / "requests" / "05_missing_facts.json")
        protected = missing["input"]["protected_existing_facts"]
        self.assertEqual(protected[0]["subjects"], ["project"])
        self.assertEqual(protected[1]["subjects"], ["alex"])
        self.assertEqual(
            self.read(run / "accepted" / "04_existing_fact_subjects.json")["fact_subjects"][0]["subjects"],
            ["alex"],
        )

    def test_fact_subject_correction_rejects_exact_list_mismatch(self) -> None:
        run = self.prepare_run(name="fact-subject-mismatch")
        self.write_fact_subject_corrections(run, [{
            "fact_id": 1,
            "exact_subjects": ["wrong", "alex"],
            "replacement_subjects": ["alex"],
            "reason": "Test exact-list validation.",
        }])
        with self.assertRaisesRegex(ValueError, "does not match fact 1"):
            subject._apply_fact_subject_corrections(run, self.persona, {1: ["alex"], 2: ["alex"]}, {"alex"})

    def test_fact_subject_correction_rejects_unknown_replacement_entity(self) -> None:
        run = self.prepare_run(name="fact-subject-unknown-entity")
        self.write_fact_subject_corrections(run, [{
            "fact_id": 1,
            "exact_subjects": ["alex"],
            "replacement_subjects": ["missing"],
            "reason": "Test entity validation.",
        }])
        with self.assertRaisesRegex(ValueError, "invalid replacement subjects for fact 1"):
            subject._apply_fact_subject_corrections(run, self.persona, {1: ["alex"], 2: ["alex"]}, {"alex"})

    def test_fact_subject_correction_rejects_duplicate_fact_correction(self) -> None:
        run = self.prepare_run(name="fact-subject-duplicate")
        self.write_fact_subject_corrections(run, [
            {"fact_id": 1, "exact_subjects": ["alex"], "replacement_subjects": ["alex"], "reason": "First correction."},
            {"fact_id": 1, "exact_subjects": ["alex"], "replacement_subjects": ["alex"], "reason": "Duplicate correction."},
        ])
        with self.assertRaisesRegex(ValueError, "duplicates fact 1"):
            subject._apply_fact_subject_corrections(run, self.persona, {1: ["alex"], 2: ["alex"]}, {"alex"})

    def test_entity_request_uses_repaired_text_and_metadata_advances_from_it(self) -> None:
        run = self.prepare_run()
        self.accept_problems(run, [self.problem(session_id="alex-s1", evidence_session_ids=["alex-s2"])])
        subject.advance(persona=self.persona, run=run)
        self.accept_rewrite_for(run, "alex-s1", ["repaired 1a", "original 1b"])
        subject.advance(persona=self.persona, run=run)

        entity_request = self.read(run / "requests" / "03_entity_inventory.json")
        self.assertEqual(entity_request["stage"], "entity_inventory")
        self.assertIn("subjects of every protected fact", entity_request["system"])
        self.assertIn("earliest supplied session", entity_request["system"])
        self.assertIn("differ in owner, time period, or state", entity_request["system"])
        self.assertIn("Do not use pronouns", entity_request["system"])
        entity_session = next(row for row in entity_request["input"]["sessions"] if row["id"] == "alex-s1")
        self.assertEqual(entity_session["messages"], ["repaired 1a", "original 1b"])
        self.assertNotIn("original 1a", json.dumps(entity_request))

        self.write_json(
            run / "accepted" / "03_entity_inventory.json",
            {"entities": [{"id": "alex", "name": "Alex", "kind": "person", "aliases": [], "introduced_in": "alex-s1", "reason": "user"}]},
        )
        result = subject.advance(persona=self.persona, run=run)
        self.assertEqual(result["status"], "request_written")
        subjects_request = self.read(run / "requests" / "04_existing_fact_subjects.json")
        self.assertEqual(subjects_request["stage"], "existing_fact_subjects")
        self.assertIn("state, preference, ownership, responsibility", subjects_request["system"])
        self.assertIn("Do not select an entity merely because", subjects_request["system"])
        self.assertIn("smallest complete set", subjects_request["system"])
        self.assertIn("mentioned only as a time boundary", subjects_request["system"])
        referenced_s1 = next(row for row in subjects_request["input"]["referenced_sessions"] if row["id"] == "alex-s1")
        self.assertEqual(referenced_s1["messages"][0], "repaired 1a")
        self.assertNotIn("original 1a", json.dumps(subjects_request))

        self.write_json(
            run / "accepted" / "04_existing_fact_subjects.json",
            {"fact_subjects": [{"fact_id": 1, "subjects": ["alex"]}, {"fact_id": 2, "subjects": ["alex"]}]},
        )
        result = subject.advance(persona=self.persona, run=run)
        self.assertEqual(result["status"], "request_written")
        missing_facts = self.read(run / "requests" / "05_missing_facts.json")
        self.assertEqual(missing_facts["stage"], "missing_facts")
        self.assertIn("Use only names, quantities, technical terms, and relationships stated", missing_facts["system"])
        self.assertIn("Do not add a cause merely because one event happened after another", missing_facts["system"])
        self.assertIn("return only the information that is still missing", missing_facts["system"])
        self.assertIn("A true dated event or past state remains historically true", missing_facts["system"])
        self.assertIn("Do not combine a lasting fact with a temporary emotion", missing_facts["system"])
        self.assertEqual(
            sorted(missing_facts["input"]),
            ["accepted_entities", "protected_existing_facts", "sessions"],
        )
        self.assertEqual(
            [row["id"] for row in missing_facts["input"]["sessions"]],
            [row["id"] for row in self.sessions],
        )
        current_s1 = next(row for row in missing_facts["input"]["sessions"] if row["id"] == "alex-s1")
        self.assertEqual(current_s1["messages"][0], "repaired 1a")
        self.assertNotIn("original 1a", json.dumps(missing_facts))

        self.write_json(
            run / "accepted" / "05_missing_facts.json",
            {"facts": [
                {"id": 3, "statement": "New earlier fact", "applies_when": "until changed", "source_session_ids": ["alex-s1"], "related_history_session_ids": [], "subjects": ["alex"], "supersedes": []},
                {"id": 4, "statement": "New later fact", "applies_when": "always", "source_session_ids": ["alex-s5"], "related_history_session_ids": ["alex-s1"], "subjects": ["alex"], "supersedes": [3]},
            ]},
        )
        result = subject.advance(persona=self.persona, run=run)
        self.assertEqual(result["status"], "request_written")
        app_effects = self.read(run / "requests" / "06_identify_app_effects.json")
        self.assertEqual(app_effects["stage"], "identify_app_effects")
        self.assertIn("Return one message_effects row for every supplied session/message pair", app_effects["system"])
        self.assertIn("Do not invent app records, record IDs, record fields, tool arguments", app_effects["system"])
        effect_schema = app_effects["response_schema"]["properties"]["message_effects"]["items"]["properties"]["effects"]["items"]
        self.assertIn("tool", effect_schema["required"])
        self.assertIn(None, effect_schema["properties"]["tool"]["enum"])
        self.assertEqual(
            [tool["name"] for tool in app_effects["input"]["supplied_tools"]],
            ["list_calendar_events", "send_email", "get_pr"],
        )
        get_pr = app_effects["input"]["supplied_tools"][2]
        self.assertEqual(get_pr["contract"], {})
        self.assertEqual(get_pr["arguments"], {"pr_id": {"required": True, "default": None, "annotation": "str"}})
        self.assertEqual(
            [fact["id"] for fact in app_effects["input"]["accepted_facts"]],
            [1, 2, 3, 4],
        )
        self.assertEqual(app_effects["input"]["accepted_facts"][0]["subjects"], ["alex"])

    def test_accepted_app_effects_requires_full_coverage_exact_evidence_and_manifest_tools(self) -> None:
        run = self.advance_to_app_effects()
        cases = [
            (
                "missing message",
                lambda rows: rows.pop(),
                "every repaired session/message pair exactly once",
            ),
            (
                "changed quote",
                lambda rows: rows[0]["effects"][0].update(evidence_quote="not in repaired text"),
                "exact evidence quote",
            ),
            (
                "missing action tool",
                lambda rows: rows[0]["effects"][0].pop("tool"),
                "missing required response fields: \\['tool'\\]",
            ),
            (
                "unsupported names tool",
                lambda rows: rows[0].update(effects=[{
                    "category": "unsupported_request",
                    "evidence_quote": "repaired 1a",
                    "reason": "The requested capability is unavailable.",
                    "tool": "send_email",
                }]),
                "unsupported_request must not name a tool",
            ),
        ]
        for label, mutate, message in cases:
            with self.subTest(label=label):
                rows = self.valid_app_effect_rows()
                mutate(rows)
                self.write_json(run / "accepted" / "06_identify_app_effects.json", {"message_effects": rows})
                with self.assertRaisesRegex(ValueError, message):
                    subject.advance(persona=self.persona, run=run)

    def test_valid_app_effects_finish_without_record_synthesis_or_replay(self) -> None:
        run = self.advance_to_app_effects()
        self.write_json(run / "accepted" / "06_identify_app_effects.json", {"message_effects": self.valid_app_effect_rows()})
        self.assertEqual(subject.advance(persona=self.persona, run=run), {"status": "app_effects_complete"})
        self.assertEqual(
            sorted(path.name for path in (run / "requests").iterdir()),
            [
                "01_find_message_problems.json",
                "02_rewrite_messages.json",
                "03_entity_inventory.json",
                "04_existing_fact_subjects.json",
                "05_missing_facts.json",
                "06_identify_app_effects.json",
            ],
        )

    def test_changed_source_hash_is_rejected(self) -> None:
        run = self.prepare_run()
        sheet = self.source_path("persona_sheet.md")
        sheet.write_text(sheet.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source files changed"):
            subject.advance(persona=self.persona, run=run)

    def test_request_decisions_require_exact_evidence_and_supported_tools(self) -> None:
        input_path = self.root / "decision-input.txt"
        input_path.write_text("accepted input", encoding="utf-8")
        decisions_path = self.root / "decisions.json"
        self.write_json(decisions_path, {
            "persona": "alex",
            "input_sha256": {
                "decision-input.txt": subject.file_hash(input_path),
            },
            "decision_count": 1,
            "records_to_add_before_replay": [],
            "decisions": [{
                "session_id": "alex-s1",
                "message_index": 0,
                "decision": "supported_action",
                "evidence_quote": "original 1a",
                "reason": "The email tool can perform the complete request.",
                "operations": [{
                    "tool": "send_email",
                    "args": {"to": "nadia@example.com", "subject": "Status", "body": "Ready."},
                }],
            }],
        })
        with patch.object(subject, "ROOT", self.root):
            _, plans = subject._load_request_decisions(
                persona="alex",
                run=self.root / "run",
                path=decisions_path,
                sessions=self.sessions,
            )
            self.assertEqual(plans[0]["app_operations"][0]["tool"], "send_email")

            value = self.read(decisions_path)
            value["decisions"][0]["evidence_quote"] = "text that is not in the message"
            self.write_json(decisions_path, value)
            with self.assertRaisesRegex(ValueError, "quote its message exactly"):
                subject._load_request_decisions(
                    persona="alex",
                    run=self.root / "run",
                    path=decisions_path,
                    sessions=self.sessions,
                )

    def test_preexisting_records_cannot_replace_an_existing_record(self) -> None:
        state = {"calendar": [{"id": "event-one", "title": "Existing"}]}
        with self.assertRaisesRegex(ValueError, "already exists"):
            subject._add_required_records(state, [{
                "state_key": "calendar",
                "record": {"id": "event-one", "title": "Replacement"},
            }])

    def test_no_model_or_network_client_imports_and_finalize_is_disabled(self) -> None:
        source = Path(subject.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue(imported_roots.isdisjoint({"openai", "anthropic", "httpx", "requests", "aiohttp", "boto3", "urllib"}))
        self.assertNotIn("AzureJsonClient", vars(subject))
        self.assertNotIn("cached_client_complete", vars(subject))
        with self.assertRaisesRegex(ValueError, "finalize is disabled"):
            subject.finalize(persona=self.persona, run=self.root / "run")


if __name__ == "__main__":
    unittest.main()
