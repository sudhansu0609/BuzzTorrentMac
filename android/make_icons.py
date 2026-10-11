"""Draws the TV app's launcher icon and home-screen banner (needs Pillow; run once).
The art matches static/favicon.svg: a yellow rounded square with a dark lightning bolt."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
YELLOW, INK, BG, TEXT = (255, 197, 49), (26, 19, 0), (11, 13, 18), (238, 240, 245)
BOLT = [(17.5, 5), (8, 18), (15, 18), (13.5, 27), (24, 14), (17, 14), (17.5, 5)]   # favicon.svg path, 32x32 grid


def mark(size: int) -> Image.Image:
    s = 4  # supersample for smooth edges
    im = Image.new("RGBA", (size * s, size * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, size * s - 1, size * s - 1], radius=size * s // 4, fill=YELLOW)
    k = size * s / 32
    d.polygon([(x * k, y * k) for x, y in BOLT], fill=INK)
    return im.resize((size, size), Image.LANCZOS)


def font(px: int) -> ImageFont.FreeTypeFont:
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def banner(w: int = 320, h: int = 180) -> Image.Image:
    s = 2
    im = Image.new("RGBA", (w * s, h * s), BG + (255,))
    glow = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([w * s * .6, -h * s * .5, w * s * 1.4, h * s * .8], fill=YELLOW + (46,))
    im.alpha_composite(glow.filter(ImageFilter.GaussianBlur(60 * s)))       # soft warm glow
    d = ImageDraw.Draw(im)
    logo = mark(70 * s)
    im.alpha_composite(logo, (int(22 * s), int((h * s - logo.height) / 2)))
    f = font(int(34 * s))
    d.text((int(104 * s), int(h * s / 2)), "BuzzTorrent", font=f, fill=TEXT, anchor="lm")
    return im.resize((w, h), Image.LANCZOS)


def main() -> None:
    out = HERE / "res" / "drawable"
    out.mkdir(parents=True, exist_ok=True)
    mark(192).save(out / "icon.png")
    xh = HERE / "res" / "drawable-xhdpi"
    xh.mkdir(parents=True, exist_ok=True)
    banner().convert("RGB").save(xh / "banner.png")
    banner().convert("RGB").save(out / "banner.png")
    print("icons written to", HERE / "res")


if __name__ == "__main__":
    main()
