import io
import json
from pathlib import Path

from PIL import Image, ImageDraw

from specimen_lab import roi
from specimen_lab.store import Store

PROFILE=json.loads((Path(__file__).resolve().parents[1]/'config/specimen_roi.json').read_text())


def test_body_crop_keeps_dark_appendages_and_exact_original_pixels(tmp_path):
    image=Image.new('RGB',(1640,1232),'white');draw=ImageDraw.Draw(image)
    draw.rectangle((740,500,860,780),fill=(120,85,35))
    # Antenna/legs extend beyond the amber seed: padding must preserve them.
    draw.line((800,500,950,420),fill='black',width=4)
    draw.line((750,650,600,800),fill='black',width=4)
    raw=io.BytesIO();image.save(raw,'PNG')
    s=Store(tmp_path);s.specimen('pilot:001','ROI fixture')
    original=s.add_image('pilot:001',raw.getvalue())
    crop,record=roi.prepare(s,original,PROFILE)
    x,y,w,h=crop['crop_box']
    assert x<600 and x+w>950 and y<420 and y+h>800
    assert w<image.width and h<image.height and crop['parent_id']==original['id']
    with Image.open(s.image_path(crop['id'],verify=True)) as actual:
        assert actual.tobytes()==image.crop((x,y,x+w,y+h)).tobytes()
    again,_=roi.prepare(s,original,PROFILE)
    assert again['id']==crop['id'] and len(s.snapshot()['images'])==2
    assert s.image_path(original['id'],verify=True).read_bytes()==raw.getvalue()
    assert record['method']==PROFILE['policy']


def test_missing_seed_keeps_full_image():
    image=Image.new('RGB',(1640,1232),'white')
    box,record=roi.locate(image,PROFILE)
    assert box==[0,0,1640,1232] and record['method']=='full_frame_fallback'


def test_lower_view_crop_is_clamped_without_losing_seed():
    image=Image.new('RGB',(1640,1232),'white');draw=ImageDraw.Draw(image)
    draw.rectangle((680,980,810,1100),fill=(120,85,35))
    x,y,w,h=roi.locate(image,PROFILE)[0]
    assert 0<=x<680<810<x+w<=1640
    assert 0<=y<980<1100<y+h<=1232
