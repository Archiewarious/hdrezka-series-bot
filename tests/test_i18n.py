"""Тексты на трёх языках: полнота, формы множественного числа, шаблоны без ошибок форматирования."""
from datetime import date, timedelta

import pytest

from app.i18n import LANGS, STRINGS, detect, fmt_date, plural, t, when

KW = dict(n=5, s=1, e=2, h=8, f=23, t=8, v="x", d="x", name="x", title="x", sxe="1×2", kind="x", tail="x", parts="x")


def test_every_key_has_every_language():
    missing = [(k, lang) for k, v in STRINGS.items() for lang in LANGS if lang not in v]
    assert missing == []


@pytest.mark.parametrize("lang", list(LANGS))
def test_every_string_formats(lang):
    for key, entry in STRINGS.items():
        val = entry[lang]
        forms = val if isinstance(val, tuple) else (val,)
        for form in forms:
            form.format(**KW)   # KeyError / ValueError = битый шаблон


def test_plural_forms():
    assert [t("ru", "parts_n", n=n) for n in (1, 2, 5, 11, 21, 104)] == \
        ["1 часть", "2 части", "5 частей", "11 частей", "21 часть", "104 части"]
    assert [t("uk", "parts_n", n=n) for n in (1, 3, 7)] == ["1 частина", "3 частини", "7 частин"]
    assert [t("en", "parts_n", n=n) for n in (1, 2)] == ["1 part", "2 parts"]
    assert plural("en", 0, ("a", "b")) == "b"


def test_detect_and_fallback():
    assert detect("uk") == "uk" and detect("en-US") == "en" and detect("ru") == "ru"
    assert detect(None) == "ru" and detect("de") == "ru"
    assert t("xx", "btn_my") == t("ru", "btn_my"), "неизвестный язык — русский"


def test_dates():
    d = date(2026, 9, 6)
    assert fmt_date("ru", d) == "6 сен" and fmt_date("uk", d) == "6 вер" and fmt_date("en", d) == "Sep 6"
    assert fmt_date("en", None) is None
    assert when("en", date.today()).endswith("· today")
    assert when("uk", date.today() + timedelta(days=1)).endswith("· завтра")
    assert when("ru", date.today() - timedelta(days=3)).endswith("· 3 дн. назад")
