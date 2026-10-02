"""Main-run helper: export fixed axe to a WSL user-local directory; no npm install."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen
from common import atomic_json, regular_tree

VERSION = '4.10.3'
REGISTRY = 'https://registry.npmjs.org/axe-core'


def export(destination):
    destination = Path(destination).absolute()
    if destination.exists():
        raise ValueError('DESTINATION_MUST_BE_NEW')
    regular_tree(destination.parent)
    metadata_url = REGISTRY + '/' + VERSION
    with urlopen(metadata_url) as response:
        if response.url != metadata_url:
            raise ValueError('UNEXPECTED_METADATA_REDIRECT')
        metadata = json.load(response)
    url = REGISTRY + '/-/axe-core-' + VERSION + '.tgz'
    if metadata['version'] != VERSION or metadata['dist']['tarball'] != url:
        raise ValueError('VERSION_SOURCE_MISMATCH')
    with urlopen(url) as response:
        if response.url != url:
            raise ValueError('UNEXPECTED_TARBALL_REDIRECT')
        data = response.read()
    integrity = 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if metadata['dist']['integrity'] != integrity:
        raise ValueError('NPM_INTEGRITY_MISMATCH')
    destination.mkdir(mode=0o700)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        for name in ('axe.min.js', 'LICENSE', 'package.json'):
            member = archive.getmember('package/' + name)
            if not member.isfile():
                raise ValueError('REGULAR_ASSET_REQUIRED')
            raw = archive.extractfile(member).read()
            (destination / name).write_bytes(raw)
            files[name] = hashlib.sha256(raw).hexdigest()
    package = json.loads((destination / 'package.json').read_text())
    if package['version'] != VERSION or package['license'] != 'MPL-2.0':
        raise ValueError('LICENSE_VERSION_MISMATCH')
    manifest = {'version': VERSION, 'source': url, 'metadata_source': metadata_url,
                'npm_integrity': integrity, 'archive_sha256': hashlib.sha256(data).hexdigest(),
                'license': 'MPL-2.0', 'files': files,
                'path': str(destination / 'axe.min.js'), 'sha256': files['axe.min.js']}
    atomic_json(destination / 'manifest.json', manifest)
    print(json.dumps(manifest, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination')
    args = parser.parse_args()
    export(args.destination)
