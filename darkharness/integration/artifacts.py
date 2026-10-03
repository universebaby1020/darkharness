"""Official-checker delegation, secret-safe output, mandates and room diagnostics."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from .mailbox import IntegrationError, digest, encode


def official_call(root, code, *args, python="python3"):
    """Call trusted organizer checkout, never vendor checker or track vocabulary."""
    root = Path(root).resolve()
    if not (root / "harness/check.py").is_file():
        raise IntegrationError("OFFICIAL_CHECKER_MISSING")
    proc = subprocess.run([python, "-B", "-c", code, *map(str, args)], cwd=root,
                          capture_output=True, text=True, encoding="utf-8", check=False)
    if proc.returncode:
        # Don't echo arbitrary stderr, which may contain paths or credentials.
        raise IntegrationError("OFFICIAL_CHECKER_ERROR")
    return json.loads(proc.stdout)


class SecretGuard:
    def __init__(self, patterns, *, source_hash):
        if not patterns:
            raise IntegrationError("OFFICIAL_PATTERNS_REQUIRED")
        self.source_hash = source_hash
        self.patterns = [(name, re.compile(pattern)) for name, pattern in patterns]
        self.patterns += [
            ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
            ("band-credential", re.compile(r'(?i)["\']?(?:api_key|agent_key|access_token|refresh_token|password)["\']?\s*[:=]\s*["\']?[^\s"\',}]{8,}')),
        ]

    @classmethod
    def official(cls, root, python="python3"):
        patterns = official_call(root, "import json; from harness.check import SECRETS; print(json.dumps(SECRETS))", python=python)
        return cls(patterns, source_hash=digest((Path(root) / "harness/check.py").read_bytes()))

    def register_known(self, credential):
        """Runtime loader values only; kept in memory, never in a snapshot/log."""
        if not isinstance(credential, str) or not credential:
            raise IntegrationError("EMPTY_RUNTIME_CREDENTIAL")
        self.patterns.append(("known-runtime-credential", re.compile(re.escape(credential))))

    def sanitize(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {self.redact(str(k)): self.sanitize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.sanitize(v) for v in value]
        return value

    def findings(self, value):
        text = value if isinstance(value, str) else encode(value)
        return sorted({name for name, pattern in self.patterns if pattern.search(text)})

    def require_clean(self, value):
        if self.findings(value):
            raise IntegrationError("OUTBOX_SECRET_BLOCKED")

    def redact(self, text):
        for _, pattern in self.patterns:
            text = pattern.sub("[REDACTED]", text)
        return text


def slug(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


RESPONSIBILITIES = {
    "coordinator": "Coordinate assigned work and route complete tasks to the appropriate peers. Once the assignment is accepted, close it with a final report of the actual accepted revision and evidence to its original requester. Do not silently end with band_no_reply or an empty final text.",
    "builder": "Implement assigned work in the scoped repository and repair findings.",
    "reviewer": "Independently review the exact supplied revision in a separate checkout.",
}


def render_mandate(name, role, harness, model, effort):
    if role not in RESPONSIBILITIES or not all(isinstance(x, str) and x.strip() and "\n" not in x for x in (name, harness, model, effort)):
        raise IntegrationError("MANDATE_CONFIG_INVALID")
    return (f"# {name}\nHarness: {harness}\nModel: {model}\nReasoning effort: {effort}\n\n"
            f"## Responsibility\n{RESPONSIBILITIES[role]}\n\n"
            "## Receiving work\nRead the entire assigned task and all evidence before acting. "
            "Do not act on incomplete handoffs. Use only existing scoped authority.\n\n"
            "## Handoff\nAddress peers using typed mentions. Include the full task, context, "
            "revision and evidence. Before sending, verify the outgoing handoff is complete, "
            "with no empty placeholders or literal undefined. For questions, return the current turn and consume "
            "the peer's reply in a continuation; do not wait inside a tool. "
            "Do not ask a human for implementation input: use dh_peer_question to route "
            "the full question to the coordinator and yield. The coordinator answers peers "
            "with the provided continuation reply command. Report out-of-scope requests "
            "as blockers without execution. Final reports to the human are permitted.\n\n"
            "## Acceptance and rejection\nAccept only when requirements and independent checks "
            "support the decision. Factory pins are controller information, not seat verification responsibility; "
            "verify the assigned product revision with the granted checks. Reject with reproducible findings and route repairs. "
            "Do not invent a rejection when review passes.\n\n"
            "## Verification diagnostics\nNOT_STARTED describes absence of a checker intent, not valid input. "
            "same_input_safe_retry describes duplicate-effect safety, not expected success or new authority. "
            "For input_correction_required, correct the input or controller configuration: "
            "RECEIPT_RUN_MISMATCH and CHECK_NOT_GRANTED are expected to recur with unchanged input and authority. "
            "An outstanding effect from another seat is not your own executed checker effect; "
            "retain the run fence and let the controller reconcile its actual owner.\n\n"
            "## Local Git and recovery\nKeep the native sandbox enabled. Use "
            "dh_local_git_commit for existing files you authored or were explicitly assigned "
            "to commit, with the canonical repository cwd and exact current HEAD. The broker "
            "does not author solutions. Use dh_review_snapshot for an independent exact-revision "
            "review checkout in assigned scratch. Do not use shell privilege escalation, push, "
            "amend, merge or rebase. Controller Grants alone authorize these tools. A maintenance "
            "continuation retains the original task and session: consume any provided peer answer "
            "and recovery receipt, and do not repeat already-committed effects.\n\n"
            "## Run completion\nThe coordinator's final report ends the current dispatched task. Other seats "
            "must not reply to that final report unless reporting a concrete defect. "
            "Do not start acknowledgement or confirmation loops.\n\n"
            "## Credential hygiene\nNever echo credential or token values in commands, "
            "outputs or messages: use variables or masking. Never assign secrets in "
            "Dockerfiles or committed configuration.\n\n"
            "## UI quality\nFor applicable UI work, use the provided read-only design pack "
            "and any UI quality helper granted for this run; follow the task specification and existing authority.\n\n"
            "## Evidence reporting\nReport actual commands, results and Git revision. "
            "Distinguish untested work and uncertain effects from success. An outbox DELIVERY_UNKNOWN "
            "between SEND_INTENT and SEND_ACK is normal pre-ACK send progress, not proof of a failed send. "
            "If it remains unresolved, preserve the delivery fence. Never replay "
            "uncertain actions without reconciliation. Keep secrets out of messages.\n")


def snapshot_check(runtime_input, submitted_bytes, expected_hash):
    if digest(runtime_input.encode("utf-8")) != expected_hash or digest(submitted_bytes) != expected_hash:
        raise IntegrationError("MANDATE_SNAPSHOT_MISMATCH")


def mandate_checks(official_root, result_root, python="python3"):
    return official_call(official_root,
                         "import json, pathlib, sys; from harness.check import _mandates; print(json.dumps({t:_mandates(pathlib.Path(sys.argv[1]),t) for t in ('toy','tablekeeper')}))",
                         result_root, python=python)


def diagnose_room(raw, expected_room, guard):
    """Supplement only. No synthetic export and no promotion to official PASS."""
    result = {"level": "IMPORT_DIAGNOSTIC", "official_precedence": True,
              "sha256": digest(raw), "warnings": [], "seats": {}, "edges": [], "roundtrips": []}
    try:
        room = json.loads(raw)
    except (ValueError, UnicodeError):
        result["warnings"].append("INVALID_JSON")
        return result
    if not isinstance(room, dict) or not isinstance(room.get("messages"), list) or not all(isinstance(m, dict) for m in room["messages"]):
        result["warnings"].append("INVALID_SHAPE")
        return result
    if room.get("scope") != "full":
        result["warnings"].append("FULL_SCOPE_NOT_CONFIRMED")
    identity = room.get("roomId") or room.get("id") or room.get("chatRoomId")
    # Schema variations remain unknown instead of inferring a room from its filename.
    if identity is None:
        result["warnings"].append("ROOM_ID_UNKNOWN")
    elif identity != expected_room:
        result["warnings"].append("ROOM_ID_MISMATCH")
    seats = {m["senderId"]: m.get("senderName") or m["senderId"] for m in room["messages"] if m.get("senderId") and str(m.get("senderType", "")).lower() == "agent"}
    result["seats"] = seats
    slugs = [slug(n) for n in seats.values()]
    if "" in slugs:
        result["warnings"].append("EMPTY_SLUG")
    if len(set(slugs)) != len(slugs):
        result["warnings"].append("SLUG_COLLISION")
    edges = {(m["senderId"], target) for m in room["messages"] if m.get("senderId") in seats and m.get("messageType") == "text" and str(m.get("senderType", "")).lower() == "agent" for target in seats if target != m["senderId"] and f"@[[{target}]]" in str(m.get("content", ""))}
    result["edges"] = sorted(edges)
    result["roundtrips"] = sorted((a, b) for a, b in edges if (b, a) in edges)
    result["secret_patterns"] = guard.findings(room)
    if result["secret_patterns"]:
        result["warnings"].append("SECRET_PATTERN_WARNING")
    return result


def git_evidence(workspace):
    def run(args):
        p = subprocess.run(["git", "-C", str(workspace), *args], capture_output=True, check=False)
        return {"exit": p.returncode, "stdout": p.stdout.decode("utf-8", "replace"), "stderr": p.stderr.decode("utf-8", "replace")}
    return {"head": run(["rev-parse", "HEAD"]), "diff": run(["diff", "--no-ext-diff", "--no-textconv"]),
            "status": run(["status", "--porcelain"])}
