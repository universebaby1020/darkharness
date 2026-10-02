"""External checker semantics: pinned official output-exists refusal, not Core.

Reviewed cli.py 369-375 returns before save(394), image(402), network(405),
service(406). This recognizes only that exact trusted invocation and diagnostic.
A nonzero code alone NEVER establishes absence of an external effect.
"""
from pathlib import Path
from .mailbox import digest
from .verification import _hash_file

HEAD = '803560d2a678ace1414465c098eb0ab5380ffade'
TREE = '7db03deeb849de4ed517178547bc38e9be77aa08'
CLI_SHA256 = '02027f20196ceb8eeb0e1aaf90a4abe22a6012a95800b949173272959a1513a3'


def official_output_exists(cfg, argv, result, stdout, stderr):
    if (cfg.get('source_head') != HEAD or cfg.get('source_tree') != TREE or
            cfg.get('cwd') != cfg.get('source_root') or
            argv[:5] != [cfg.get('executable'), '-B', '-m', 'harness', 'run'] or
            argv.count('--out') != 1 or result.get('exit_code') != 2 or stdout != b''):
        return None
    output = result.get('output')
    if argv[argv.index('--out') + 1] != output or stderr != (output + ' already exists; use a fresh output directory to preserve evidence\n').encode():
        return None
    cli = Path(cfg['source_root']) / 'harness/cli.py'
    if _hash_file(cli)['sha256'] != CLI_SHA256:
        return None
    return {'recognizer': 'official-output-exists-v1', 'source_head': HEAD,
            'source_tree': TREE, 'cli_sha256': CLI_SHA256,
            'refusal_lines': [369, 375], 'first_report_line': 394,
            'first_build_line': 402, 'external_execution': 'NOT_EXECUTED',
            'stdout_sha256': digest(stdout), 'stderr_sha256': digest(stderr)}
