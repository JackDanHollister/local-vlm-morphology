"""Draw selected saved TraitTrail results with their recorded model and status."""
from __future__ import annotations

import hashlib
import argparse
import json
from pathlib import Path
import textwrap

from PIL import Image, ImageDraw, ImageFont
from _figure_fonts import font_path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'examples/other-models'
SOURCE = BASE / 'sources'
OUT = BASE / 'figures'
CARD_W, CARD_H = 1400, 1330
PHOTO_H = 820

SPECS = [
    dict(stem='codex-beetle-condition', group='codex-damage', file='run.json',
         title='Codex · GPT-6 Astra', feature='condition.appendage_damage',
         label='Appendage damage: uncertain', crop=(220, 540, 740, 590),
         credit='Photo: Jack Dan Hollister', colour='#A52770'),
    dict(stem='codex-shell-pattern', group='codex-shell', file='run.json',
         title='Codex · GPT-6 Astra', feature='body.pattern',
         label='Shell pattern: mottled', crop=(150, 360, 1250, 1008),
         credit='Photo: Jack Dan Hollister', colour='#A52770'),
    dict(stem='claude-butterfly-wings', group='butterfly', file='claude.json',
         title='Claude Opus 5.5', feature='wings.visible_count',
         label='Visible wings: 4', crop=(0, 0, 1600, 1066),
         credit='Photo: Lauri Mällo / PlutoF · CC BY 4.0', colour='#007D79'),
    dict(stem='gemini-beetle-condition', group='beetle', file='gemini.json',
         title='Gemini 3.1 Pro Preview', feature='condition.appendage_damage',
         label='Appendage damage: predicted', crop=(250, 240, 650, 600),
         credit='Photo: Jack Dan Hollister', colour='#A52770'),
    dict(stem='kimi-beetle-legs', group='beetle', file='kimi.json',
         title='Moonshot · Kimi K3', feature='legs.visible_count',
         label='Visible legs: 4', crop=(250, 240, 650, 600),
         credit='Photo: Jack Dan Hollister', colour='#235FB1'),
    dict(stem='astra-butterfly-hairs', group='butterfly', file='astra.json',
         title='GPT-6 Astra · API example', feature='body.hairs_visible',
         label='Body hairs: visible', crop=(0, 0, 1600, 1066),
         credit='Photo: Lauri Mällo / PlutoF · CC BY 4.0', colour='#A52770'),
]


def main() -> None:
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT)
    parser.add_argument('--font')
    parser.add_argument('--bold-font')
    args = parser.parse_args()
    output = args.output.resolve()
    OUT = output / 'examples/other-models/figures'
    OUT.mkdir(parents=True, exist_ok=True)
    regular, bold = font_path(args.font), font_path(args.bold_font, bold=True)
    title_font = ImageFont.truetype(bold, 61)
    feature_font = ImageFont.truetype(regular, 53)
    note_font = ImageFont.truetype(regular, 43)
    tag_font = ImageFont.truetype(regular, 46)
    entries = []
    sheet = Image.new('RGB', (2 * CARD_W + 60, 3 * CARD_H + 80), 'white')
    for index, spec in enumerate(SPECS):
        data_path = SOURCE / spec['group'] / spec['file']
        data = json.loads(data_path.read_text(encoding="utf-8"))
        codex = spec['group'].startswith('codex-')
        obs = data['result']['observations'] if codex else data['observation']['observations']
        selected = next(o for o in obs if o['feature_id'] == spec['feature'])
        image_path = SOURCE / spec['group'] / ('image.jpg' if spec['group'] == 'butterfly' else 'image.png')
        original_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if codex:
            record = json.loads((image_path.parent / 'image-record.json').read_text(encoding="utf-8"))
            assert record['sha256'] == original_sha
            assert all(e['image_id'] == record['id'] for e in selected['evidence'])
        else:
            image_nodes = [n['record'] for n in data['graph']['@graph'] if n.get('@type') == 'lab:image']
            assert any(n['sha256'] == original_sha for n in image_nodes)
            assert all(e['image_id'] == data['image_id'] for e in selected['evidence'])
        with Image.open(image_path) as source:
            original = source.convert('RGB')
        cx, cy, cw, ch = spec['crop']
        scale = min((CARD_W - 48) / cw, PHOTO_H / ch)
        size = (round(cw * scale), round(ch * scale))
        photo = original.crop((cx, cy, cx + cw, cy + ch)).resize(size, Image.Resampling.LANCZOS)
        card = Image.new('RGB', (CARD_W, CARD_H), 'white')
        draw = ImageDraw.Draw(card)
        draw.text((24, 12), spec['title'], font=title_font, fill='#182B29')
        draw.text((24, 94), spec['label'], font=feature_font, fill='#182B29')
        ox, oy = (CARD_W - size[0]) // 2, 185 + (PHOTO_H - size[1]) // 2
        card.paste(photo, (ox, oy))
        for number, e in enumerate(selected['evidence'], 1):
            x, y, w, h = e['bbox']
            x, y = (x * original.width - cx) * scale + ox, (y * original.height - cy) * scale + oy
            w, h = w * original.width * scale, h * original.height * scale
            assert x >= ox - 0.01 and y >= oy - 0.01
            assert x + w <= ox + size[0] + 1 and y + h <= oy + size[1] + 1
            draw.rectangle((x, y, x + w, y + h), outline='white', width=10)
            draw.rectangle((x, y, x + w, y + h), outline=spec['colour'], width=5)
            if len(selected['evidence']) > 1:
                draw.text((x + 4, max(185, y - 52)), str(number), font=tag_font,
                          fill=spec['colour'], stroke_width=3, stroke_fill='white')
        note = selected['note']
        lines = textwrap.wrap(note, 65)
        assert len(lines) <= 3
        for n, line in enumerate(lines):
            draw.text((24, 1030 + n * 54), line, font=note_font, fill='#253D35')
        draw.text((24, 1210), 'Saved model prediction · boxes uncorrected · human checking needed', font=note_font, fill='#4C5D56')
        draw.text((24, 1268), spec['credit'], font=note_font, fill='#4C5D56')
        dest = OUT / (spec['stem'] + '.png')
        card.save(dest, dpi=(300, 300), optimize=True)
        sheet.paste(card, ((index % 2) * (CARD_W + 20) + 10, (index // 2) * (CARD_H + 20) + 10))
        entries.append(dict(image=str(dest.relative_to(output)), model_identifier=data['model'],
                            engine=data.get('engine', 'API; see source run metadata'),
                            run_id=data['id'] if codex else data['run_id'],
                            photo=str(image_path.relative_to(ROOT)), photo_sha256=original_sha,
                            source_json=str(data_path.relative_to(ROOT)),
                            source_json_sha256=hashlib.sha256(data_path.read_bytes()).hexdigest(),
                            display_crop_xywh=list(spec['crop']), credit=spec['credit'],
                            licence='https://creativecommons.org/licenses/by/4.0/',
                            observation=selected, human_reviewed=False))
    preview = sheet.copy()
    preview.thumbnail((1600, 2100), Image.Resampling.LANCZOS)
    preview.save(OUT / 'other-models-preview.jpg', quality=95, subsampling=0)
    (OUT / 'figure-provenance.json').write_text(json.dumps(entries, ensure_ascii=False, indent=2) + '\n', encoding="utf-8", newline="")
    print(json.dumps({'other_model_panels': len(entries), 'source_boxes_corrected': 0, 'new_inference_requests': 0}))


if __name__ == '__main__':
    main()
