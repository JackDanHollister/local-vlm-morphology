"""Portable, hash-checked copies of one completed scan and its model evidence."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile

from .store import Store, canonical


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def payload_path(root, relative):
    parts = PurePosixPath(relative)
    if not relative or parts.is_absolute() or '..' in parts.parts or '\\' in relative:
        raise ValueError('Invalid bundle path')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Bundle path escapes its directory')
    return path


def export_bundle(store, job_id, target):
    """Read a terminal job; retain IDs, original bytes and failed responses too."""
    target = Path(target).resolve()
    if target.exists():
        raise ValueError('Bundle destination already exists')
    job = json.loads((store.root / 'demos' / job_id / 'terminal.json').read_text())
    if job['state'] != 'complete':
        raise ValueError('Only a completed scan can be bundled')
    image_ids = set(job['original_ids'] + job['image_ids'])
    snap = store.snapshot()
    images = [i for i in snap['images'] if i['id'] in image_ids]
    runs = [store.run(r) for r in job['run_ids']]
    if len(images) != len(image_ids) or any(r['state'] == 'running' for r in runs):
        raise ValueError('Incomplete scan evidence')
    if any(set(r['image_ids']) - image_ids for r in runs):
        raise ValueError('A run references images outside this scan')
    if any(i['parent_id'] and i['parent_id'] not in image_ids for i in images):
        raise ValueError('Crop parent missing from scan')
    records = dict(
        specimens=[s for s in snap['specimens'] if s['id'] == job['specimen_id']],
        images=images, runs=runs,
        reviews=[r for r in snap['reviews'] if r['run_id'] in job['run_ids']],
    )
    target.mkdir(parents=True)
    (target / 'records.json').write_text(canonical(records) + '\n', encoding='utf-8')
    for item in images:
        source = store.image_path(item['id'], verify=True)
        dest = payload_path(target, item['path'])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    for run in runs:
        dest = target / 'runs' / run['id']
        dest.mkdir(parents=True)
        for name in ('request.json', 'schema.json', 'prompt.txt', 'raw-response.txt', 'terminal.json'):
            shutil.copyfile(store.root / 'runs' / run['id'] / name, dest / name)
        # Coordinate adapters retain the model's original wire representation.
        # Carry it with the converted response so exports remain reviewable.
        optional = ['wire-schema.json', 'wire-response.txt']
        if run['engine'] == 'local-openai-compatible':
            optional.append('execution.json')
        for name in optional:
            source = store.root / 'runs' / run['id'] / name
            if source.is_file():
                shutil.copyfile(source, dest / name)
    shutil.copytree(store.root / 'demos' / job_id, target / 'demos' / job_id)
    for group in ('hardware', 'hardware_sessions'):
        source = store.root / group / job['session_id']
        if source.exists():
            shutil.copytree(source, target / group / job['session_id'])
    # A continued scan can reuse originals captured by an earlier session.
    for action in snap['hardware_actions']:
        if action['terminal'].get('image_id') in image_ids:
            relative = Path('hardware') / action['session_id'] / action['action_id']
            if not (target / relative).exists():
                shutil.copytree(store.root / relative, target / relative)
    files = {p.relative_to(target).as_posix(): sha256(p)
             for p in sorted(target.rglob('*')) if p.is_file()}
    manifest = dict(schema='specimen-scan-bundle-v1', job_id=job_id, captured_at=job.get('acquisition_finished'),
                    finished=job['finished'], original_count=len(job['original_ids']),
                    crop_count=sum(i['role'] == 'crop' for i in images), run_count=len(runs),
                    files=files, payload_bytes=sum((target / name).stat().st_size for name in files))
    (target / 'bundle.json').write_text(canonical(manifest) + '\n', encoding='utf-8')
    return manifest


def verify_bundle(bundle):
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / 'bundle.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 'specimen-scan-bundle-v1':
        raise ValueError('Unknown bundle format')
    for name, expected in manifest['files'].items():
        path = payload_path(bundle, name)
        if not path.is_file() or sha256(path) != expected:
            raise ValueError('Bundle checksum mismatch: ' + name)
    if 'records.json' not in manifest['files']:
        raise ValueError('Bundle is missing its records')
    return manifest


def restore_bundle(bundle, target):
    """Restore into a separate working store; never overwrite existing evidence."""
    bundle, target = Path(bundle).resolve(), Path(target).resolve()
    manifest = verify_bundle(bundle)
    origin = dict(job_id=manifest['job_id'], manifest_sha256=sha256(bundle / 'bundle.json'))
    if target.exists():
        marker = target / 'bundle-origin.json'
        if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')) != origin:
            raise ValueError('Working directory already exists with a different origin; choose another --store')
        store = Store(target)
        audit = store.audit()
        if not audit['ok']:
            raise ValueError('Working evidence failed audit: ' + '; '.join(audit['errors']))
        return store
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.' + target.name + '-', dir=target.parent))
    try:
        for name in manifest['files']:
            dest = payload_path(staging, name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(payload_path(bundle, name), dest)
        records = json.loads((staging / 'records.json').read_text(encoding='utf-8'))
        store = Store(staging)
        with store.db() as db:
            for s in records['specimens']:
                db.execute('INSERT INTO specimens VALUES(?,?,?)', (s['id'], s['label'], s['created']))
            for item in records['images']:
                db.execute('INSERT INTO images VALUES(?,?,?)', (item['id'], item['specimen_id'], canonical(item)))
            for run in records['runs']:
                data = {k: v for k, v in run.items() if k not in {'state', 'result'}}
                db.execute('INSERT INTO runs VALUES(?,?,?,?,?)',
                           (run['id'], run['specimen_id'], run['state'], canonical(data),
                            canonical(run['result']) if run['result'] is not None else None))
            for review in records['reviews']:
                db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?)',
                           tuple(review[k] for k in ('id', 'run_id', 'feature_id', 'verdict', 'note', 'reviewer', 'created')))
        audit = store.audit()
        if not audit['ok']:
            raise ValueError('Restored evidence failed audit: ' + '; '.join(audit['errors']))
        (staging / 'bundle-origin.json').write_text(canonical(origin) + '\n', encoding='utf-8')
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging)
        raise
    return Store(target)
