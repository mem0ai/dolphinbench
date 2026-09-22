from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from reference import artifacts as bridge
from reference import evaluate as matrix


class ExternalSeedBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.hermes_home = self.root / ".hermes"
        self.profile_name = "dolphinbench-source-builtin-morgan"
        self.profile_dir = self.hermes_home / "profiles" / self.profile_name
        self.checkpoint_dir = self.root / "construction" / "checkpoint"
        self.checkpoint_dir.mkdir(parents=True)
        self.corpus_path = self.checkpoint_dir / "life_sim.yaml"
        self.session_ids, self.source_ids, self.hindsight_ids = self._write_corpus()
        for filename, payload in {
            "facts.yaml": "facts: []\n",
            "entities.yaml": "entities: []\n",
            "app_state.json": {"apps": {}},
            "checkpoint.json": {"identity": "test-checkpoint", "sessions": 2765},
        }.items():
            path = self.checkpoint_dir / filename
            path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        self._write_profile()
        self.tests_dir = self.root / "tests" / "morgan"
        self.tests_dir.mkdir(parents=True)
        for index in range(1, 201):
            (self.tests_dir / f"{index:03d}.yaml").write_text(f"id: {index:03d}\n")
        self.manifest_path, self.state_path, self.result_path = self._write_seed()
        self.hindsight_config_path = self.root / "hindsight-provider.json"
        self.supermemory_config_path = self.root / "supermemory-provider.json"
        self.hindsight_receipt_path = self.root / "hindsight-receipt.json"
        self.supermemory_receipt_path = self.root / "supermemory-receipt.json"
        self._write_receipts_and_configs()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_corpus(self) -> tuple[list[str], list[str], list[str]]:
        sessions = []
        session_ids: list[str] = []
        source_ids: list[str] = []
        hindsight_ids: list[str] = []
        for index in range(1, bridge.EXPECTED_SESSIONS + 1):
            session_id = f"{index:06d}"
            message_count = 2 if index <= 635 else 1
            messages = []
            for message_index in range(message_count):
                messages.append(f"Morgan's source message {session_id}-{message_index}.")
                source_ids.append(bridge._stable_source_id("morgan", session_id, message_index))
                hindsight_ids.append(bridge._stable_hindsight_id("morgan", session_id, message_index))
            sessions.append({"id": session_id, "messages": messages})
            session_ids.append(session_id)
        self.corpus_path.write_text(yaml.safe_dump({"sessions": sessions}, sort_keys=False))
        return session_ids, source_ids, hindsight_ids

    def _write_profile(self) -> None:
        self.profile_dir.mkdir(parents=True)
        config = {
            "model": {"provider": "azure-foundry", "default": "gpt-5.6-luna"},
            "memory": {"memory_enabled": True, "provider": "builtin"},
            "toolsets": ["dolphinbench-apps", "memory", "session_search"],
            "platform_toolsets": {"hermes": ["dolphinbench-apps", "memory", "session_search"]},
            "mcp_servers": {"dolphinbench-apps": {"command": "python", "args": ["server.py"]}},
            "agent": {"max_turns": 60},
        }
        (self.profile_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        (self.profile_dir / "SOUL.md").write_text("Morgan source profile.\n")
        (self.profile_dir / "state.db").write_bytes(b"preserve this state")
        (self.profile_dir / "sessions" / "seed.json").parent.mkdir()
        (self.profile_dir / "sessions" / "seed.json").write_text("{\"session\": \"000001\"}\n")
        # These prove that the bridge excludes credentials and auth locks.
        (self.profile_dir / "auth.json").write_text("{\"access_token\": \"not-for-output\"}\n")
        (self.profile_dir / "auth.lock").write_text("locked\n")

    def _write_seed(self) -> tuple[Path, Path, Path]:
        source_hashes = {
            str(path): bridge._sha256(path)
            for path in [
                self.checkpoint_dir / "life_sim.yaml",
                self.checkpoint_dir / "facts.yaml",
                self.checkpoint_dir / "entities.yaml",
                self.checkpoint_dir / "app_state.json",
                self.checkpoint_dir / "checkpoint.json",
                *sorted(self.tests_dir.glob("*.yaml")),
            ]
        }
        sim_path = self.root / "numeric_life_sim.yaml"
        sim_path.write_text(yaml.safe_dump({
            "sessions": [{"id": session_id} for session_id in self.session_ids],
        }, sort_keys=False))
        result_path = self.root / "builtin-result.json"
        result_path.write_text(json.dumps({
            "provider": "builtin",
            "seed_sessions": [
                {"id": session_id, "label": session_id}
                for session_id in self.session_ids
            ],
            "phase1_ended_at": "2026-08-28T15:59:31+00:00",
        }))
        manifest_path = self.root / "builtin-manifest.json"
        manifest = {
            "manifest_version": 2,
            "phase": "prepared_offline",
            "root": str(self.root),
            "run_id": "source-run",
            "personas": ["morgan"],
            "configurations": ["builtin"],
            "agent": {"model": "gpt-5.6-luna", "context_length": 1050000},
            "judge": {"backend": "azure", "deployment": "gpt-5.4"},
            "pricing": {"model": "gpt-5.6-luna"},
            "launch": {"canonical_runner": "harness/run_simulation.py"},
            "evaluation_scope": {"kind": "final_corpus", "scores_are_publishable": True},
            "persona_inputs": {
                "morgan": {
                    "checkpoint_dir": str(self.checkpoint_dir),
                    "numeric_simulation": str(sim_path),
                    "validation": {
                        "sessions": 2765,
                        "tests": 200,
                        "checkpoint_identity": "test-checkpoint",
                        "metadata": {"declared_sessions": 2765},
                    },
                },
            },
            "jobs": [{
                "id": "builtin-morgan",
                "persona": "morgan",
                "configuration": "builtin",
                "provider": "builtin",
                "agent_runtime": "hermes",
                "profile": self.profile_name,
                "toolsets": ["dolphinbench-apps", "memory", "session_search"],
                "sim_path": str(sim_path),
                "result_path": str(result_path),
            }],
            "hashes": {"files": source_hashes},
        }
        manifest_path.write_text(json.dumps(manifest, indent=2))
        state_path = self.root / "builtin-state.json"
        profile_hashes = bridge._profile_config_hashes(self.profile_dir)
        state_path.write_text(json.dumps({
            "manifest": str(manifest_path.resolve()),
            "profiles": {"builtin-morgan": profile_hashes},
            "phases": [{
                "index": 1,
                "jobs": [{
                    "id": "builtin-morgan",
                    "status": "completed",
                    "result_path": str(result_path),
                }],
            }],
        }, indent=2))
        return manifest_path, state_path, result_path

    def _write_receipts_and_configs(self) -> None:
        bank_id = "dolphinbench-morgan-500k-final-v1"
        self.hindsight_config_path.write_text(json.dumps({
            "provider": "hindsight",
            "identity": {
                "kind": "hindsight",
                "bank_id": bank_id,
                "narrative_timezone": "America/Los_Angeles",
                "agent_runtime": "hermes",
            },
            "config": {
                "mode": "local_external",
                "api_url": "https://hindsight.example.test",
                "budget": "mid",
            },
        }))
        batches = []
        for index in range(0, len(self.hindsight_ids), 20):
            ids = self.hindsight_ids[index : index + 20]
            batches.append({
                "batch_index": len(batches),
                "document_ids": ids,
                "completed_document_ids": ids,
                "status": "completed",
            })
        self.hindsight_receipt_path.write_text(json.dumps({
            "schema_version": 1,
            "hindsight_version": "0.9.2",
            "provider": "hindsight",
            "bank_id": bank_id,
            "persona": "morgan",
            "corpus_path": str(self.corpus_path.resolve()),
            "corpus_sha256": bridge._sha256(self.corpus_path),
            "documents_total": 3400,
            "completed_documents": 3400,
            "status": "completed",
            "batches": batches,
        }))
        container_tag = "dolphinbench-morgan-500k-final-v1"
        self.supermemory_config_path.write_text(json.dumps({
            "provider": "supermemory",
            "identity": {
                "kind": "supermemory",
                "container_tag": container_tag,
                "narrative_timezone": "America/Los_Angeles",
                "agent_runtime": "hermes",
            },
            "config": {"base_url": "https://supermemory.example.test"},
        }))
        source_digest = hashlib.sha256("\n".join(sorted(self.source_ids)).encode()).hexdigest()
        self.supermemory_receipt_path.write_text(json.dumps({
            "receipt_version": 2,
            "provider": "supermemory",
            "persona": "morgan",
            "container_tag": container_tag,
            "corpus": {
                "path": str(self.corpus_path.resolve()),
                "sha256": bridge._sha256(self.corpus_path),
                "source_count": 3400,
                "source_ids_sha256": source_digest,
            },
            "sources": {
                source_id: {"source_id": source_id, "provider_id": f"provider-{index}"}
                for index, source_id in enumerate(self.source_ids)
            },
            "status": "complete",
        }))

    def _run(self, provider: str, output_name: str) -> dict[str, str]:
        return bridge.bridge_external_seed(
            receipt_path=self.hindsight_receipt_path if provider == "hindsight" else self.supermemory_receipt_path,
            seed_manifest_path=self.manifest_path,
            seed_state_path=self.state_path,
            builtin_profile_dir=self.profile_dir,
            provider_config_path=self.hindsight_config_path if provider == "hindsight" else self.supermemory_config_path,
            output_dir=self.root / output_name,
            hermes_home=self.hermes_home,
        )

    def test_hindsight_profile_and_matrix_artifacts(self) -> None:
        result = self._run("hindsight", "hindsight-bridge")
        output_profile = Path(result["profile"])
        matrix_profile = Path(result["matrix_profile"])
        source_config = yaml.safe_load((self.profile_dir / "config.yaml").read_text())
        output_config = yaml.safe_load((output_profile / "config.yaml").read_text())
        expected_config = copy.deepcopy(source_config)
        expected_config["memory"]["provider"] = "hindsight"
        self.assertEqual(output_config, expected_config)
        self.assertEqual(
            json.loads((output_profile / "hindsight/config.json").read_text())["bank_id"],
            "dolphinbench-morgan-500k-final-v1",
        )
        self.assertTrue((output_profile / "state.db").read_bytes() == b"preserve this state")
        self.assertFalse((output_profile / "auth.json").exists())
        self.assertFalse((output_profile / "auth.lock").exists())
        self.assertEqual(
            bridge._profile_config_hashes(output_profile),
            bridge._profile_config_hashes(matrix_profile),
        )
        manifest = json.loads(Path(result["manifest"]).read_text())
        state = json.loads(Path(result["state"]).read_text())
        job = manifest["jobs"][0]
        self.assertEqual(job["id"], "hindsight-morgan")
        self.assertEqual(job["profile"], result["profile_name"])
        self.assertEqual(job["seed_receipt_path"], str(self.hindsight_receipt_path.resolve()))
        self.assertEqual(job["seed_receipt_sha256"], bridge._sha256(self.hindsight_receipt_path))
        self.assertEqual(state["manifest"], str(Path(result["manifest"]).resolve()))
        self.assertEqual(state["profiles"]["hindsight-morgan"], bridge._profile_config_hashes(output_profile))
        self.assertEqual(json.loads(Path(result["result"]).read_text())["external_ingestion"]["provider"], "hindsight")

        with patch.object(matrix, "HERMES_HOME", self.hermes_home):
            seed_manifest, seed_state = matrix._seed_source_state(Path(result["manifest"]), Path(result["state"]))
            self.assertTrue(matrix._seed_is_complete_for_job(seed_state, seed_manifest["jobs"][0]))

    def test_old_test_files_can_change_after_seeding(self) -> None:
        # Test files are evaluated later. They are not part of a completed
        # external-memory ingestion or its source seed.
        (self.tests_dir / "003.yaml").write_text("id: 003\nchanged after seeding\n")
        (self.tests_dir / "200.yaml").write_text("id: 200\nchanged after seeding\n")

        result = self._run("hindsight", "test-files-changed-bridge")
        manifest = json.loads(Path(result["manifest"]).read_text())
        bridge_metadata = manifest["bridge"]
        file_hashes = manifest["hashes"]["files"]
        self.assertTrue(Path(result["manifest"]).is_file())
        self.assertTrue(Path(result["state"]).is_file())
        self.assertEqual(bridge_metadata["expected_messages"], 3400)
        self.assertNotIn("expected_tests", bridge_metadata)
        self.assertNotIn("test_directory_sha256", bridge_metadata)
        self.assertNotIn("tests", manifest["persona_inputs"]["morgan"]["validation"])
        self.assertFalse(any("/tests/" in str(path) for path in file_hashes))

    def test_supermemory_receipt_is_accepted(self) -> None:
        result = self._run("supermemory", "supermemory-bridge")
        profile = Path(result["profile"])
        config = yaml.safe_load((profile / "config.yaml").read_text())
        self.assertEqual(config["memory"]["provider"], "supermemory")
        self.assertTrue((profile / "supermemory.json").is_file())
        self.assertFalse((profile / "hindsight").exists())
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(manifest["jobs"][0]["provider_identity"]["kind"], "supermemory")
        self.assertEqual(manifest["bridge"]["receipt_document_count"], 3400)

    def test_rejects_pending_receipt_and_credentials(self) -> None:
        pending = json.loads(self.hindsight_receipt_path.read_text())
        pending["batches"][0]["status"] = "pending"
        pending_path = self.root / "pending.json"
        pending_path.write_text(json.dumps(pending))
        with self.assertRaises(bridge.BridgeError):
            bridge.bridge_external_seed(
                receipt_path=pending_path,
                seed_manifest_path=self.manifest_path,
                seed_state_path=self.state_path,
                builtin_profile_dir=self.profile_dir,
                provider_config_path=self.hindsight_config_path,
                output_dir=self.root / "pending-output",
                hermes_home=self.hermes_home,
            )

        credentials = json.loads(self.hindsight_config_path.read_text())
        credentials["config"]["api_key"] = "must-not-be-accepted"
        credential_path = self.root / "credential-config.json"
        credential_path.write_text(json.dumps(credentials))
        with self.assertRaises(bridge.BridgeError):
            bridge.bridge_external_seed(
                receipt_path=self.hindsight_receipt_path,
                seed_manifest_path=self.manifest_path,
                seed_state_path=self.state_path,
                builtin_profile_dir=self.profile_dir,
                provider_config_path=credential_path,
                output_dir=self.root / "credential-output",
                hermes_home=self.hermes_home,
            )

    def test_rejects_mismatched_corpus_receipt(self) -> None:
        receipt = json.loads(self.supermemory_receipt_path.read_text())
        receipt["corpus"]["source_count"] = 3399
        bad_path = self.root / "bad-count.json"
        bad_path.write_text(json.dumps(receipt))
        with self.assertRaises(bridge.BridgeError):
            bridge.bridge_external_seed(
                receipt_path=bad_path,
                seed_manifest_path=self.manifest_path,
                seed_state_path=self.state_path,
                builtin_profile_dir=self.profile_dir,
                provider_config_path=self.supermemory_config_path,
                output_dir=self.root / "bad-count-output",
                hermes_home=self.hermes_home,
            )


if __name__ == "__main__":
    unittest.main()
