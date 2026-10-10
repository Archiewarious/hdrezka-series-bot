"""Поиск и «нажал — слежу» (10.10.2026): кнопка называется найденным, мусор по одному слову не показывается,
нажатие сразу подписывает, «↩️ Отменить» снимает подписку и оставляет карточку. Старт — одно сообщение."""
import pytest
from sqlalchemy import select

import app.bot.main as main
from app.bot import guard
from app.db import session
from app.models import Subscription, User
from test_bot_handlers import FakeCallback, FakeMessage, _message
from test_integration import _known_franchise, _page


@pytest.fixture
def quiet(monkeypatch):
    """Ни сайта, ни фоновых задач: подписка не дочитывает страницу, лимиты свежие."""
    monkeypatch.setattr(main, "_background", lambda coro: coro.close())
    monkeypatch.setattr(guard, "cheap_actions", guard.UserLimiter(per_minute=30, per_hour=600))
    monkeypatch.setattr(guard, "site_actions", guard.UserLimiter(per_minute=0, per_hour=0))

    async def no_photo(*a, **kw):
        return None
    monkeypatch.setattr(main.posters, "send_photo_cached", no_photo)


async def _dragon_catalog(s):
    """«дом дракона»: сам сериал во франшизе «Игра престолов» и три совпадения по одному слову — как в проде 10.10.2026."""
    got = await _known_franchise(s, 9100, "Игра престолов")
    pororo = await _known_franchise(s, 9200, "Пингвинёнок Пороро")
    dragon = await _page(s, 9101, "Дом Дракона", last=(2, 8), franchise_id=got.id)
    await _page(s, 9102, "Игра престолов", last=(8, 6), franchise_id=got.id, finished=True)
    await _page(s, 9201, "Пингвинёнок Пороро: Приключения в замке дракона", last=(1, 3), franchise_id=pororo.id)
    await _page(s, 9300, "Новая таверна дракона", last=(1, 42))
    return got, dragon


def _buttons(msg: FakeMessage) -> list[tuple[str, str]]:
    kb = msg.answers[-1][1]
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


def test_search_shows_what_was_found_and_drops_one_word_matches(db, quiet):
    async def scenario():
        async with session() as s:
            got, dragon = await _dragon_catalog(s)
            await s.commit()
        msg = _message(61)
        await main._handle_search(msg, "ru", "дом дракона")
        return msg, dragon.id

    msg, dragon_id = db(scenario)
    buttons = _buttons(msg)
    assert msg.answers[-1][0] == main.t("ru", "found_n", n=1)
    assert buttons[0] == ("🔔 Дом Дракона · 2×8 · «Игра престолов»", f"go:p:{dragon_id}")
    assert len(buttons) == 2 and buttons[1][1].startswith("site:"), "таверна и Пороро — совпадение по одному слову"


def test_query_matching_the_franchise_name_shows_the_franchise(db, quiet):
    async def scenario():
        async with session() as s:
            fr = await _known_franchise(s, 9400, "Ведьмак")
            await _page(s, 9401, "Ведьмак: Кошмар волка", franchise_id=fr.id, finished=True)
            await _page(s, 9402, "Ведьмак", last=(4, 8), franchise_id=fr.id)
            await _page(s, 9403, "Сильнейшая ведьма в мире стартует", last=(1, 1))
            await s.commit()
        msg = _message(62)
        await main._handle_search(msg, "ru", "ведьмак")
        return msg, fr.id

    msg, fid = db(scenario)
    buttons = _buttons(msg)
    text_, data = buttons[0]
    assert data == f"go:f:{fid}" and text_.startswith("🔔 Ведьмак · 2 части, выходит 1")
    assert len(buttons) == 2, "«ведьма» рядом с полным совпадением «ведьмак» — шум"


def test_tap_follows_the_franchise_and_undo_keeps_the_card(db, quiet):
    async def scenario():
        async with session() as s:
            s.add(User(id=63))
            got, dragon = await _dragon_catalog(s)
            await s.commit()
        results = FakeMessage(63, reply_markup=main._kb([[("🔔 Дом Дракона · 2×8 · «Игра престолов»", f"go:p:{dragon.id}")]]))
        cb = FakeCallback(63, f"go:p:{dragon.id}", results)
        await main.cb_go(cb)
        async with session() as s:
            subs = [(x.scope, x.franchise_id) for x in (await s.execute(select(Subscription))).scalars()]
            sub_id = await s.scalar(select(Subscription.id))
        card_text, card_kb = results.answers[-1]
        undo = next(b.callback_data for row in card_kb.inline_keyboard for b in row if b.text == main.t("ru", "btn_undo"))

        card = FakeMessage(63)
        await main.cb_undo(FakeCallback(63, undo, card))
        async with session() as s:
            left = (await s.execute(select(Subscription))).scalars().all()
        return got.id, subs, results, card_text, undo, sub_id, dragon.id, card, left

    fid, subs, results, card_text, undo, sub_id, page_id, card, left = db(scenario)
    assert subs == [("franchise", fid)], "нажатие в выдаче — франшиза целиком, как «🔔 Следить» в карточке"
    assert results.reply_markup.inline_keyboard[0][0].text.startswith("✓ Дом Дракона"), "в выдаче — галочка"
    assert card_text.startswith("✅ Слежу: <b>Дом Дракона</b>") and "И за всей франшизой «Игра престолов»" in card_text
    assert undo == f"undo:p:{sub_id}:{page_id}"
    assert left == [] and card.edits, "отменено, карточка осталась на месте"
    after_text, after_kb = card.edits[-1]
    assert after_text.startswith("Найдено: <b>Дом Дракона</b>")
    assert any(b.text == main.t("ru", "btn_follow") for row in after_kb.inline_keyboard for b in row)


def test_tap_on_a_finished_show_without_franchise_watches_for_a_continuation(db, quiet):
    async def scenario():
        async with session() as s:
            s.add(User(id=64))
            page = await _page(s, 9500, "Старый сериал", last=(3, 10), finished=True)
            await s.commit()
        msg = FakeMessage(64)
        await main.cb_go(FakeCallback(64, f"go:p:{page.id}", msg))
        async with session() as s:
            subs = [(x.scope, x.page_id) for x in (await s.execute(select(Subscription))).scalars()]
        return page.id, subs, msg.answers[-1][0]

    page_id, subs, card_text = db(scenario)
    assert subs == [("page", page_id)] and card_text.startswith("✅ Слежу за продолжением: <b>Старый сериал</b>")


def test_start_is_one_short_message(db):
    async def scenario():
        msg = FakeMessage(65)
        await main._welcome(msg, "ru")
        return msg.answers

    answers = db(scenario)
    assert len(answers) == 1 and answers[0][0] == main.t("ru", "start")
    assert "Дом дракона" in answers[0][0] and len(answers[0][0]) < 150


def test_new_buttons_use_one_word():
    """«Подписаться» в кнопках больше нет: везде «Следить» (10.10.2026)."""
    from app.i18n import STRINGS
    buttons = {k: v["ru"] for k, v in STRINGS.items() if k.startswith("btn_") and isinstance(v["ru"], str)}
    assert [k for k, v in buttons.items() if "одписа" in v] == []
