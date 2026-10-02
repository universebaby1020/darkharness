"""Reproducible, local-only publication preparation from selected UIQA package.

Never opens original design archives, installs assets, or modifies the inputs.
Mapping basis: adopted WO-DH0-02R3 section 2.2 / foreman license-check table.
Directory-level mappings use /* honestly; no sentence-level audit is claimed.
"""
import argparse
import hashlib
import json
import os

if __package__:
    from .public_guard import load_private
else:
    from public_guard import load_private
from pathlib import Path
import shutil

PACK_REL = 'docs/design/ui-design-engineering-clean'
EVIDENCE_REL = 'evidence/wo-dh0-02r3-hygiene'
SOURCES = {
    'emil-design-eng': ('emilkowalski/skills', '85e8e2363b713506e1d5b6e07a0eb2da66be1bc3', 'skills/emil-design-eng/SKILL.md', '2026 Emil Kowalski', 'MIT', 'emil-design-eng-LICENSE'),
    'make-interfaces-feel-better': ('jakubkrehel/make-interfaces-feel-better', '35545ea1512ad59fa463e6b1f95ca9c052981fe6', 'skills/make-interfaces-feel-better/*', '2026 Jakub Krehel', 'MIT', 'make-interfaces-feel-better-LICENSE'),
    'fixing-accessibility': ('ibelick/ui-skills', 'b1cc8e0073ac64b09b3d38cd604407aa20c2b7ad', 'skills/fixing-accessibility/SKILL.md', '2026 Julien Thibeaut', 'MIT', 'fixing-accessibility-LICENSE'),
    'playwright-cli': ('microsoft/playwright-cli', '74354ecc7a43da16d91a9bc54fa8db8283a3fcf5', 'skills/playwright-cli/*', 'Microsoft Corporation (upstream source attribution)', 'Apache-2.0', 'playwright-cli-LICENSE'),
    'shadcn': ('shadcn-ui/ui', '98a1fe67b439324ddc857f47fbdce056600a4329', 'skills/shadcn/*', '2023 shadcn', 'MIT', 'shadcn-LICENSE.md'),
}
EXPECTED = ['57b46f1', '6a067e9', 'cac7c46', 'cefe596', 'fad4d88']
ORIGINALS = 'SKILL.md, governance/*, references/motion.md, references/layout-and-state.md, SOURCES_AND_SCOPE.md'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def substitute(text, pairs):
    """Apply private literal pairs; absent source is a changed input, not success."""
    for pair in pairs:
        if pair['source'] not in text:
            raise ValueError('PUBLICATION_SOURCE_LITERAL_MISSING')
        text = text.replace(pair['source'], pair['replacement'])
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--private-config', type=Path, default=os.environ.get('DH_PUBLIC_PRIVATE_CONFIG'))
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    try:
        private = load_private(args.private_config, repo)
    except (OSError, ValueError, UnicodeError):
        parser.exit(2, 'PUBLICATION_PRIVATE_CONFIG_INVALID: values suppressed\n')
    src = args.package / 'design_pack/ui-design-engineering-clean'
    # Validate the exact substitution inputs before writing any publication file.
    sanitized = {rel: substitute((src / rel).read_text(encoding='utf-8'), pairs)
                 for rel, pairs in private['design_pack_replacements'].items()}
    dest = repo / PACK_REL
    evidence = repo / EVIDENCE_REL
    if dest.exists():
        raise ValueError('DESTINATION_ALREADY_EXISTS')
    mapping = json.loads((src / 'FILE_SOURCE_MAP.json').read_text(encoding='utf-8'))
    original_licenses = {}
    for item, expected in zip(SOURCES.values(), EXPECTED):
        data = (src / 'licenses' / item[-1]).read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        assert blob.startswith(expected)
        assert sha(data) == mapping['license_sha256'][item[-1]]
        original_licenses[item[-1]] = {'bytes': len(data), 'sha256': sha(data), 'git_blob': blob}
    # Verify source map identities and permitted source areas BEFORE deriving hashes.
    assert all(source in SOURCES for sources in mapping['retained_reference_sources'].values() for source in sources)
    inventory = (args.package / 'provenance/INPUT_AND_DISPOSITION.json').read_bytes()
    assert len(json.loads(inventory)['files']) == 147
    shutil.copytree(src, dest)
    # Match published Git text blobs on every host; license bytes stay untouched.
    for p in dest.rglob('*'):
        if p.is_file() and 'licenses' not in p.relative_to(dest).parts:
            p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n'))
    # Before any staging: protect exact license bytes, including CRLF Apache file.
    attrs = repo / '.gitattributes'
    old = attrs.read_text(encoding='utf-8') if attrs.exists() else ''
    rules = [PACK_REL + '/licenses/** -text', EVIDENCE_REL + '/validation-package/design_pack/ui-design-engineering-clean/licenses/** -text']
    attrs.write_text(old + ('' if not old or old.endswith('\n') else '\n') + '\n'.join(r for r in rules if r not in old) + '\n', encoding='utf-8', newline='\n')
    for name, sources in mapping['retained_reference_sources'].items():
        p = dest / 'references' / name
        text = p.read_text(encoding='utf-8')
        paths = '; '.join(SOURCES[s][0] + '/' + SOURCES[s][2] + '@' + SOURCES[s][1] for s in sources)
        extra = ' 명령은 호환성 확인 전 실행하지 않는 예시이며 `<PINNED_VERSION>`은 기존 환경에서 결속할 자리다.' if name == 'shadcn.md' else ''
        header = '> 2026-10-02 수정·선별: ' + paths + '의 한국어 각색. 2026-10-02 편집: 선별본의 권한·검증 경계, 출처 경로와 수정 고지를 정정하고 관련 없는 버전 설명을 제거했다. 원 라이선스와 귀속은 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)를 따른다.' + extra
        p.write_text(header + '\n' + text.split('\n', 1)[1], encoding='utf-8', newline='\n')
    motion = dest / 'references/motion.md'
    motion.write_text('> 2026-10-02 수정·선별: 일반 개념의 보존 출처는 emilkowalski/skills/skills/emil-design-eng/SKILL.md@' + SOURCES['emil-design-eng'][1] + ' (emil-design-eng)이다. 원작 코드·동작 수치·혼합 원문은 제외하고 한국어 지침을 새로 서술했다. 새 문안은 MIT이며 보존 원천의 고지도 유지한다.\n\n' + motion.read_text(encoding='utf-8'), encoding='utf-8', newline='\n')
    mapping['retained_reference_sources']['motion.md'] = ['emil-design-eng']
    mapping['upstream_sources'] = {key: {'repository': 'https://github.com/' + val[0], 'commit': val[1], 'paths': [val[2]], 'copyright': val[3], 'license': val[4], 'license_file': 'licenses/' + val[5], 'mapping_basis': 'adopted WO section 2.2 and supplied foreman table; /* denotes directory-level scope, not exact sentence/file correspondence'} for key, val in SOURCES.items()}
    mapping['original_authored_files_mit'] = ORIGINALS.split(', ')
    mapping['publication_revision'] = '2026-10-02-c7-mit-hygiene'
    (dest / 'FILE_SOURCE_MAP.json').write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    notices = dest / 'THIRD_PARTY_NOTICES.md'
    text = notices.read_text(encoding='utf-8')
    text = sanitized['THIRD_PARTY_NOTICES.md']
    text = text.replace('사용자의 공통규범과 새 작성 문서는 사용자 요청에 따른 작업물이며, 이 파일로 사용자 원문의 저작권자·배포조건을 새로 발명하지 않는다.',
                        'C7=1: SKILL.md, governance/*, references/motion.md, references/layout-and-state.md, SOURCES_AND_SCOPE.md의 원 작성 문안은 이 repo의 MIT LICENSE를 따른다. 보존된 제3자 부분의 라이선스는 바꾸지 않는다.')
    text += '\n## Original authored files — C7=1\n\nThe original authored wording in ' + ORIGINALS + ' follows the repository MIT LICENSE. Retained third-party portions remain under their upstream licenses; the repository MIT grant does not replace them. references/browser-verification.md is an Apache-2.0 derivative with modifications dated 2026-10-02. The supplied pinned-tree inspection found no upstream NOTICE. This is not legal clearance. Upstream paths and commits are in FILE_SOURCE_MAP.json.\n'
    notices.write_text(text, encoding='utf-8', newline='\n')
    scope = dest / 'governance/COMMON_GUIDANCE_SCOPE_KO.md'
    text = sanitized['governance/COMMON_GUIDANCE_SCOPE_KO.md']
    scope.write_text(text, encoding='utf-8', newline='\n')
    root_notice = repo / 'THIRD_PARTY_NOTICES.md'
    text = root_notice.read_text(encoding='utf-8')
    text += '\n## Selected UI design pack\n\nThe text-only pack is at `' + PACK_REL + '/`, outside the Python package. Its local notices and five byte-preserved license files stay in place. Source names are attribution, not endorsement. No images, logos, fonts, npm packages, axe-core assets, or browser binaries are bundled.\n\n| Owner/repository | Pinned commit | Used upstream paths | Copyright/attribution | License | Local license copy |\n|---|---|---|---|---|---|\n'
    for val in SOURCES.values():
        text += '| [ ' + val[0] + ' ](https://github.com/' + val[0] + ') | `' + val[1] + '` | `' + val[2] + '` | ' + val[3] + ' | ' + val[4] + ' | `' + PACK_REL + '/licenses/' + val[5] + '` |\n'
    text += '\n`references/browser-verification.md` is an Apache-2.0 derivative (Korean adaptation and authority, evidence, source and version-notice edits dated 2026-10-02). The supplied 2026-10-02 pinned-tree inspection found no upstream NOTICE in these five sources. Other retained derivative portions keep their respective upstream licenses, not the root MIT grant. FILE_SOURCE_MAP.json records upstream paths at the granularity supported by the adopted work order; directory wildcards are not a sentence-level provenance audit.\n\nC7=1: Original authored wording in `' + ORIGINALS + '` inside the pack follows this repository\'s MIT LICENSE. This grant does not relicense retained third-party portions. Original DarkHarness code follows the root MIT LICENSE. No legal certainty or future asset clearance is claimed.\n'
    root_notice.write_text(text, encoding='utf-8', newline='\n')
    # Evidence replica is the SELECTED supplemental package, not original archives.
    # Replace only its pack with verified publication bytes; preserve 147 input table.
    validation = evidence / 'validation-package'
    shutil.copytree(args.package, validation)
    for p in dest.rglob('*'):
        if p.is_file():
            target = validation / 'design_pack/ui-design-engineering-clean' / p.relative_to(dest)
            target.write_bytes(p.read_bytes())
    assert (validation / 'provenance/INPUT_AND_DISPOSITION.json').read_bytes() == inventory
    # Checker was inspected separately. Manifest refresh follows mapping/license validation.
    import importlib.util
    spec = importlib.util.spec_from_file_location('package_validator', args.package / 'validation/check_package.py')
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    (validation / 'PACKAGE_MANIFEST.json').write_text(json.dumps(validator.make_manifest(validation), ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    report = {'mapping_verified_against': 'adopted WO section 2.2 / supplied foreman table and selected FILE_SOURCE_MAP; no original archives read', 'licenses': original_licenses, 'inventory_sha256': sha(inventory), 'inventory_records': 147, 'publication_pack': PACK_REL, 'legal_clearance': 'NOT_CLAIMED'}
    (evidence / 'design-preparation.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
