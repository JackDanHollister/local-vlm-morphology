"""Check published files and the history ZIP for common credentials and private metadata."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import subprocess
from zipfile import ZipFile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
IMAGES = {'.jpg','.jpeg','.png','.tif','.tiff','.webp'}
PATTERNS = {
    'private key': re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----'),
    'provider credential': re.compile(rb'\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16})\b'),
    'other service credential': re.compile(rb'\b(?:AIza[A-Za-z0-9_-]{35}|glpat-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|hf_[A-Za-z0-9]{30,}|npm_[A-Za-z0-9]{30,})\b'),
    'personal home directory': re.compile(rb'(?:/Users/|/home/)[A-Za-z0-9._-]+/'),
    'private network address': re.compile(rb'\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b'),
    'device MAC address': re.compile(rb'\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b'),
    'credential in URL': re.compile(rb'[?&](?:access_token|api_key|apikey|token|signature|X-Amz-Signature)=[A-Za-z0-9_%+/-]{12,}'),
}


def findings(name: str, raw: bytes) -> list[str]:
    result = []
    suffix = Path(name).suffix.lower()
    for label, pattern in PATTERNS.items():
        if pattern.search(raw):
            result.append(label)
    if suffix in IMAGES:
        image = Image.open(BytesIO(raw))
        exif = image.getexif()
        gps = exif.get_ifd(34853)
        if gps.get(2) or gps.get(4):
            result.append('GPS coordinates')
        private = {42016, 42032, 42033, 42037}
        if any(exif.get(tag) for tag in private) or any(exif.get_ifd(34665).get(tag) for tag in private):
            result.append('camera owner/device identifier')
    return result


def main() -> None:
    names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).decode().strip('\0').split('\0')
    problems = []
    checked = 0
    for name in sorted(set(names)):
        path = ROOT / name
        if not name or not path.is_file():
            continue
        if path.suffix.lower() == '.zip':
            with ZipFile(path) as archive:
                for item in archive.infolist():
                    checked += 1
                    for label in findings(item.filename, archive.read(item)):
                        problems.append(f'{name}:{item.filename}: {label}')
        else:
            checked += 1
            for label in findings(name, path.read_bytes()):
                problems.append(f'{name}: {label}')
    if problems:
        raise SystemExit('\n'.join(problems))
    print(f'{checked} files and archive entries checked; no credential patterns, personal paths, private-network identifiers or photo GPS coordinates found.')


if __name__ == '__main__':
    main()
