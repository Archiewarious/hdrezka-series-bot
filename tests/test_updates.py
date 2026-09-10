"""Блок «Обновления» с главной — источник событий о сериях (F13).

Фикстура — живой блок 10.09.2026 (без служебных data-url и id строк). Живой случай, из-за
которого перешли на блок: «Крестьянин 999 уровня» 1×12 вышла 9 сентября в субтитрах и дубляже,
а поллер, читавший ленты разделов по курсору, её не увидел.
"""
from datetime import date

from app.rezka.parser import match_voice, parse_updates, voice_key
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


def test_voice_nested_parens_are_kept(html):
    """10.09.2026: «(FanVoxUA (Украинский))» разбиралось в «FanVoxUA (Украинский»."""
    voices = {i.voice for i in parse_updates(html("home_updates")) if i.voice}
    assert "FanVoxUA (Украинский)" in voices
    assert all(v.count("(") == v.count(")") and not v.startswith("(") for v in voices)


def test_voice_key_keeps_qualifiers():
    assert voice_key("  Дубляж   (TVOË) ") == "дубляж (tvoë)"
    assert voice_key("Дубляж (TVOË)") != voice_key("Дубляж (18+)"), "уточнение в скобках — другой переводчик"


def test_match_voice_exact_and_safe_fallbacks():
    names = {1: "FanVoxUA (Украинский)", 2: "Многоголосый закадровый", 3: "Оригинал (+субтитры)", 4: "Дубляж",
             5: "ТО Дубляжная", 6: "лостфильм (LostFilm)", 7: "октопус (Octopus/Ultradox)",
             8: "HDrezka Studio", 9: "HDrezka Studio (18+)"}
    assert match_voice(names, "FanVoxUA (Украинский)") == 1
    assert match_voice(names, "многоголосый") == 2
    assert match_voice(names, "Субтитры") == 3
    assert match_voice(names, "Дубляж") == 4, "точное имя важнее похожего «ТО Дубляжная»"
    assert match_voice(names, "LostFilm") == 6 and match_voice(names, "Octopus") == 7
    assert match_voice(names, "HDrezka Studio (18+)") == 9 and match_voice(names, "HDrezka Studio") == 8
    assert match_voice(names, "Coldfilm") is None and match_voice(names, None) is None


def test_match_voice_refuses_to_guess():
    """10.09.2026: «Дубляж» отмечал случайного из нескольких «Дубляж (…)» на одной странице."""
    assert match_voice({1: "Дубляж (HDrezka Studio)", 2: "Дубляж (TVOË)", 3: "Дубляж (неофициальный)"}, "Дубляж") is None
    assert match_voice({1: "HDrezka Studio", 2: "HDrezka Studio"}, "HDrezka Studio") is None
    assert match_voice({1: "HDrezka Studio"}, "HDrezka Studio (Украинский)") is None, "украинская версия — не русская"


def test_classify_update():
    assert classify_update(True, (1, 12), 1, 12, fresh=True) == "voice"
    assert classify_update(False, (1, 11), 1, 12, fresh=False) == "new"
    assert classify_update(False, (1, 24), 2, 1, fresh=True) == "new", "новый сезон"
    assert classify_update(False, (3, 11), 3, 3, fresh=True) == "catchup", "дозвучка старой серии"
    assert classify_update(False, None, 1, 1, fresh=True) == "new", "премьера на странице без записей"
    assert classify_update(False, None, 1, 5, fresh=False) == "catchup", "старое событие о незнакомом тайтле"
