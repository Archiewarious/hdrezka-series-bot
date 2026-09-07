"""Стартовая картинка бота для BotFather (640×360): веер обложек, свечение, зерно.

Без единого слова — картинка в Telegram одна на все языки, локализуется только текст.
Обложки берутся из каталога: `SELECT poster_url FROM pages WHERE ...` → скачать в posters/p1..p5.jpg.
Запуск: pip install pillow && python tools/welcome_banner.py [каталог_с_постерами] [выходной.png]
"""
import random
from PIL import Image, ImageDraw, ImageFilter, ImageChops

W, H = 640, 360
random.seed(7)

# --- фон: глубокий градиент + размытые цветные пятна (mesh-gradient)
bg = Image.new("RGB", (W, H))
d = ImageDraw.Draw(bg)
for y in range(H):
    k = y / H
    d.line([(0, y), (W, y)], fill=(int(7 + 5 * k), int(10 + 7 * k), int(19 + 11 * k)))

blobs = Image.new("RGBA", (W, H), (0, 0, 0, 0))
b = ImageDraw.Draw(blobs)
for x, y, r, color, a in (
    (110, 310, 200, (50, 80, 235), 78),
    (540, 60, 190, (230, 60, 150), 60),
    (330, 190, 175, (0, 175, 210), 58),
    (610, 340, 160, (140, 75, 245), 52),
):
    b.ellipse([x - r, y - r, x + r, y + r], fill=color + (a,))
bg = Image.alpha_composite(bg.convert("RGBA"), blobs.filter(ImageFilter.GaussianBlur(95)))


def poster(w: int, h: int, path: str) -> Image.Image:
    """Настоящая обложка: вписываем по короткой стороне, скругляем углы, блик и тонкая рамка."""
    src = Image.open(path).convert("RGB")
    k = max(w / src.width, h / src.height)
    src = src.resize((max(w, int(src.width * k)), max(h, int(src.height * k))), Image.LANCZOS)
    left, top_ = (src.width - w) // 2, int((src.height - h) * 0.42)      # чуть выше центра: там лица
    src = src.crop((left, top_, left + w, top_ + h))

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=int(w * 0.11), fill=255)
    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    card.paste(src, (0, 0), mask)

    sheen = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(sheen).polygon([(0, 0), (w, 0), (w, int(h * .28)), (0, int(h * .46))],
                                  fill=(255, 255, 255, 18))
    card = Image.alpha_composite(card, Image.composite(sheen, Image.new("RGBA", (w, h), (0, 0, 0, 0)), mask))
    ImageDraw.Draw(card).rounded_rectangle([0, 0, w - 1, h - 1], radius=int(w * 0.11),
                                           outline=(255, 255, 255, 60), width=1)
    return card


import sys
BASE = sys.argv[1] if len(sys.argv) > 1 else "posters"
POSTERS = [f"{BASE}/p5.jpg",   # Поднятие уровня в одиночку
           f"{BASE}/p2.jpg",   # Дом Дракона
           f"{BASE}/p1.jpg",   # Игра престолов — центр: тёмная и узнаваемая
           f"{BASE}/p4.jpg",   # Ведьмак
           f"{BASE}/p3.jpg"]   # О моём перерождении в слизь [ТВ-4] — светлое пятно на краю
LAYOUT = [(-204, 30, 13, .78), (-108, 10, 6, .88), (0, -6, 0, 1.10), (108, 10, -6, .88), (204, 30, -13, .78)]

glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ImageDraw.Draw(glow).ellipse([320 - 132, 176 - 104, 320 + 132, 176 + 104], fill=(80, 210, 255, 120))
bg = Image.alpha_composite(bg, glow.filter(ImageFilter.GaussianBlur(52)))

