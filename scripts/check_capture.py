"""Check retained Capture material without its original repository."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


def local_path(relative: str) -> Path:
    parts = PurePosixPath(relative)
    if parts.is_absolute() or '..' in parts.parts or '\\' in relative:
        raise ValueError('Invalid preservation path')
    return ROOT / relative


def main() -> None:
    manifest = json.loads((ROOT / 'archive/capture-manifest.json').read_text())
    if manifest.get('schema') not in {'capture-migration-v1', 'capture-migration-v2', 'capture-migration-v3'}:
        raise ValueError('Unknown Capture manifest format')
    archive = local_path(manifest['archive']['path'])
    if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest['archive']['sha256']:
        raise ValueError('History archive checksum mismatch')
    total = 0
    with ZipFile(archive) as z:
        for entry in manifest['files']:
            if entry['storage'] == 'file':
                raw = local_path(entry['path']).read_bytes()
            elif entry['storage'] == 'zip':
                raw = z.read(entry['member'])
            else:
                raise ValueError('Unknown preservation storage')
            if len(raw) != entry['bytes'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ValueError('Preserved file mismatch: ' + entry['source_path'])
            total += len(raw)
    partial = manifest['schema'] == 'capture-migration-v3'
    expected_bytes = manifest['retained_bytes'] if partial else manifest['source_bytes']
    expected_count = manifest['retained_file_count'] if partial else manifest['source_file_count']
    if total != expected_bytes or len(manifest['files']) != expected_count:
        raise ValueError('Capture manifest totals disagree')
    if partial:
        omitted = manifest['omitted_source_files']
        source_paths = [e['source_path'] for e in manifest['files']] + [e['source_path'] for e in omitted]
        if len(source_paths) != manifest['source_file_count'] or len(set(source_paths)) != len(source_paths):
            raise ValueError('Retained and omitted source records disagree')
    print(f"{expected_count} retained Capture files verified; {total} bytes checked.")
    if manifest.get('privacy_redaction'):
        print('Workstation and device metadata redactions are recorded in the manifest.')


if __name__ == '__main__':
    main()
