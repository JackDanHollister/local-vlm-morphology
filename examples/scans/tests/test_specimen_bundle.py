import io
import json
from pathlib import Path
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PIL import Image

from specimen_lab.bundle import export_bundle, restore_bundle, verify_bundle
from specimen_lab.contract import FEATURES
from specimen_lab.server import handler
from specimen_lab.store import Store, canonical, write_new


@pytest.fixture
def bundled(tmp_path):
    store = Store(tmp_path / 'source')
    store.specimen('pilot:001', 'Synthetic specimen')
    buf = io.BytesIO()
    Image.new('RGB', (80, 60), 'orange').save(buf, 'PNG')
    original = store.add_image('pilot:001', buf.getvalue())
    crop = store.crop(original['id'], [10, 10, 40, 30])
    run = store.begin_run('pilot:001', [crop['id']], 'synthetic')
    response = dict(summary='Synthetic bundle fixture', next_view_id=None, next_view_reason='Finished',
                    observations=[dict(feature_id=k, status='unknown', value=None, visibility='occluded',
                                       confidence=None, evidence=[], note='Synthetic') for k in FEATURES])
    store.finish_run(run['id'], json.dumps(response))
    job = dict(id='demo_bundle', session_id='bundle', specimen_id='pilot:001', state='complete',
               created='2026-09-11T12:00:00+00:00', finished='2026-09-11T12:01:00+00:00',
               original_ids=[original['id']], image_ids=[crop['id']], run_ids=[run['id']])
    for name in ['terminal.json', 'progress.json']:
        write_new(store.root / 'demos' / job['id'] / name, canonical(job).encode())
    target = tmp_path / 'bundle'
    export_bundle(store, job['id'], target)
    return store, target


def test_roundtrip_preserves_exact_evidence_and_isolates_new_reviews(bundled, tmp_path):
    source, bundle = bundled
    restored = restore_bundle(bundle, tmp_path / 'working')
    assert restored.audit() == source.audit()
    assert restored.graph() == source.graph()
    for item in source.snapshot()['images']:
        assert restored.image(item['id']) == item
        assert restored.image_path(item['id']).read_bytes() == source.image_path(item['id']).read_bytes()
    run = restored.snapshot()['runs'][0]
    restored.review(run['id'], next(iter(FEATURES)), 'needs_review', 'Local review')
    assert len(restore_bundle(bundle, tmp_path / 'working').snapshot()['reviews']) == 1
    assert source.snapshot()['reviews'] == []
    assert json.loads((bundle / 'records.json').read_text())['reviews'] == []


def test_damaged_bundle_rejected_before_working_store_created(bundled, tmp_path):
    _, bundle = bundled
    asset = next((bundle / 'assets').iterdir())
    asset.write_bytes(b'corrupted image')
    with pytest.raises(ValueError, match='checksum'):
        restore_bundle(bundle, tmp_path / 'working')
    assert not (tmp_path / 'working').exists()


def test_export_preserves_adapter_wire_evidence(bundled, tmp_path):
    source, _ = bundled
    run = source.snapshot()['runs'][0]
    folder = source.root / 'runs' / run['id']
    originals = {'wire-response.txt': b'{"bbox":[125,250,625,750]}',
                 'wire-schema.json': b'{"type":"object"}'}
    for name, raw in originals.items():
        write_new(folder / name, raw)
    target = tmp_path / 'with-wire-evidence'
    manifest = export_bundle(source, 'demo_bundle', target)
    restored = restore_bundle(target, tmp_path / 'restored-with-wire')
    for name, raw in originals.items():
        relative = Path('runs') / run['id'] / name
        assert relative.as_posix() in manifest['files']
        assert (restored.root / relative).read_bytes() == raw


def test_path_escape_rejected(bundled):
    _, bundle = bundled
    manifest = json.loads((bundle / 'bundle.json').read_text())
    manifest['files']['../outside'] = '0' * 64
    (bundle / 'bundle.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Invalid bundle path'):
        verify_bundle(bundle)


def test_existing_unrelated_directory_never_overwritten(bundled, tmp_path):
    _, bundle = bundled
    target = tmp_path / 'working'
    target.mkdir()
    (target / 'keep.txt').write_text('Keep this')
    with pytest.raises(ValueError, match='different origin'):
        restore_bundle(bundle, target)
    assert (target / 'keep.txt').read_text() == 'Keep this'


def test_viewer_import_and_serve_need_no_hardware_module(bundled):
    source, _ = bundled
    code = '''
import sys
sys.modules['fcntl'] = None
sys.modules['specimen_lab.hardware'] = None
from unittest.mock import patch
from specimen_lab.server import serve
from specimen_lab.store import Store
with patch('specimen_lab.server.ThreadingHTTPServer') as server:
    serve(Store(sys.argv[1]), view_only=True, allow_live=False)
    server.return_value.__enter__.return_value.serve_forever.assert_called_once()
'''
    result = subprocess.run([sys.executable, '-c', code, str(source.root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_home_http_rejects_live_even_with_available_controller(bundled):
    store, _ = bundled
    class Controller:
        def list(self): return []
        def start(self, **kwargs): raise AssertionError('Must reject before controller access')
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(store, Controller(), allow_live=False))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        settings = json.load(urlopen(base + '/api/demos'))
        assert settings['allow_live'] is False
        payload = dict(specimen_id='pilot:001', provider_id='codex', source='live', mode='tour', max_views=40, attended=True)
        request = Request(base + '/api/demos/start', json.dumps(payload).encode(),
                          {'Content-Type': 'application/json', 'Origin': base})
        with pytest.raises(HTTPError) as error:
            urlopen(request)
        assert error.value.code == 400
        assert 'saved images' in error.value.read().decode()
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_recorded_controller_rejects_live_before_provider_lookup(bundled, monkeypatch):
    from specimen_lab import demo
    store, _ = bundled
    def forbidden(*args): raise AssertionError('No model or hardware access expected')
    monkeypatch.setattr(demo.providers, 'provider', forbidden)
    manager = demo.DemoManager(store, allow_live=False)
    with pytest.raises(ValueError, match='saved images'):
        manager.start('pilot:001', 'codex', source='live', attended=True)
