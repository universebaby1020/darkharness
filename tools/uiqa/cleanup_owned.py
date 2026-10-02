"""Main-run post-STOP cleanup. Supply an exact effect ID from private effect.json."""
import argparse
from pathlib import Path
import re
from checker import Docker, cleanup
from common import atomic_json

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('effect')
    p.add_argument('new_private_evidence_directory')
    args = p.parse_args()
    if not re.fullmatch('[0-9a-f]{32}', args.effect):
        p.error('exact UIQA effect ID required')
    out = Path(args.new_private_evidence_directory)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    verified = cleanup(Docker(out), args.effect)
    atomic_json(out / 'cleanup.json', {'effect': args.effect, 'verified': verified,
                'prior_execution': 'PARTIAL', 'broker_fence': 'Preserved; use existing preflight_recovery; do not replay'})
    raise SystemExit(0 if verified else 1)
