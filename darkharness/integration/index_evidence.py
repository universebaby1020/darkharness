"""Semantic readback of preserved SHA1 Git index evidence; no stat-cache trust.

Only cache metadata and optional cache extensions are ignored. Split/sparse index,
intent-to-add, skip-worktree, assume-valid and unmerged stages remain unsafe.
"""
import hashlib
import struct
from .mailbox import IntegrationError


def indexed_entries(raw):
    try:
        if raw[:4] != b'DIRC' or hashlib.sha1(raw[:-20]).digest() != raw[-20:]:
            raise ValueError()
        version, count = struct.unpack('!II', raw[4:12])
        if version not in {2, 3, 4}:
            raise ValueError()
        pos, previous, entries = 12, b'', []
        for _ in range(count):
            start = pos
            mode = struct.unpack('!I', raw[pos + 24:pos + 28])[0]
            blob = raw[pos + 40:pos + 60].hex()
            flags = struct.unpack('!H', raw[pos + 60:pos + 62])[0]
            pos += 62
            extended = 0
            if flags & 0x4000:
                if version == 2:
                    raise ValueError()
                extended = struct.unpack('!H', raw[pos:pos + 2])[0]
                pos += 2
            if flags & 0xb000 or extended:  # assume-valid / stage / extended flags
                raise ValueError()
            if version == 4:
                n = raw[pos] & 127
                while raw[pos] & 128:
                    pos += 1
                    n = ((n + 1) << 7) + (raw[pos] & 127)
                pos += 1
                if n > len(previous):
                    raise ValueError()
                end = raw.index(b'\0', pos)
                path = previous[:len(previous) - n] + raw[pos:end]
                pos = end + 1
            else:
                end = raw.index(b'\0', pos)
                path = raw[pos:end]
                pos = start + ((end + 1 - start + 7) // 8) * 8
                if any(raw[end:pos]):
                    raise ValueError()
            if not path or path.startswith(b'/') or any(p in {b'', b'.', b'..', b'.git'} for p in path.split(b'/')) or mode not in {0o100644, 0o100755} or len(blob) != 40 or blob == '0' * 40:
                raise ValueError()
            if flags & 0xfff != min(len(path), 0xfff) or previous and path <= previous:
                raise ValueError()
            entries.append([path.decode('utf-8'), format(mode, 'o'), blob, 0])
            previous = path
        while pos < len(raw) - 20:
            name, size = raw[pos:pos + 4], struct.unpack('!I', raw[pos + 4:pos + 8])[0]
            if len(name) != 4 or not 65 <= name[0] <= 90:
                raise ValueError()  # mandatory extension, e.g. split index 'link'
            pos += 8 + size
        if pos != len(raw) - 20:
            raise ValueError()
        return entries
    except (ValueError, IndexError, struct.error, UnicodeError):
        raise IntegrationError('CONTINUATION_INDEX_UNSAFE') from None


def tree_entries(broker, tree):
    entries = []
    for record in broker._git('ls-tree', '-rz', '--full-tree', tree).split(b'\0'):
        if record:
            meta, path = record.split(b'\t', 1)
            mode, kind, blob = meta.decode().split()
            if kind != 'blob' or mode not in {'100644', '100755'}:
                raise IntegrationError('CONTINUATION_INDEX_UNSAFE')
            entries.append([path.decode('utf-8'), mode, blob, 0])
    return sorted(entries, key=lambda e: e[0].encode())


def same_source(a, b):
    # Raw hashes remain in both immutable proofs, but do not determine equivalence.
    return {k: v for k, v in a.items() if k != 'index'} == {k: v for k, v in b.items() if k != 'index'}
