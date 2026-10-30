"""Find a usable font without assuming the original Mac workstation."""
from pathlib import Path

from PIL import ImageFont


def font_path(explicit=None, *, bold=False):
    candidates = [explicit] if explicit else [
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        'C:/Windows/Fonts/arialbd.ttf' if bold else 'C:/Windows/Fonts/arial.ttf',
        'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf',
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), 16).path
        except OSError:
            pass
    raise ValueError('No usable font found. Supply --font and --bold-font paths.')
