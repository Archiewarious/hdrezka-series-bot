"""Настройки — одна кнопка на пункт со значением (11.10.2026); тексты без примеров сериалов."""
from sqlalchemy import text

import app.bot.main as main
from app.db import session
from app.i18n import STRINGS
from app.models import User
from test_bot_handlers import FakeCallback


def _kb(edit):
    return [[(b.text, b.callback_data) for b in row] for row in edit[1].inline_keyboard]


def test_main_screen_is_one_button_per_setting(db):
    async def scenario():
        async with session() as s:
            s.add(User(id=71, photos=True, quiet_from=23, quiet_to=8, tz_offset=3))
            await s.commit()
        cb = FakeCallback(71, "set:back")
        await main.cb_settings(cb)
        first = cb.message.edits[-1]
        await main.cb_settings(FakeCallback(71, "set:photos", cb.message))      # тумблер
        return first, cb.message.edits[-1]

    first, toggled = db(scenario)
    rows = _kb(first)
    assert all(len(r) == 1 for r in rows) and len(rows) == 6, "без сетки «Вкл/Выкл»"
    assert [d for r in rows for _, d in r] == ["set:photos", "set:quietcfg", "set:digest", "set:tzcfg", "set:voice", "set:lang"]
    assert rows[0][0][0] == "🖼 Картинки: вкл" and rows[1][0][0] == "🌙 Тихие часы: 23:00–08:00"
    assert rows[3][0][0] == "🕒 Пояс: UTC+3 · Москва" and rows[5][0][0] == "🌐 Язык: Русский"
    assert first[0].count("\n") == 1, "в тексте только заголовок и подсказка"
    assert _kb(toggled)[0][0][0] == "🖼 Картинки: выкл"


def test_quiet_hours_screen_turns_on_and_off_in_place(db):
    async def scenario():
        async with session() as s:
            s.add(User(id=72))
            await s.flush()
            # None в конструкторе не выключает: у колонок значение по умолчанию 23 и 8
            await s.execute(text("UPDATE users SET quiet_from = NULL, quiet_to = NULL WHERE id = 72"))
            await s.commit()
        cb = FakeCallback(72, "set:quietcfg")
        await main.cb_settings(cb)
        off = cb.message.edits[-1]
        await main.cb_settings(FakeCallback(72, "set:quiet:1", cb.message))
        on = cb.message.edits[-1]
        await main.cb_quiet(FakeCallback(72, "setq:off", cb.message))
        return off, on, cb.message.edits[-1]

    off, on, off_again = db(scenario)
    assert off[0] == main.t("ru", "quiet_off_head") and _kb(off)[0][0] == ("Включить: 23:00–08:00", "set:quiet:1")
    assert on[0].startswith("🌙 <b>Тихие часы</b>\nС 23:00 до 08:00") and ("Выключить тихие часы", "setq:off") in _kb(on)[2]
    assert off_again[0] == main.t("ru", "quiet_off_head"), "выключил — остался на экране тихих часов"


def test_time_zone_has_its_own_screen(db):
    async def scenario():
        async with session() as s:
            s.add(User(id=73, tz_offset=3))
            await s.commit()
        cb = FakeCallback(73, "set:tzcfg")
        await main.cb_settings(cb)
        await main.cb_settings(FakeCallback(73, "set:tz:-1", cb.message))
        return cb.message.edits

    first, after = db(scenario)
    assert first[0].startswith("🕒 <b>Часовой пояс</b>: UTC+3") and after[0].startswith("🕒 <b>Часовой пояс</b>: UTC+2")
    assert _kb(after)[0][1] == ("UTC+2 · Киев (зима)", "noop") or _kb(after)[0][1][0].startswith("UTC+2")


def test_prompts_have_no_example_titles():
    """11.10.2026, по просьбе владельца: «просто напишите название или пришлите ссылку», без «слизь»/«дом дракона»."""
    for key in ("start", "find_prompt", "query_short", "new_no_subs"):
        for lang in ("ru", "uk", "en"):
            text_ = STRINGS[key][lang]
            assert "<i>" not in text_ and "слиз" not in text_ and "дракон" not in text_.lower(), (key, lang)
    assert STRINGS["find_prompt"]["ru"] == "Просто напишите название или пришлите ссылку."