BASE_W, BASE_H = 122, 183
order = [0, 4, 1, 3, 2]                        # дальние рисуем первыми
for i in order:
    dx, dy, rot, scale = LAYOUT[i]
    w, h = int(BASE_W * scale), int(BASE_H * scale)
    card = poster(w, h, POSTERS[i])
    if i == 2:                                  # центральная — затемнение под кнопку и сама кнопка
        scrim = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(scrim).ellipse([w // 2 - 62, h // 2 - 62, w // 2 + 62, h // 2 + 62], fill=(0, 0, 0, 95))
        card = Image.alpha_composite(card, scrim.filter(ImageFilter.GaussianBlur(22)))
        pd = ImageDraw.Draw(card)
        mx, my, r = w // 2, h // 2, 26
        halo = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(halo).ellipse([mx - r - 10, my - r - 10, mx + r + 10, my + r + 10], fill=(255, 255, 255, 60))
        card = Image.alpha_composite(card, halo.filter(ImageFilter.GaussianBlur(12)))
        pd = ImageDraw.Draw(card)
        pd.ellipse([mx - r, my - r, mx + r, my + r], fill=(255, 255, 255, 55), outline=(255, 255, 255, 255), width=3)
        pd.polygon([(mx - 8, my - 13), (mx - 8, my + 13), (mx + 14, my)], fill=(255, 255, 255, 255))
    if scale < 0.85:
        card = card.filter(ImageFilter.GaussianBlur(0.8))
    if scale < 1.0:                             # дальние карточки притемняем
        alpha = card.split()[3].point(lambda v: int(v * (0.72 if scale < .85 else 0.9)))
        card.putalpha(alpha)
    rotated = card.rotate(rot, resample=Image.BICUBIC, expand=True)

    shadow = Image.new("RGBA", rotated.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 150), (0, 0), rotated.split()[3])
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    px, py = 320 + dx - rotated.width // 2, 176 + dy - rotated.height // 2
    bg.alpha_composite(shadow, (px + 4, py + 12))
    bg.alpha_composite(rotated, (px, py))

def bell(dr, cx, cy, s_, fill):
    dr.pieslice([cx - s_ * .62, cy - s_ * .95, cx + s_ * .62, cy + s_ * .35], 180, 360, fill=fill)
    dr.rectangle([cx - s_ * .62, cy - s_ * .30, cx + s_ * .62, cy + s_ * .30], fill=fill)
    dr.polygon([(cx - s_ * .62, cy + s_ * .30), (cx + s_ * .62, cy + s_ * .30),
                (cx + s_ * .86, cy + s_ * .52), (cx - s_ * .86, cy + s_ * .52)], fill=fill)
    dr.rounded_rectangle([cx - s_ * .90, cy + s_ * .46, cx + s_ * .90, cy + s_ * .62], radius=s_ * .08, fill=fill)
    dr.ellipse([cx - s_ * .20, cy + s_ * .62, cx + s_ * .20, cy + s_ * 1.02], fill=fill)
    dr.ellipse([cx - s_ * .12, cy - s_ * 1.12, cx + s_ * .12, cy - s_ * .88], fill=fill)


bx, by_ = 388, 84
badge_glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ImageDraw.Draw(badge_glow).ellipse([bx - 44, by_ - 44, bx + 44, by_ + 44], fill=(42, 171, 238, 150))
bg = Image.alpha_composite(bg, badge_glow.filter(ImageFilter.GaussianBlur(26)))
bd = ImageDraw.Draw(bg)
bd.ellipse([bx - 25, by_ - 25, bx + 25, by_ + 25], fill=(255, 255, 255, 250))
bd.ellipse([bx - 21, by_ - 21, bx + 21, by_ + 21], fill=(42, 171, 238, 255))
bell(bd, bx, by_ - 1, 11, (255, 255, 255, 255))

# --- виньетка и зерно
vig = Image.new("L", (W, H), 0)
ImageDraw.Draw(vig).ellipse([-60, -60, W + 60, H + 60], fill=255)
vig = vig.filter(ImageFilter.GaussianBlur(95))
bg = Image.composite(bg, Image.new("RGBA", (W, H), (5, 7, 13, 255)), vig)

out = bg.convert("RGB")
noise = Image.effect_noise((W, H), 7).convert("L").point(lambda v: 128 + (v - 128) // 5)
out = ImageChops.overlay(out, Image.merge("RGB", (noise, noise, noise)))
out.save(sys.argv[2] if len(sys.argv) > 2 else "welcome.png")
print("ok")
