"""Блок «Обновления» с главной — источник событий о сериях (F13).

Фикстура — живой блок 10.09.2026 (без служебных data-url и id строк). Живой случай, из-за
которого перешли на блок: «Крестьянин 999 уровня» 1×12 вышла 9 сентября в субтитрах и дубляже,
а поллер, читавший ленты разделов по курсору, её не увидел.
"""
from datetime import date

from app.rezka.parser import norm_voice, parse_updates
from app.service import classify_update


def test_parse_updates_week_by_days(html):
    items = parse_updates(html("home_updates"))
    assert len(items) > 500
    days = sorted({i.day for i in items})
    assert len(days) == 7 and days[0] == date(2026, 9, 4) and days[-1] == date(2026, 9, 10)
    assert all(i.hdrezka_id and i.season >= 1 and i.episode >= 1 for i in items)
    assert {i.section for i in items} <= {"series", "animation", "cartoons", "films"}


def test_parse_updates_live_case(html):
    items = [i for i in parse_updates(html("home_updates")) if i.hdrezka_id == 90694]
    ep12 = [i for i in items if (i.season, i.episode) == (1, 12)]
    assert ep12, "серия, которую пропустил поллер, в блоке есть"
    assert {"Субтитры", "Дубляж"} <= {i.voice for i in ep12}
    assert all(i.day == date(2026, 9, 9) and i.section == "animation" for i in ep12)
    assert ep12[0].url == "/animation/fantasy/90694-krestyanin-999-urovnya-2026.html"
    assert ep12[0].title == "Крестьянин 999 уровня"


def test_norm_voice_matches_page_names():
    assert norm_voice("Субтитры") == norm_voice("Оригинал (+субтитры)")
    assert norm_voice("FanVoxUA (Украинский)") == norm_voice("FanVoxUA")
    assert norm_voice("Дубляж") != norm_voice("ТО Дубляжная")
    assert norm_voice("AniLibria") == "anilibria" and norm_voice(None) == ""


def test_classify_update():
    assert classify_update(1, 12, has_row=False, known=(1, 11)) == "new"
    assert classify_update(2, 1, has_row=False, known=(1, 24)) == "new", "новый сезон"
    assert classify_update(1, 1, has_row=False, known=None) == "new", "тайтл без истории"
    assert classify_update(1, 12, has_row=True, known=(1, 12)) == "voice", "та же серия, другая озвучка"
    assert classify_update(3, 3, has_row=False, known=(3, 11)) == "catchup", "дозвучка старой серии"
    assert classify_update(1, 11, has_row=False, known=(1, 11)) == "catchup", "последняя известная, но не записанная"
