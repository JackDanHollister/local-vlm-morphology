#!/usr/bin/env python3
"""Open the bundled scan at home without connecting to scanner hardware."""

import argparse
import json
import os
from pathlib import Path

from specimen_lab.bundle import restore_bundle
from specimen_lab.server import serve


def main():
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, help='Separate writable evidence directory; defaults to runs/SCAN_ID')
    parser.add_argument('--scan', default='1', help='Scan number (1 is newest) or full demo ID')
    parser.add_argument('--list', action='store_true', help='List the five included scans, then exit')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--analyse', action='store_true', help='Enable new model runs using saved images only')
    parser.add_argument('--check', action='store_true', help='Restore and verify the bundled evidence, then exit')
    args = parser.parse_args()
    catalog = json.loads((project / 'demo' / 'catalog.json').read_text(encoding='utf-8'))
    if args.list:
        for index, item in enumerate(catalog['scans'], 1):
            print(f"{index}: {item['id']} | completed {item['finished']} | {item['original_count']} photos")
        return
    selected = next((item for index, item in enumerate(catalog['scans'], 1)
                     if args.scan in {str(index), item['id']}), None)
    if selected is None:
        parser.error('Unknown scan; use --list to see included scans')
    if args.analyse and os.name == 'nt':
        parser.error('New model runs require Linux, macOS or WSL; the saved-results viewer works here without --analyse')
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535')
    store = restore_bundle(project / 'demo' / 'scans' / selected['id'],
                           args.store or project / 'runs' / selected['id'])
    if args.check:
        print(json.dumps(store.audit(), indent=2))
        return
    print('Saved-image demo: ' + selected['id'] + '. Scanner access is disabled.', flush=True)
    print('Press Ctrl+C to stop. Your working copy is ' + str(store.root), flush=True)
    try:
        serve(store, args.port, allow_live=False, view_only=not args.analyse)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
