"""Preparation/import CLI. validate never reads credentials or connects to Band."""
import argparse
import json
from pathlib import Path
import sys

from .artifacts import SecretGuard, diagnose_room
from .launch import prepare, validate_config
from .mailbox import IntegrationError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "prepare", "diagnose-room"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--official-root")
    parser.add_argument("--official-python", default=sys.executable)
    parser.add_argument("--room-file")
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if args.action == "validate":
            result = validate_config(config)
        elif args.action == "prepare":
            result = prepare(config, args.official_root, args.official_python)
        else:
            guard = SecretGuard.official(args.official_root, args.official_python)
            result = diagnose_room(Path(args.room_file).read_bytes(), config["room_id"], guard)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (IntegrationError, OSError, ValueError, TypeError, KeyError) as exc:
        code = str(exc) if isinstance(exc, IntegrationError) else type(exc).__name__
        print(json.dumps({"ok": False, "reason": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
