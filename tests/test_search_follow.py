"""Поиск и «нажал — слежу» (10.10.2026): кнопка называется найденным, мусор по одному слову не показывается,
нажатие сразу подписывает, «↩️ Отменить» снимает подписку и оставляет карточку. Старт — одно сообщение."""
import pytest
from sqlalchemy import select

import app.bot.main as main
from app.bot import guard
from app.db import session
from app.models import Schedule, Subscription, User
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


def test_tap_follows_only_the_found_show_and_undo_keeps_the_card(db, quiet):
    """26.10.2026: нажатие — только найденное (раньше — вся франшиза: «Сорвиголова» тянула 38 частей). Вся франшиза —
    кнопкой в карточке."""
    async def scenario():
        async with session() as s:
            s.add(User(id=63))
            got, dragon = await _dragon_catalog(s)
            await s.commit()
        results = FakeMessage(63, reply_markup=main._kb([[("🔔 Дом Дракона · 2×8 · «Игра престолов»", f"go:p:{dragon.id}")]]))
        cb = FakeCallback(63, f"go:p:{dragon.id}", results)
        await main.cb_go(cb)
        async with session() as s:
            subs = [(x.scope, x.page_id) for x in (await s.execute(select(Subscription))).scalars()]
            sub_id = await s.scalar(select(Subscription.id))
        card_text, card_kb = results.answers[-1]
        buttons = [(b.text, b.callback_data) for row in card_kb.inline_keyboard for b in row]
        undo = next(d for text_, d in buttons if text_ == main.t("ru", "btn_undo"))

        card = FakeMessage(63)
        await main.cb_undo(FakeCallback(63, undo, card))
        async with session() as s:
            left = (await s.execute(select(Subscription))).scalars().all()
        return got.id, subs, results, card_text, buttons, undo, sub_id, dragon.id, card, left

    fid, subs, results, card_text, buttons, undo, sub_id, page_id, card, left = db(scenario)
    assert subs == [("page", page_id)], "только «Дом Дракона», не вся «Игра престолов»"
    assert results.reply_markup.inline_keyboard[0][0].text.startswith("✓ Дом Дракона"), "в выдаче — галочка"
    assert card_text.startswith("✅ Слежу: <b>Дом Дракона</b>") and "франшизой" not in card_text
    assert ("🎞 Вся франшиза «Игра престолов» (2)", f"subf:{fid}:{page_id}") in buttons
    assert undo == f"undo:p:{sub_id}:{page_id}"
    assert left == [] and card.edits, "отменено, карточка осталась на месте"
    after_text, after_kb = card.edits[-1]
    assert after_text.startswith("Найдено: <b>Дом Дракона</b>")
    assert any(b.text == main.t("ru", "btn_follow") for row in after_kb.inline_keyboard for b in row)


def test_tick_answers_and_turns_back_into_a_bell_after_undo(db, quiet):
    """19.11.2026: «✓ …» после нажатия была noop и молчала — новички жали её по 1–4 раза. Теперь тост «уже слежу».
    После «↩️ Отменить» в карточке галочка в выдаче остаётся — нажатие на неё говорит «подписки уже нет» и возвращает
    «🔔 …»: нажал — снова слежу. Остальные кнопки выдачи не трогаются."""
    async def scenario():
        async with session() as s:
            s.add(User(id=68))
            got, dragon = await _dragon_catalog(s)
            await s.commit()
        go = f"go:p:{dragon.id}"
        results = FakeMessage(68, reply_markup=main._kb([[("🔔 Дом Дракона · 2×8 · «Игра престолов»", go)],
                                                          [("🔍 Нет нужного — искать на сайте", "site:0123456789ab")]]))
        await main.cb_go(FakeCallback(68, go, results))
        tick = results.reply_markup.inline_keyboard[0][0]
        following = FakeCallback(68, tick.callback_data, results)
        await main.cb_done(following)
        card_kb = results.answers[-1][1]
        undo = next(b.callback_data for row in card_kb.inline_keyboard for b in row if b.text == main.t("ru", "btn_undo"))
        await main.cb_undo(FakeCallback(68, undo, FakeMessage(68)))
        gone = FakeCallback(68, tick.callback_data, results)
        await main.cb_done(gone)
        bell = results.reply_markup.inline_keyboard[0][0]
        await main.cb_go(FakeCallback(68, bell.callback_data, results))
        async with session() as s:
            subs = [x.page_id for x in (await s.execute(select(Subscription))).scalars()]
        return dragon.id, tick, following.toasts, gone.toasts, bell, results.reply_markup, subs

    page_id, tick, following, gone, bell, kb, subs = db(scenario)
    assert (tick.text, tick.callback_data) == ("✓ Дом Дракона · 2×8 · «Игра престолов»", f"done:go:p:{page_id}")
    assert following == [main.t("ru", "toast_following")] and following[0].startswith("Уже слежу ✓")
    assert gone == [main.t("ru", "toast_no_sub")]
    assert (bell.text, bell.callback_data) == ("🔔 Дом Дракона · 2×8 · «Игра престолов»", f"go:p:{page_id}")
    assert kb.inline_keyboard[0][0].text.startswith("✓ ") and subs == [page_id], "снова слежу — снова галочка"
    assert kb.inline_keyboard[1][0].callback_data == "site:0123456789ab"


