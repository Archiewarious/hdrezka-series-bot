from app.sender import BUTTON_MAX_LEN, Rendered, _digest, fit_button, _single_keyboard, watch_url


def _r(kind, title, sxe, scope="page", sub_id=7):
    label = f"▶ Смотреть {sxe}" if kind != "new_part" else "▶ Открыть"
    return Rendered(kind, 1, 88337, title, "txt", f"• <b>{title}</b> — {sxe}", label,
                    "https://x/a.html#t:1-s:1-e:1", None, None, sub_id, scope, can_follow=True)


def test_watch_url():
    assert watch_url("https://x/a.html", 509, 4, 22) == "https://x/a.html#t:509-s:4-e:22"
    assert watch_url("https://x/a.html", None, 4, 22) == "https://x/a.html"


def testfit_button_keeps_suffix():
    b = fit_button("▶ ", "О моём перерождении в слизь [ТВ-4] очень длинное название", " 4×22")
    assert len(b) <= BUTTON_MAX_LEN and b.endswith(" 4×22") and b.startswith("▶ ")
    assert fit_button("▶ ", "Слизь", " 4×22") == "▶ Слизь 4×22"


def test_single_keyboard_episode_and_new_part():
    kb = _single_keyboard(_r("voice:509", "Слизь", "4×22", scope="franchise"))
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert texts == ["▶ Смотреть 4×22", "🎙 Другие озвучки", "🔕 Не следить за франшизой"]
    assert kb.inline_keyboard[2][0].callback_data == "unsubq:7"
    kb = _single_keyboard(_r("new_part", "Бабочка", ""))
    assert [b.text for row in kb.inline_keyboard for b in row] == ["▶ Открыть", "➕ Следить за этой частью", "🔕 Не следить"]
    kb = _single_keyboard(_r("episode", "Слизь", "4×22", sub_id=None))
    assert len(kb.inline_keyboard) == 1, "без подписки — только ссылка"


def test_digest():
    body, kb = _digest([_r("episode", "Слизь", "4×22"), _r("voice:1", "Дом Дракона", "3×9"), _r("new_part", "Бабочка", "")])
    assert body.startswith("🆕 Вышли новые серии\n\n•")
    assert [r[0].text for r in kb.inline_keyboard] == ["▶ Слизь 4×22", "▶ Дом Дракона 3×9", "▶ Бабочка"]
    assert all(len(r[0].text) <= BUTTON_MAX_LEN for r in kb.inline_keyboard)
    body, _ = _digest([_r("new_part", "А", ""), _r("new_part", "Б", "")])
    assert body.startswith("🆕 Новые части франшиз")
