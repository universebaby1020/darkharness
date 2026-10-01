"""Public index/tree guard. Never prints matched values. No credential reads.

Official credential shapes: dark-factory harness/check.py revision 803560d2a678.
This is an independent guard, not the contest layout checker.
"""
import argparse
from pathlib import PurePosixPath
import re
import subprocess
import sys

SHAPES = {
    "bearer-token": r"(?i)\bbearer\s+(?=[A-Za-z0-9._\-]*\d)[A-Za-z0-9._\-]{20,}",
    "api-key": r"\bsk-[A-Za-z0-9._\-]{16,}",
    "aws-access-key": r"\bAKIA[0-9A-Z]{16}\b",
    "github-token": r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
    "env-assignment": r"(?i)\b[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S+",
    "url-credentials": r'''(?<=://)[^/\s:@"'\\]+:[^/\s@"'\\]+(?=@)''',
    "band-key": r'''(?i)\b(?:band[_-]?(?:agent[_-]?)?key|agent[_-]key)\s*[:=]\s*["']?[A-Za-z0-9._-]{16,}''',
    "private-windows-path": r"(?i)[a-z]:[\\/]+Users[\\/]+[A-Za-z0-9_.-]+[\\/]",
    "private-linux-path": r"/home/[A-Za-z0-9_.-]+/",
}
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
CONFIG_SUFFIXES = {".env", ".json", ".toml", ".ini", ".cfg", ".yml", ".yaml"}


def findings(path, data):
    p = PurePosixPath(path)
    reasons = []
    if any(part.lower() in {".env", ".envrc", "auth.json", "credentials", "runtime-profiles", "state", ".venv", "__pycache__"} or part.lower().startswith(".env.") for part in p.parts) or p.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".pfx", ".pyc"}:
        reasons.append("private-file")
        return reasons  # No credential body is needed to reject a credential path.
    text = data.decode("utf-8", errors="replace")
    for name, expression in SHAPES.items():
        if name == "env-assignment" and p.suffix.lower() not in CONFIG_SUFFIXES:
            continue
        if re.search(expression, text):
            reasons.append(name)
    for match in EMAIL.findall(text):
        domain = match.rsplit("@", 1)[1].lower()
        if not domain.endswith(".invalid") and domain != "users.noreply.github.com":
            reasons.append("personal-email")
            break
    return reasons


def git(*argv):
    return subprocess.check_output(["git", *argv])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", help="scan this exact committed tree; default: entire index")
    args = parser.parse_args()
    if args.revision:
        records = git("ls-tree", "-r", "-z", args.revision).split(b"\0")
    else:
        records = git("ls-files", "--stage", "-z").split(b"\0")
    failures = []
    for record in records:
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        fields = metadata.split()
        mode, oid = fields[0], fields[2] if args.revision else fields[1]
        path = name.decode("utf-8")
        if mode not in {b"100644", b"100755"}:
            reasons = ["nonregular-index-entry"]
        else:
            reasons = findings(path, b"")
            if not reasons:
                reasons = findings(path, git("cat-file", "blob", oid.decode()))
        for reason in reasons:
            failures.append(f"{path}: {reason}")
    # Commit identity gate. Repository-local actor identity is also public data.
    if args.revision:
        author_data = git("show", "-s", "--format=%ae%n%ce", args.revision)
    else:
        author_data = git("var", "GIT_AUTHOR_IDENT") + git("var", "GIT_COMMITTER_IDENT")
    if findings("identity.txt", author_data):
        failures.append("commit identity: personal-email")
    print("\n".join(failures) if failures else "PUBLIC_GUARD_PASS")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
