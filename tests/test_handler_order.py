"""Кнопки нижнего меню должны разбираться раньше ловушки on_text.

Живой случай 07.09.2026: обработчик календаря стоял в файле ниже `on_text`, поэтому нажатие
«📅 Календарь» уходило в поиск по сайту как обычный текст. aiogram проверяет обработчики
в порядке регистрации, а он совпадает с порядком в файле.
"""
import pathlib
import re

SRC = (pathlib.Path(__file__).parents[1] / "app" / "bot" / "main.py").read_text().splitlines()

CATCH_ALL = '@dp.message(F.text & ~F.text.startswith("/"))'
MENU_RX = re.compile(r'@dp\.message\(F\.text\.in_\(MENU\["(\w+)"\]\)\)')


def test_menu_buttons_registered_before_catch_all():
    catch_all = next(i for i, line in enumerate(SRC) if line.strip() == CATCH_ALL)
    late = [MENU_RX.search(line).group(1) for i, line in enumerate(SRC)
            if i > catch_all and MENU_RX.search(line)]
    assert late == [], f"обработчики кнопок ниже on_text — нажатие уйдёт в поиск: {late}"


def test_every_menu_button_has_a_handler():
    handled = {MENU_RX.search(line).group(1) for line in SRC if MENU_RX.search(line)}
    assert handled == {"btn_find", "btn_my", "btn_new", "btn_cal", "btn_settings", "btn_help"}
