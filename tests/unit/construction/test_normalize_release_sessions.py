"""Single-message release conversion preserves history, tests, and ingestion links."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from registry.facts import load_facts, load_sessions, source_session_ids_for_test
from construction import normalize_release_sessions as conversion


def example():
    return {
        "persona": "morgan",
        "sessions": [
            {"id": "old-a", "narrative_date": "2023-01-01T09:00:00-08:00",
             "messages": ["  Setup\nwith exact spacing.  ", "The follow-up request."]},
            {"id": "old-b", "narrative_date": "2023-01-01T09:00:00-08:00",
             "messages": ["A later message."], "label": "Original label"},
        ],
        "facts": {1: {"id": 1, "statement": "An exact fact.", "applies_when": "always",
                       "source_session_ids": ["old-a"], "related_history_session_ids": ["old-b"],
                       "supersedes": [], "subjects": []}},
        "tests": [{"id": "001", "test": "Use the fact.", "load_bearing_facts": [1],
                   "mock_state": {}, "grade": {"config": {"assertions": []}}}],
    }


class ConversionTests(unittest.TestCase):
    def test_message_level_evidence_does_not_include_neighboring_messages(self):
        original = example()
        original["source_facts"] = [{"id": 1, "sources": [
            {"session_id": "old-a", "message_index": 1}]}]
        result, mapping = conversion.normalize(original)
        self.assertEqual(result["facts"][1]["source_session_ids"], ["000002"])
        result["facts"][1]["source_session_ids"].insert(0, "000001")
        with self.assertRaisesRegex(ValueError, "evidence coverage"):
            conversion.verify_equivalence(original, result, mapping)

    def test_exact_text_dates_order_and_reference_expansion(self):
        original = example()
        before = copy.deepcopy(original)
        result, mapping = conversion.normalize(original)
        self.assertEqual(original, before)
        self.assertEqual([s["id"] for s in result["sessions"]], ["000001", "000002", "000003"])
        self.assertEqual(result["sessions"][0]["messages"], ["  Setup\nwith exact spacing.  "])
        self.assertEqual(result["sessions"][2]["label"], "Original label")
        self.assertEqual(result["facts"][1]["source_session_ids"], ["000001", "000002"])
        self.assertEqual(result["facts"][1]["related_history_session_ids"], ["000003"])
        self.assertEqual(result["tests"], original["tests"])
        self.assertEqual(mapping[1], {"session_id": "000002", "original_session_id": "old-a",
                                      "original_message_index": 1})

    def test_verifier_rejects_lost_or_changed_evidence(self):
        original = example()
        for change in ("text", "date", "order", "fact", "reference", "test", "map", "coverage"):
            with self.subTest(change=change):
                result, mapping = copy.deepcopy(conversion.normalize(original))
                if change == "text":
                    result["sessions"][0]["messages"][0] = "Rewritten."
                elif change == "date":
                    result["sessions"][0]["narrative_date"] = "2023-01-02"
                elif change == "order":
                    result["sessions"].reverse()
                elif change == "fact":
                    result["facts"][1]["statement"] = "A different fact."
                elif change == "reference":
                    result["facts"][1]["source_session_ids"] = ["000002"]
                elif change == "test":
                    result["tests"][0]["mock_state"] = None
                elif change == "map":
                    mapping[0]["original_message_index"] = 1
                else:
                    result["sessions"].pop()
                with self.assertRaises(ValueError):
                    conversion.verify_equivalence(original, result, mapping)

    def test_rejects_invalid_source_instead_of_dropping_messages(self):
        for messages in ([], [""], [" "], [{"role": "user", "content": "text"}]):
            with self.subTest(messages=messages):
                original = example()
                original["sessions"][0]["messages"] = messages
                with self.assertRaises(ValueError):
                    conversion.normalize(original)
        original = example()
        original["sessions"][1]["id"] = "old-a"
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            conversion.normalize(original)
        original = example()
        original["facts"][1]["source_session_ids"] = ["missing"]
        with self.assertRaisesRegex(ValueError, "unknown source"):
            conversion.normalize(original)

    def test_ingestion_links_keep_latest_record_without_rewriting_it(self):
        _, mapping = conversion.normalize(example())
        records = [
            {"session_id": "old-a", "message_index": 0, "driver_ok": False},
            {"session_id": "old-a", "message_index": 0, "driver_ok": True},
            {"session_id": "old-a", "message_index": 1, "seed_item_id": "old-a:1"},
            {"session_id": "old-b", "message_index": 0},
        ]
        before = copy.deepcopy(records)
        self.assertEqual(conversion.link_ingestion(mapping, records),
                         {"000001": 1, "000002": 2, "000003": 3})
        self.assertEqual(records, before)
        self.assertEqual(conversion.link_ingestion(mapping, records[:1], require_complete=False),
                         {"000001": 0})
        with self.assertRaisesRegex(ValueError, "every source"):
            conversion.link_ingestion(mapping, records[:1])
        records[-1]["session_id"] = "unknown"
        with self.assertRaisesRegex(ValueError, "unknown source"):
            conversion.link_ingestion(mapping, records)
        records[-1]["session_id"] = "old-b"
        records[-1]["seed_item_id"] = "old-a:0"
        with self.assertRaisesRegex(ValueError, "conflicting source"):
            conversion.link_ingestion(mapping, records)

    def test_does_not_replace_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "preserve previous"):
                conversion.prepare(Path(directory))


class ExportTests(unittest.TestCase):
    def make_source(self, root):
        releases = {}
        for persona in conversion.PERSONAS:
            release = example()
            release["persona"] = persona
            checkpoint = root / persona / "accepted_final"
            checkpoint.mkdir(parents=True)
            release["checkpoint"] = checkpoint
            (checkpoint / "life_sim.yaml").write_text(yaml.safe_dump({"sessions": release["sessions"]}))
            (checkpoint / "facts.yaml").write_text(yaml.safe_dump({
                "version": 2, "persona": persona, "facts": list(release["facts"].values())}))
            tests_dir = root / "tests" / persona
            tests_dir.mkdir(parents=True)
            tests = []
            hashes = {}
            for i in range(1, 201):
                test = copy.deepcopy(release["tests"][0])
                test["id"] = f"{i:03d}"
                path = tests_dir / f"{i:03d}.yaml"
                path.write_text(yaml.safe_dump(test))
                hashes[f"{i:03d}"] = conversion.sha256(path)
                tests.append(test)
            release["tests"] = tests
            release["manifest"] = {"checkpoint_identity": f"{persona}-original",
                                   "candidate_sha256": hashes}
            release["metadata"] = {"history_tokens_o200k_base": 20}
            manifest = root / "authoring/release_manifests" / f"{persona}.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps(release["manifest"]))
            releases[persona] = release
        return releases

    def test_shared_state_survives_release_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = self.make_source(root)
            for persona, release in releases.items():
                folder = root / "tests" / persona
                base = folder / "state.json"
                base.write_text(json.dumps({"inbox": [{"id": "preserved"}]}))
                release["tests"][0]["mock_state"] = json.loads(base.read_text())
                compact = {**release["tests"][0], "mock_state": {}, "mock_state_base": {
                    "path": "state.json", "sha256": conversion.sha256(base)}}
                (folder / "001.yaml").write_text(yaml.safe_dump(compact))
                release["manifest"]["published_sha256"] = {
                    p.stem: conversion.sha256(p) for p in folder.glob("*.yaml")}
            with patch.object(conversion, "load_release", side_effect=lambda p, **kw: releases[p]), \
                    patch.object(conversion, "validate_persona"):
                output = root / "converted"
                conversion.prepare(output, root=root)
                converted = conversion.verify(output, root=root)
                for persona in conversion.PERSONAS:
                    self.assertEqual(converted[persona]["tests"], releases[persona]["tests"])
                    self.assertEqual((output / "tests" / persona / "state.json").read_bytes(),
                                     (root / "tests" / persona / "state.json").read_bytes())

    def test_disk_round_trip_and_existing_fact_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = self.make_source(root)
            before = {path: conversion.sha256(path) for path in root.rglob("*") if path.is_file()}
            with patch.object(conversion, "load_release", side_effect=lambda p, **kw: releases[p]), \
                    patch.object(conversion, "validate_persona"):
                output = root / "converted"
                conversion.prepare(output, root=root)
                converted = conversion.verify(output, root=root)
                self.assertEqual(before, {path: conversion.sha256(path) for path in before})
                for persona in conversion.PERSONAS:
                    self.assertEqual(load_sessions(persona, root=output), converted[persona]["sessions"])
                    self.assertEqual(load_facts(persona, root=output), converted[persona]["facts"])
                    self.assertEqual(source_session_ids_for_test(
                        converted[persona]["tests"][0], persona, root=output), ["000001", "000002"])
                path = output / "registry/personas/morgan/life_sim.yaml"
                path.write_text(path.read_text() + "changed: true\n")
                with self.assertRaisesRegex(ValueError, "Converted file changed"):
                    conversion.verify(output, root=root)
                manifest_path = output / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["files"][str(path.relative_to(output))] = conversion.sha256(path)
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, "metadata changed"):
                    conversion.verify(output, root=root)



if __name__ == "__main__":
    unittest.main()
