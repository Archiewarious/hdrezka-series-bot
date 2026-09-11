"""Уведомление — пост (решение 11.09.2026): название, сезон и серия, озвучка, одна кнопка на сайт."""
from app.sender import BUTTON_MAX_LEN, TG_MAX_LEN, Rendered, _digest, _single_keyboard, fit_button, retry_delay, watch_url


def _r(kind, title, page_id=1, line=None, url="https://x/a.html#t:1-s:1-e:1"):
    return Rendered(kind, page_id, title, "txt", line or f"🎬 <b>{title}</b>", url, None, None)


def test_watch_url():
    assert watch_url("https://x/a.html", 509, 4, 22) == "https://x/a.html#t:509-s:4-e:22"
    assert watch_url("https://x/a.html", None, 4, 22) == "https://x/a.html"


def test_fit_button_keeps_suffix():
    b = fit_button("▶ ", "О моём перерождении в слизь [ТВ-4] очень длинное название", " 4×22")
    assert len(b) <= BUTTON_MAX_LEN and b.endswith(" 4×22") and b.startswith("▶ ")
    assert fit_button("▶ ", "Слизь") == "▶ Слизь"


def test_post_has_only_watch_button():
    for kind in ("episode", "voice:56", "new_part"):
        kb = _single_keyboard(_r(kind, "Слизь"))
        assert [[b.text for b in row] for row in kb.inline_keyboard] == [["▶ Смотреть на HDrezka"]]
        assert kb.inline_keyboard[0][0].url.startswith("https://")
    assert _single_keyboard(_r("episode", "Slime"), lang="en").inline_keyboard[0][0].text == "▶ Watch on HDrezka"


def test_digest_one_button_per_series():
    items = [_r("voice:56", "Слизь", page_id=1, url="u1"), _r("voice:7", "Слизь", page_id=1, url="u2"),
             _r("episode", "Дом Дракона", page_id=2, url="u3"), _r("new_part", "Бабочка", page_id=3, url="u4")]
    body, kb = _digest(items)
    assert body.startswith("🆕 <b>Вышли новые серии</b>\n\n🎬 <b>Слизь</b>\n\n🎬 <b>Слизь</b>")
    assert [(r[0].text, r[0].url) for r in kb.inline_keyboard] == [("▶ Слизь", "u2"), ("▶ Дом Дракона", "u3"),
                                                                     ("▶ Бабочка", "u4")]
    assert _digest([_r("new_part", "А"), _r("new_part", "Б", page_id=2)])[0].startswith("🆕 <b>Новые части</b>")
    assert _digest([_r("episode", "Slime")], lang="uk")[0].startswith("🆕 <b>Вийшли нові серії</b>")


def test_digest_never_cuts_html():
    items = [_r("episode", f"Сериал {i}", page_id=i, line="🎬 <b>" + "x" * 300 + "</b>") for i in range(30)]
    body, kb = _digest(items)
    assert len(body) <= TG_MAX_LEN and body.count("<b>") == body.count("</b>") and body.endswith("…")
    assert len(kb.inline_keyboard) == 10


def test_retry_delay_grows_and_never_gives_up():
    """11.09.2026: после пятой неудачи уведомление навсегда оставалось «в очереди»."""
    assert [retry_delay(a) for a in (1, 2, 3, 4, 5, 6, 50)] == [60, 120, 240, 480, 960, 1800, 1800]