def test_tick_counts_a_franchise_followed_as_a_whole(db, quiet):
    """19.11.2026: часть франшизы, за которой следят целиком, и франшиза из выдачи — «уже слежу»; неизвестная
    страница — «подписки нет», без ошибок."""
    async def scenario():
        async with session() as s:
            s.add(User(id=69))
            got, dragon = await _dragon_catalog(s)
            await main.svc.subscribe_franchise(s, 69, got.id)
            await s.commit()
        toasts = []
        for data in (f"done:go:p:{dragon.id}", f"done:go:f:{got.id}", f"done:sub:{dragon.hdrezka_id}", "done:go:p:999999"):
            cb = FakeCallback(69, data, FakeMessage(69))
            await main.cb_done(cb)
            toasts += cb.toasts
        return toasts

    following, no_sub = main.t("ru", "toast_following"), main.t("ru", "toast_no_sub")
    assert db(scenario) == [following, following, following, no_sub]


def test_tap_does_not_duplicate_a_followed_franchise(db, quiet):
    async def scenario():
        async with session() as s:
            s.add(User(id=66))
            got, dragon = await _dragon_catalog(s)
            await main.svc.subscribe_franchise(s, 66, got.id)
            await s.commit()
        msg = FakeMessage(66)
        await main.cb_go(FakeCallback(66, f"go:p:{dragon.id}", msg))
        async with session() as s:
            return [x.scope for x in (await s.execute(select(Subscription))).scalars()], msg.answers[-1][0]

    scopes, card_text = db(scenario)
    assert scopes == ["franchise"] and "Слежу в составе франшизы «Игра престолов»" in card_text


def test_finished_show_in_a_big_franchise_offers_only_this_show(db, quiet):
    """Карточка «Сорвиголовы» (26.10.2026): «🔔 Следить за продолжением» — эта страница, вся франшиза — отдельно;
    третий сезон уже в расписании — сказано прямо."""
    async def scenario():
        async with session() as s:
            s.add(User(id=67))
            fr = await _known_franchise(s, 9800, "Человек-паук")
            dd = await _page(s, 9801, "Сорвиголова: Рожденный заново", last=(2, 8), franchise_id=fr.id, finished=True)
            await _page(s, 9802, "Вижен", franchise_id=fr.id)
            s.add(Schedule(page_id=dd.id, season=3, episode=1, air_date=None))
            await s.commit()
        text_, kb = await main._render_page_card(67, dd.id)
        return fr.id, dd, text_, [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]

    fid, dd, text_, buttons = db(scenario)
    assert "3-й сезон уже в расписании — пришлю 3×1, как только выйдет." in text_
    assert ("🔔 Следить за продолжением", f"wait:{dd.hdrezka_id}") in buttons
    assert ("🎞 Вся франшиза «Человек-паук» (2)", f"subf:{fid}:{dd.id}") in buttons
    assert not any((d or "").startswith("subf_all") for _, d in buttons), "одной кнопкой на всю франшизу не подписывает"


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
    assert "Просто напишите название или пришлите ссылку." in answers[0][0] and len(answers[0][0]) < 150


def test_new_buttons_use_one_word():
    """«Подписаться» в кнопках больше нет: везде «Следить» (10.10.2026)."""
    from app.i18n import STRINGS
    buttons = {k: v["ru"] for k, v in STRINGS.items() if k.startswith("btn_") and isinstance(v["ru"], str)}
    assert [k for k, v in buttons.items() if "одписа" in v] == []
