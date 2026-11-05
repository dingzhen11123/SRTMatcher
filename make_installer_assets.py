"""Generate installer artwork (NSIS bitmaps + launcher logo) from srtmatcher-logo.png.

Usage: python make_installer_assets.py <output_dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
LOGO = ROOT / "srtmatcher-logo.png"
FONT_DIR = Path(r"C:\Windows\Fonts")

BG_TOP = (12, 18, 27)
BG_BOTTOM = (16, 26, 44)
ACCENT = (90, 82, 232)
ACCENT_2 = (38, 119, 232)
TITLE = (248, 251, 255)
MUTED = (143, 160, 181)
FAINT = (82, 97, 118)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ["msyhbd.ttc", "msyh.ttc"] if bold else ["msyh.ttc"]
    for name in names + ["segoeui.ttf", "arial.ttf"]:
        path = FONT_DIR / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def logo(size: int) -> Image.Image:
    return Image.open(LOGO).convert("RGBA").resize((size, size), Image.LANCZOS)


def vertical_gradient(width: int, height: int, top, bottom) -> Image.Image:
    image = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)
    return image


def glow(width: int, height: int, center, radius: int, color, alpha: int) -> Image.Image:
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx, cy = center
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=(*color, alpha))
    return layer.filter(ImageFilter.GaussianBlur(radius // 2))


def centered_text(draw: ImageDraw.ImageDraw, width: int, y: int, text: str, fnt, fill) -> None:
    box = draw.textbbox((0, 0), text, font=fnt)
    draw.text(((width - (box[2] - box[0])) / 2 - box[0], y), text, font=fnt, fill=fill)


def wizard_bitmap(scale: int = 1) -> Image.Image:
    """Left panel of the welcome/finish pages (MUI2 native size 164x314)."""
    width, height = 164 * scale, 314 * scale
    base = vertical_gradient(width, height, BG_TOP, BG_BOTTOM).convert("RGBA")
    base.alpha_composite(glow(width, height, (int(width * 0.2), int(height * 0.18)), 90 * scale, ACCENT, 90))
    base.alpha_composite(glow(width, height, (int(width * 0.95), int(height * 0.62)), 80 * scale, ACCENT_2, 60))
    mark = logo(76 * scale)
    base.alpha_composite(mark, ((width - mark.width) // 2, 56 * scale))
    draw = ImageDraw.Draw(base)
    centered_text(draw, width, 150 * scale, "字幕多功能工具", font(16 * scale, bold=True), TITLE)
    centered_text(draw, width, 176 * scale, "SRTMatcher", font(10 * scale), MUTED)
    line_y = 212 * scale
    draw.line([(width * 0.3, line_y), (width * 0.7, line_y)], fill=(*FAINT, 255), width=scale)
    small = font(9 * scale)
    for offset, text in enumerate(("语音识别 · 文稿精对齐", "AI 校对 · 说话人识别")):
        centered_text(draw, width, (226 + offset * 18) * scale, text, small, MUTED)
    return base.convert("RGB")


def header_bitmap(scale: int = 1) -> Image.Image:
    """Top-right header image of the inner pages (MUI2 native size 150x57, white header)."""
    width, height = 150 * scale, 57 * scale
    base = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    mark = logo(40 * scale)
    base.alpha_composite(mark, (width - mark.width - 10 * scale, (height - mark.height) // 2))
    return base.convert("RGB")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "build" / "installer-assets")
    out.mkdir(parents=True, exist_ok=True)
    # 2 倍分辨率：微软雅黑让向导对话框变大，缩小显示比放大拉伸清晰。
    wizard_bitmap(scale=2).save(out / "wizard.bmp", "BMP")
    header_bitmap(scale=2).save(out / "header.bmp", "BMP")
    logo(64).save(out / "srtmatcher-logo-64.png", "PNG")
    print(f"installer assets written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
