"""Official gate4 component delegation and import diagnostics, not qualification."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from darkharness.integration.artifacts import (SecretGuard, diagnose_room, render_mandate,
       snapshot_check, mandate_checks, slug)
from darkharness.integration.launch import validate_config, prepare
from darkharness.integration.mailbox import IntegrationError, digest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = Path(os.environ.get("DH_OFFICIAL_ROOT", str(ROOT / "inputs/handoff-20261001/DarkHarness_Opus55_Codex_Handoff_20261001/official/dark-factory-803560d2a678")))


@unittest.skipUnless((OFFICIAL / "harness/check.py").is_file(), "trusted official checkout unavailable")
class ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = SecretGuard.official(OFFICIAL, sys.executable)

    def test_official_secret_patterns_assembled_at_runtime(self):
        fixtures = ["sk" + "-" + "A" * 18,
                    "AK" + "IA" + "A" * 16,
                    "gh" + "p_" + "B" * 22,
                    "Bearer " + "x" * 22 + "1",
                    "APP_" + "TOKEN" + "=" + "synthetic-value",
                    "https://" + "user" + ":" + "synthetic" + "@example.invalid"]
        for text in fixtures:
            self.assertTrue(self.guard.findings(text))
            with self.assertRaisesRegex(IntegrationError, "OUTBOX_SECRET_BLOCKED"):
                self.guard.require_clean({"tool_result": text})
            self.assertNotEqual(self.guard.redact(text), text)
        self.assertFalse(self.guard.findings("bearer authentication is a protocol description"))

    def test_opaque_known_credential_registered_only_in_memory(self):
        guard = SecretGuard.official(OFFICIAL, sys.executable)
        opaque = "opaque" + "value" + "fixture" + "123456"
        self.assertFalse(guard.findings(opaque))
        guard.register_known(opaque)
        self.assertTrue(guard.findings(opaque))
        with self.assertRaisesRegex(IntegrationError, "OUTBOX_SECRET_BLOCKED"):
            guard.require_clean({"result": opaque})
        self.assertEqual(guard.sanitize({"data": [opaque]}), {"data": ["[REDACTED]"]})
        self.assertNotIn(opaque, guard.source_hash)

    def test_additional_json_credential_pattern(self):
        text = json.dumps({"agent_" + "key": "assembled" + "-fixture-value"})
        self.assertIn("band-credential", self.guard.findings(text))

    def test_generic_mandates_official_toy_and_tablekeeper(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "mandates"
            folder.mkdir()
            for role in ("coordinator", "builder", "reviewer"):
                text = render_mandate("dh " + role, role, "DarkHarness (Band SDK Codex)", "gpt-6-astra", "high")
                raw = text.encode()
                (folder / (slug("dh " + role) + ".md")).write_bytes(raw)
                snapshot_check(text, raw, digest(raw))
            result = mandate_checks(OFFICIAL, td, sys.executable)
            self.assertEqual(result, {"toy": [], "tablekeeper": []})
            # Official vocabulary, not a locally imitated regex.
            p = folder / "dhbuilder.md"
            p.write_text(p.read_text() + "\nexpected" + "_revision\n", encoding="utf-8")
            result = mandate_checks(OFFICIAL, td, sys.executable)
            self.assertEqual(result["toy"], [])
            self.assertTrue(any("gate 4" in v for v in result["tablekeeper"]))

    def test_snapshot_hash_mismatch(self):
        with self.assertRaisesRegex(IntegrationError, "MANDATE_SNAPSHOT_MISMATCH"):
            snapshot_check("actual runtime", b"different submitted", digest(b"different submitted"))

    def test_room_typed_edges_ignore_tool_echo_and_slug_conflict(self):
        room = {"id": "room", "scope": "full", "messages": [
            {"senderId": "a", "senderName": "dh builder", "senderType": "agent", "messageType": "text", "content": "@[[b]] task"},
            {"senderId": "b", "senderName": "dh reviewer", "senderType": "agent", "messageType": "tool_result", "content": "@[[a]] echoed"}]}
        result = diagnose_room(json.dumps(room).encode(), "room", self.guard)
        self.assertEqual(result["edges"], [("a", "b")])
        self.assertEqual(result["roundtrips"], [])
        room["messages"][1]["messageType"] = "text"
        result = diagnose_room(json.dumps(room).encode(), "room", self.guard)
        self.assertEqual(len(result["roundtrips"]), 2)
        room["messages"][1]["senderName"] = "dh-builder"
        self.assertIn("SLUG_COLLISION", diagnose_room(json.dumps(room).encode(), "room", self.guard)["warnings"])

    def test_room_unknown_wrong_scope_shape_secret(self):
        self.assertIn("INVALID_JSON", diagnose_room(b"no", "r", self.guard)["warnings"])
        self.assertIn("INVALID_SHAPE", diagnose_room(b"[]", "r", self.guard)["warnings"])
        room = {"scope": "filtered", "messages": [{"senderId": "a", "senderName": "한글", "senderType": "agent", "content": "sk" + "-" + "Q" * 20}]}
        result = diagnose_room(json.dumps(room).encode(), "r", self.guard)
        for code in ("FULL_SCOPE_NOT_CONFIRMED", "ROOM_ID_UNKNOWN", "EMPTY_SLUG", "SECRET_PATTERN_WARNING"):
            self.assertIn(code, result["warnings"])

    def test_prepare_three_seats_does_not_read_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "result"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
        config = json.loads((SOURCE_ROOT / "darkharness/integration/run.example.json").read_text())
            config["workspace"] = str(repo.resolve())
            config["credentials_path"] = str((root / "nonexistent" / "agents.json").resolve())
            result = validate_config(config)
            self.assertEqual(result["credentials"], "NOT_READ")
            result = prepare(config, OFFICIAL, sys.executable)
            self.assertEqual(len(result["snapshots"]), 3)
            self.assertFalse(Path(config["credentials_path"]).exists())
