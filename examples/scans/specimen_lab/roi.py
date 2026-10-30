"""Deterministic, specimen-specific demo crops; originals remain the evidence source.

The pilot's amber body markings locate a generous rectangular ROI. This is a
framing heuristic, not segmentation or a guarantee that every appendage is seen.
Unconfigured specimens and frames without a reliable seed retain the full image.
"""
import json
import math

from PIL import Image

from . import hardware


def load_profile(specimen_id):
    path = hardware.PROJECT / 'config' / 'specimen_roi.json'
    if not path.exists():
        return None
    profile = json.loads(path.read_text())
    return profile if profile['specimen_id'] == specimen_id else None


def locate(image, profile):
    width, height = image.size
    if profile['policy'] != 'pilot-amber-body-context-v1':
        raise ValueError('Unknown ROI policy')
    small = image.convert('RGB')
    small.thumbnail((410, 308))
    sw, sh = small.size
    left, top, right, bottom = profile['search_region_normalized']
    points = []
    for y in range(int(top * sh), min(sh, math.ceil(bottom * sh))):
        for x in range(int(left * sw), min(sw, math.ceil(right * sw))):
            r, g, b = small.getpixel((x, y))
            if 35 < r < 200 and r - b > 20 and g - b > 10 and r >= g * .9 and r < g * 1.9:
                points.append((x, y))
    if len(points) < 20:
        return [0, 0, width, height], {'method': 'full_frame_fallback', 'reason': 'Insufficient body-colour seed'}
    x0 = math.floor(min(x for x, y in points) * width / sw)
    y0 = math.floor(min(y for x, y in points) * height / sh)
    x1 = math.ceil((max(x for x, y in points) + 1) * width / sw)
    y1 = math.ceil((max(y for x, y in points) + 1) * height / sh)
    scale = width / 3280
    padding = math.ceil(profile['padding_px_at_3280_width'] * scale)
    w = min(width, max(x1 - x0 + 2 * padding, math.ceil(profile['minimum_size'][0] * scale)))
    h = min(height, max(y1 - y0 + 2 * padding, math.ceil(profile['minimum_size'][1] * scale)))
    x = min(width - w, max(0, (x0 + x1 - w) // 2))
    y = min(height - h, max(0, (y0 + y1 - h) // 2))
    return [x, y, w, h], {'method': profile['policy'], 'seed_bbox': [x0, y0, x1-x0, y1-y0],
                           'padding_pixels': padding, 'qualification': 'Framing heuristic; check appendage coverage against original'}


def prepare(store, original, profile):
    if not profile:
        return original, {'method': 'full_frame', 'reason': 'No specimen ROI profile configured'}
    with Image.open(store.image_path(original['id'], verify=True)) as image:
        box, detail = locate(image, profile)
    detail.update(original_id=original['id'], crop_box=box)
    if box == [0, 0, original['width'], original['height']]:
        return original, detail
    # Repeated model comparisons use the same crop ID and lossless pixel asset.
    for existing in store.snapshot()['images']:
        if existing['parent_id'] == original['id'] and existing['crop_box'] == box:
            store.image_path(existing['id'], verify=True)
            return existing, detail
    return store.crop(original['id'], box), detail
