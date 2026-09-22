import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import yaml
from harness.dataset import load_test, spec_sha256

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_data", HERE / "build_data.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = HERE.parent
        cls.index, cls.files = builder.project(cls.root)

    def test_complete_release_and_self_contained_exports(self):
        self.assertEqual(self.index["tests"], 600)
        self.assertEqual(len(self.index["personas"]), 3)
        total = 0
        for persona in builder.PERSONAS:
            base = self.root / "registry/personas" / persona
            history = json.loads(self.files[f"{persona}-history.json"])
            original = yaml.safe_load((base / "life_sim.yaml").read_bytes())["sessions"]
            self.assertEqual([(row["id"], row["date"], row["content"]) for row in history],
                             [(str(row["id"]), row["narrative_date"], row["messages"][0]) for row in original])
            total += len(history)
            data = json.loads(self.files[f"{persona}.json"])
            original_facts = yaml.safe_load((base / "facts.yaml").read_bytes())["facts"]
            self.assertEqual(len(data["facts"]), len(original_facts))
            for fact in original_facts:
                self.assertEqual(data["facts"][str(fact["id"])]["statement"], fact["statement"])
                for field in ("source_session_ids", "related_history_session_ids"):
                    self.assertEqual(data["facts"][str(fact["id"])][field],
                                     [str(value) for value in fact.get(field, [])])
            messages = {row["id"]: row for row in history}
            evidence = json.loads((self.root / "authoring/release_manifests" / f"{persona}.json").read_text())
            for test in data["tests"]:
                relative = f"tests/{persona}/{test['id']}.yaml"
                raw = (self.root / relative).read_bytes()
                source = load_test(self.root / relative)
                exported = yaml.load(self.files[relative], Loader=yaml.CSafeLoader)
                self.assertEqual(exported, source)
                self.assertEqual(spec_sha256(exported), evidence["evaluated_spec_sha256"][test["id"]])
                self.assertNotIn("mock_state_base", exported)
                self.assertEqual(test["sha256"], builder.sha(self.files[relative]))
                self.assertEqual(test["request"], source["test"])
                self.assertEqual(test["grade"], source["grade"])
                self.assertNotIn("app_state", test)
                self.assertEqual(json.loads(self.files[f"tests/{persona}/{test['id']}-state.json"]), source.get("mock_state", {}))
                self.assertEqual(test["fact_ids"], source["load_bearing_facts"])
                self.assertEqual(test["tools"], source.get("expected_tool_calls", []))
                for fact_id in test["fact_ids"]:
                    fact = data["facts"][str(fact_id)]
                    for message_id in fact["source_session_ids"]:
                        self.assertIn(test["id"], messages[message_id]["source_test_ids"])
                    for message_id in fact["related_history_session_ids"]:
                        self.assertIn(test["id"], messages[message_id]["source_test_ids"] +
                                      messages[message_id]["context_test_ids"])
        self.assertEqual(total, self.index["messages"])

    def test_deterministic(self):
        self.assertEqual(builder.project(self.root), (self.index, self.files))


class InvalidReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {"format": "single_message_release", "files": {}, "sources": {}}
        self.write("registry/personas.yaml", {"personas": [
            {"id": persona, "name": persona.title()} for persona in builder.PERSONAS]}, verified=False)
        self.fact = {"id": 1, "statement": "Required value", "applies_when": "Always",
                     "source_session_ids": ["000001"], "related_history_session_ids": []}
        for persona in builder.PERSONAS:
            self.manifest["sources"][persona] = {
                "sessions": 1, "user_message_tokens": 2, "checkpoint_identity": "a" * 64}
            self.write(f"registry/personas/{persona}/life_sim.yaml", {"sessions": [
                {"id": "000001", "narrative_date": "2025-01-01T23:30:00-08:00", "messages": ["Exact original text"]}]})
            self.write(f"registry/personas/{persona}/facts.yaml", {"facts": [self.fact]})
            for i in range(1, 201):
                self.write(f"tests/{persona}/{i:03d}.yaml", {
                    "id": f"{i:03d}", "narrative_anchor_date": "2025-02-01", "test": "Send it.",
                    "load_bearing_facts": [1], "expected_tool_calls": ["send"],
                    "grade": {"type": "assertions", "config": {"assertions": [
                        {"type": "tool_used", "tool": "send", "extra": {"preserved": True}}]}},
                    "mock_state": {"messages": []}})
        self.save_manifest()

    def write(self, relative, value, verified=True):
        raw = json.dumps(value).encode()
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        if verified:
            self.manifest["files"][relative] = builder.sha(raw)

    def save_manifest(self):
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))

    def test_small_valid_fixture(self):
        index, files = builder.project(self.root)
        self.assertEqual((index["tests"], index["messages"]), (600, 3))
        self.assertIn(b'2025-01-01T23:30:00-08:00', files["morgan-history.json"])

    def test_shared_state_must_be_bound_and_match_its_hash(self):
        relative = "tests/morgan/001.yaml"
        test = json.loads((self.root / relative).read_text())
        self.write("tests/morgan/state.json", {"inbox": ["original"]})
        test["mock_state_base"] = {"path": "state.json",
            "sha256": self.manifest["files"]["tests/morgan/state.json"]}
        self.write(relative, test)
        self.save_manifest()
        _, files = builder.project(self.root)
        self.assertEqual(json.loads(files["tests/morgan/001-state.json"]),
                         {"inbox": ["original"], "messages": []})
        self.write("tests/morgan/state.json", {"inbox": ["changed"]})
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "Shared app state hash mismatch"):
            builder.project(self.root)
        del self.manifest["files"]["tests/morgan/state.json"]
        self.save_manifest()
        with self.assertRaises(KeyError):
            builder.project(self.root)

    def test_hash_mismatch_writes_nothing(self):
        (self.root / "tests/morgan/001.yaml").write_text("changed")
        out = self.root / "output"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            builder.build(self.root, out)
        self.assertFalse(out.exists())

    def test_dangling_source_fails(self):
        fact = copy.deepcopy(self.fact)
        fact["source_session_ids"] = ["999999"]
        self.write("registry/personas/morgan/facts.yaml", {"facts": [fact]})
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "unresolved source_session_ids"):
            builder.project(self.root)

    def test_dangling_related_context_fails(self):
        fact = copy.deepcopy(self.fact)
        fact["related_history_session_ids"] = ["999999"]
        self.write("registry/personas/morgan/facts.yaml", {"facts": [fact]})
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "unresolved related_history_session_ids"):
            builder.project(self.root)

    def test_unknown_fact_fails(self):
        relative = "tests/morgan/001.yaml"
        test = json.loads((self.root / relative).read_bytes())
        test["load_bearing_facts"] = [99]
        self.write(relative, test)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "unknown fact"):
            builder.project(self.root)

    def test_missing_test_fails(self):
        (self.root / "tests/morgan/200.yaml").unlink()
        del self.manifest["files"]["tests/morgan/200.yaml"]
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "exactly 200"):
            builder.project(self.root)


if __name__ == "__main__":
    unittest.main()
