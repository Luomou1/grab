from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "packaging" / "assets"
ICO_PATH = ASSET_DIR / "grab.ico"
PNG_PATH = ASSET_DIR / "grab_icon_preview.png"


def rounded_rectangle_gradient(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gradient = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pixels = gradient.load()
    top = (38, 54, 65)
    bottom = (15, 20, 27)
    for y in range(size):
        ratio = y / max(size - 1, 1)
        color = tuple(round(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        for x in range(size):
            pixels[x, y] = (*color, 255)

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((12, 12, size - 12, size - 12), radius=44, fill=255)
    image.alpha_composite(gradient)
    image.putalpha(mask)
    return image


def draw_icon(size: int = 256) -> Image.Image:
    scale = size / 256
    image = rounded_rectangle_gradient(size)
    draw = ImageDraw.Draw(image)

    def p(value: float) -> int:
        return round(value * scale)

    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((p(44), p(64), p(214), p(184)), radius=p(22), fill=(0, 0, 0, 105))
    shadow = shadow.filter(ImageFilter.GaussianBlur(p(10)))
    image.alpha_composite(shadow)

    draw.rounded_rectangle((p(38), p(56), p(210), p(176)), radius=p(22), fill=(230, 238, 242, 255))
    draw.rounded_rectangle((p(38), p(56), p(210), p(176)), radius=p(22), outline=(255, 255, 255, 120), width=p(2))
    draw.rounded_rectangle((p(57), p(40), p(125), p(76)), radius=p(14), fill=(22, 214, 208, 255))
    draw.rectangle((p(76), p(76), p(190), p(91)), fill=(204, 217, 224, 255))

    draw.ellipse((p(78), p(88), p(170), p(180)), fill=(18, 25, 34, 255))
    draw.ellipse((p(91), p(101), p(157), p(167)), fill=(21, 118, 199, 255))
    draw.ellipse((p(105), p(115), p(143), p(153)), fill=(31, 222, 218, 255))
    draw.ellipse((p(116), p(126), p(132), p(142)), fill=(236, 252, 252, 255))
    draw.arc((p(92), p(102), p(156), p(166)), start=205, end=330, fill=(153, 223, 255, 190), width=p(5))

    # 扫描线和采集点强调“采集程序”的用途。
    scan_color = (22, 214, 208, 255)
    draw.line((p(36), p(198), p(220), p(198)), fill=scan_color, width=p(6))
    draw.line((p(132), p(76), p(132), p(206)), fill=scan_color, width=p(5))
    for x, height in ((58, 14), (82, 24), (180, 28), (206, 16)):
        draw.rounded_rectangle(
            (p(x), p(198 - height), p(x + 8), p(198 + height)),
            radius=p(4),
            fill=(22, 214, 208, 220),
        )
    draw.ellipse((p(52), p(192), p(64), p(204)), fill=(236, 252, 252, 255))
    draw.ellipse((p(174), p(192), p(186), p(204)), fill=(236, 252, 252, 255))

    return image


def main() -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    image = draw_icon()
    image.save(PNG_PATH)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    image.save(ICO_PATH, sizes=sizes)
    print(ICO_PATH)
    print(PNG_PATH)


if __name__ == "__main__":
    main()
