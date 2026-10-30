"""Assemble publication figures from unchanged photographs and saved Qwen boxes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from _figure_fonts import font_path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT
SOURCES = ROOT / 'examples/qwen/sources'
SINGLES = ROOT / 'examples/qwen/figures'
PAPER = ROOT / 'paper/figures'
MODEL = 'qwen3.8:27b-mtp-q8_0'
WIDTH = 4252
MARGIN = 62
GAP = 28
CELL_W = (WIDTH - 2 * MARGIN - 3 * GAP) // 4
PHOTO_H = 668
HEADER_H = 94
ROW_TITLE_H = 94
CELL_H = HEADER_H + PHOTO_H
ROW_GAP = 68
COLOURS = {'L': '#235FB1', 'A': '#B44F00', 'W': '#007D79', 'C': '#A52770', 'P': '#A52770'}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_observations(folder: str) -> tuple[dict, str]:
    path = SOURCES / folder
    if (path / 'results.json').exists():
        data = json.loads((path / 'results.json').read_text(encoding="utf-8"))
        assert data['model'] == MODEL
        return {o['feature_id']: o for o in data['observation']['observations']}, data['run_id']
    records = json.loads((path / 'annotations.json').read_text(encoding="utf-8"))
    observations = {}
    for item in records['data']:
        for raw in item['attributes']['oa:hasBody']['oa:value']:
            o = json.loads(raw)
            if o['feature_id'] in observations:
                assert observations[o['feature_id']]['evidence'] == o['evidence']
                assert observations[o['feature_id']]['value'] == o['value']
            observations[o['feature_id']] = o
    assert all(o['model'] == MODEL for o in observations.values())
    return observations, observations['wings.present']['run_id']


SPECS = [
    dict(slug='beetle', title='Beetle · scanner photograph',
         expected_sha='6019faed95e229dd4be4e0d6235e526a8a78c51c033b4452932e42073185b623',
         crop=(1040, 670, 610, 830), rotate=90,
         credit='Photo: Jack Dan Hollister',
         source='TraitTrail scanner session demo_6eb6e616b68e45c4; image_127f501143104d29',
         source_url='https://github.com/JackDanHollister/traittrail-capture',
         licence='https://creativecommons.org/licenses/by/4.0/',
         features=[('legs.visible_count', 'Legs: 6', 'L'),
                   ('antennae.visible_count', 'Antennae: 2', 'A'),
                   ('body.colours', 'Body colour: brown', 'C')]),
    dict(slug='spread-butterfly', title='Spread-wing butterfly · Aglais io LM00125',
         expected_sha='612f9d99881a905855e4a5c5a3bb84559d5beca2a6aedd19c841591c0ec90f38',
         crop=(0, 0, 1600, 1066), rotate=0,
         credit='Photo: Lauri Mällo / PlutoF · CC BY 4.0',
         source='GBIF occurrence 2234225450; Aglais io LM00125',
         source_url='https://www.gbif.org/occurrence/2234225450',
         licence='https://creativecommons.org/licenses/by/4.0/',
         features=[('wings.visible_count', 'Wings: 4', 'W'),
                   ('antennae.visible_count', 'Antennae: 2', 'A'),
                   ('body.pattern', 'Pattern: spotted', 'P')]),
    dict(slug='folded-butterfly', title='Folded-wing specimen · ZMA.INS.1533437',
         expected_sha='445df8c679974151e3042ad02b46e8028eb0cda4d78defdbec85872272e437bb',
         crop=(190, 990, 540, 470), rotate=0,
         credit='Photo: Naturalis Biodiversity Center · CC0',
         source='Naturalis ZMA.INS.1533437_2',
         source_url='https://medialib.naturalis.nl/file/id/ZMA.INS.1533437_2/format/large',
         licence='https://creativecommons.org/publicdomain/zero/1.0/legalcode',
         features=[('wings.present', 'Wings: visible', 'W'),
                   ('antennae.visible_count', 'Antennae: 2', 'A'),
                   ('legs.present', 'Legs: visible', 'L')]),
]


def fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    global OUTPUT, SINGLES, PAPER
    parser = argparse.ArgumentParser()
    parser.add_argument('--font')
    parser.add_argument('--bold-font')
    parser.add_argument('--output', type=Path, default=ROOT)
    args = parser.parse_args()
    OUTPUT = args.output.resolve()
    SINGLES = OUTPUT / 'examples/qwen/figures'
    PAPER = OUTPUT / 'paper/figures'
    regular, bold = font_path(args.font), font_path(args.bold_font, bold=True)
    return (ImageFont.truetype(regular, 57), ImageFont.truetype(bold, 62),
            ImageFont.truetype(regular, 46))


def transform_box(box: list, size: tuple, crop: tuple, rotation: int) -> tuple:
    width, height = size
    cx, cy, cw, ch = crop
    x, y, w, h = box
    x, y, w, h = x * width - cx, y * height - cy, w * width, h * height
    if rotation == 90:
        return y, cw - x - w, h, w
    assert rotation == 0
    return x, y, w, h


def main() -> None:
    regular, bold, small = fonts()
    SINGLES.mkdir(parents=True, exist_ok=True)
    PAPER.mkdir(parents=True, exist_ok=True)
    row_h = ROW_TITLE_H + CELL_H
    height = 2 * MARGIN + 3 * row_h + 2 * ROW_GAP + 102
    combined = Image.new('RGB', (WIDTH, height), 'white')
    draw = ImageDraw.Draw(combined)
    manifest = dict(model_identifier_as_recorded=MODEL, human_reviewed=False,
                    inference_requests=0, paper_size_pixels=[WIDTH, height],
                    paper_dpi=600, bbox_format='normalised x, y, width, height in original photograph',
                    transformation='Display crops and a 90-degree anticlockwise beetle rotation only. No box corrections.',
                    specimens=[])
    letter = 0
    for row, spec in enumerate(SPECS):
        folder = SOURCES / spec['slug']
        photo_path = folder / 'image.jpg'
        assert sha(photo_path) == spec['expected_sha']
        observations, run = load_observations(spec['slug'])
        assert len(observations) == 16
        with Image.open(photo_path) as source:
            original = source.convert('RGB')
        cx, cy, cw, ch = spec['crop']
        cropped = original.crop((cx, cy, cx + cw, cy + ch))
        if spec['rotate'] == 90:
            cropped = cropped.transpose(Image.Transpose.ROTATE_90)
        scale = min(CELL_W / cropped.width, PHOTO_H / cropped.height)
        size = (round(cropped.width * scale), round(cropped.height * scale))
        scaled = cropped.resize(size, Image.Resampling.LANCZOS)
        ox, oy = (CELL_W - size[0]) // 2, (PHOTO_H - size[1]) // 2
        row_y = MARGIN + row * (row_h + ROW_GAP)
        draw.text((MARGIN, row_y), spec['title'], font=bold, fill='#182B29')
        row_image = Image.new('RGB', (WIDTH, row_h + 145), 'white')
        row_draw = ImageDraw.Draw(row_image)
        row_draw.text((MARGIN, 4), spec['title'], font=bold, fill='#182B29')
        spec_manifest = dict(specimen=spec['slug'], run_id=run, source=spec['source'],
                             source_url=spec['source_url'], credit=spec['credit'], licence=spec['licence'],
                             source_photo=str(photo_path.relative_to(ROOT)), source_photo_sha256=sha(photo_path),
                             original_size=list(original.size), crop_xywh=list(spec['crop']),
                             display_rotation_anticlockwise_degrees=spec['rotate'],
                             source_files=[dict(path=str(p.relative_to(ROOT)), sha256=sha(p))
                                           for p in sorted(folder.iterdir()) if p.is_file()], panels=[])
        selections = [(None, 'Unlabelled', None)] + spec['features']
        for col, (feature_id, title, prefix) in enumerate(selections):
            code = chr(ord('a') + letter)
            letter += 1
            panel = Image.new('RGB', (CELL_W, CELL_H), 'white')
            pd = ImageDraw.Draw(panel)
            header = f'({code}) {title}'
            assert pd.textlength(header, font=regular) < CELL_W - 8
            pd.text((0, 10), header, font=regular, fill='#182B29')
            panel.paste(scaled, (ox, HEADER_H + oy))
            selected = None
            if feature_id:
                selected = observations[feature_id]
                assert selected['status'] == 'observed'
                tag_bounds = []
                for index, e in enumerate(selected['evidence'], 1):
                    x, y, w, h = transform_box(e['bbox'], original.size, spec['crop'], spec['rotate'])
                    # All selected evidence must remain visible inside the common crop.
                    assert x >= -0.01 and y >= -0.01
                    assert x + w <= cropped.width + 0.01 and y + h <= cropped.height + 0.01
                    x, y, w, h = ox + x * scale, HEADER_H + oy + y * scale, w * scale, h * scale
                    colour = COLOURS[prefix]
                    pd.rectangle((x, y, x + w, y + h), outline='white', width=10)
                    pd.rectangle((x, y, x + w, y + h), outline=colour, width=5)
                    if len(selected['evidence']) > 1:
                        tag = str(index)
                        tw = pd.textlength(tag, font=small) + 10
                        candidates = [(x + 3, y - 53), (x + w - tw, y - 53),
                                      (x + 3, y + h + 3), (x + w - tw, y + h + 3)]
                        for tx, ty in candidates:
                            box = (tx, ty, tx + tw, ty + 55)
                            inside = tx >= 0 and ty >= HEADER_H and box[2] <= CELL_W and box[3] <= CELL_H
                            clear = all(box[2] + 8 <= b[0] or box[0] >= b[2] + 8 or
                                        box[3] + 8 <= b[1] or box[1] >= b[3] + 8 for b in tag_bounds)
                            if inside and clear:
                                break
                        tag_bounds.append(box)
                        pd.text((tx, ty), tag, font=small, fill=colour,
                                stroke_width=3, stroke_fill='white')
            stem = f'{code}_{spec["slug"]}_{feature_id.replace(".", "-") if feature_id else "unlabelled"}'
            single = Image.new('RGB', (CELL_W + 48, CELL_H + 220), 'white')
            single.paste(panel, (24, 8))
            sd = ImageDraw.Draw(single)
            sd.text((24, CELL_H + 44), 'Saved Qwen prediction · unreviewed' if feature_id else 'Unlabelled source photograph · display crop',
                    font=small, fill='#4C5D56')
            credit = spec['credit'].split(' · ')
            sd.text((24, CELL_H + 104), credit[0], font=small, fill='#4C5D56')
            sd.text((24, CELL_H + 162), ' · '.join(credit[1:]), font=small, fill='#4C5D56')
            single_path = SINGLES / (stem + '.png')
            single.save(single_path, dpi=(300, 300), optimize=True)
            x = MARGIN + col * (CELL_W + GAP)
            combined.paste(panel, (x, row_y + ROW_TITLE_H))
            row_image.paste(panel, (x, ROW_TITLE_H))
            spec_manifest['panels'].append(dict(panel=code, title=title, feature_id=feature_id,
                                                observation=selected, image=str(single_path.relative_to(OUTPUT)),
                                                image_sha256=sha(single_path)))
        row_draw.text((MARGIN, row_h + 27), spec['credit'], font=small, fill='#4C5D56')
        row_draw.text((MARGIN, row_h + 82), 'Saved Qwen predictions · same display crop across the four panels · boxes uncorrected', font=small, fill='#4C5D56')
        strip_path = SINGLES / (spec['slug'] + '-four-panels.png')
        row_image.save(strip_path, dpi=(600, 600), optimize=True)
        spec_manifest['four_panel_strip'] = str(strip_path.relative_to(OUTPUT))
        manifest['specimens'].append(spec_manifest)
    assert letter == 12
    draw.text((MARGIN, height - 77), 'Saved Qwen predictions · boxes uncorrected · photographs shown at different magnifications',
              font=small, fill='#4C5D56')
    main_png = PAPER / 'qwen-three-specimens-4x3.png'
    main_tiff = PAPER / 'qwen-three-specimens-4x3.tif'
    combined.save(main_png, dpi=(600, 600), optimize=True)
    combined.save(main_tiff, dpi=(600, 600), compression='tiff_lzw')
    preview = combined.copy()
    preview.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
    preview.save(PAPER / 'qwen-three-specimens-preview.jpg', quality=95, subsampling=0)
    manifest['paper_files'] = [dict(path=str(p.relative_to(OUTPUT)), sha256=sha(p)) for p in [main_png, main_tiff]]
    (SINGLES / 'figure-provenance.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding="utf-8", newline="")
    print(json.dumps({'panels': letter, 'specimens': len(SPECS), 'paper_pixels': [WIDTH, height],
                      'dpi': 600, 'unchanged_sources': 9, 'new_inference': 0}, indent=2))


if __name__ == '__main__':
    main()
