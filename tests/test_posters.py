"""Постеры приводятся к одному размеру: 12.09.2026 в ленте уведомлений картинки шли разной высоты."""
import io

from PIL import Image

from app.posters import POSTER_SIZE, _to_standard


def _jpeg(w: int, h: int, colour=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "JPEG")
    return buf.getvalue()


def _size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as im:
        assert im.format == "JPEG"
        return im.size


def test_any_proportion_becomes_one_size():
    """Отношения сторон у HDREZKA: 0.625 («Изгнанный») … 0.745 («Адский уровень»)."""
    for w, h in ((640, 1024), (2086, 2951), (2000, 3000), (805, 1080), (1000, 1414)):
        assert _size(_to_standard(_jpeg(w, h))) == POSTER_SIZE, f"{w}x{h}"


def test_picture_is_not_cropped():
    """Вписываем целиком: у постеров текст у самых краёв, обрезать нельзя. Поля — размытый фон."""
    w, h = 2000, 3000                                     # уже POSTER_SIZE — поля будут слева и справа
    src = Image.new("RGB", (w, h), (10, 10, 10))
    src.paste(Image.new("RGB", (w, 60), (255, 255, 255)), (0, 0))          # белая полоса по верхнему краю
    src.paste(Image.new("RGB", (w, 60), (255, 255, 255)), (0, h - 60))     # и по нижнему
    buf = io.BytesIO()
    src.save(buf, "JPEG")
    with Image.open(io.BytesIO(_to_standard(buf.getvalue()))) as out:
        assert out.size == POSTER_SIZE
        assert out.getpixel((POSTER_SIZE[0] // 2, 8))[0] > 200, "верхний край постера на месте"
        assert out.getpixel((POSTER_SIZE[0] // 2, POSTER_SIZE[1] - 8))[0] > 200, "нижний край на месте"
        assert out.getpixel((3, POSTER_SIZE[1] // 2))[0] < 100, "по бокам — затемнённый фон, а не картинка"


def test_broken_bytes_are_sent_as_is():
    """Лучше постер не того размера, чем пост без картинки."""
    import asyncio

    from app.posters import to_standard
    assert asyncio.run(to_standard(b"not an image")) == b"not an image"
