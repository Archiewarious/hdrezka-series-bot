"""Тексты бота и уведомлений на трёх языках: ru / uk / en.

Ключ → {lang: строка}. Строка с формами множественного числа — кортеж форм: ru/uk — 3 формы
(1, 2–4, 5+), en — 2; выбирается по kw["n"]. Нет перевода — берём русский (полнота проверяется тестом).
Язык нового пользователя — из Telegram language_code (detect), дальше — настройка в ⚙️.
"""
from __future__ import annotations

from datetime import date

LANGS = {"ru": "Русский", "uk": "Українська", "en": "English"}
DEFAULT = "ru"


def detect(language_code: str | None) -> str:
    code = (language_code or "").lower()[:2]
    return code if code in ("uk", "en") else DEFAULT


def plural(lang: str, n: int, forms: tuple) -> str:
    if lang == "en":
        return forms[0] if n == 1 else forms[-1]
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return forms[0]
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return forms[1]
    return forms[2]


def t(lang: str, key: str, **kw) -> str:
    entry = STRINGS[key]
    val = entry.get(lang) or entry[DEFAULT]
    if isinstance(val, tuple):
        val = plural(lang, kw["n"], val)
    return val.format(**kw) if kw else val


MONTHS = {
    "ru": ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"],
    "uk": ["січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}
WEEKDAYS = {
    "ru": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"],
    "uk": ["пн", "вт", "ср", "чт", "пт", "сб", "нд"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}


def fmt_date(lang: str, d: date | None) -> str | None:
    if not d:
        return None
    m = MONTHS.get(lang, MONTHS[DEFAULT])[d.month - 1]
    return f"{m} {d.day}" if lang == "en" else f"{d.day} {m}"


def when(lang: str, d: date) -> str:
    delta = (d - date.today()).days
    if delta == 0:
        rel = t(lang, "today")
    elif delta == 1:
        rel = t(lang, "tomorrow")
    elif delta == -1:
        rel = t(lang, "yesterday")
    elif delta > 1:
        rel = t(lang, "in_days", n=delta)
    else:
        rel = t(lang, "days_ago", n=-delta)
    return f"{WEEKDAYS.get(lang, WEEKDAYS[DEFAULT])[d.weekday()]} {fmt_date(lang, d)} · {rel}"


STRINGS: dict[str, dict[str, str | tuple]] = {
    # --- меню и команды
    "btn_find": {"ru": "🔍 Найти", "uk": "🔍 Знайти", "en": "🔍 Search"},
    "btn_my": {"ru": "📋 Мои подписки", "uk": "📋 Мої підписки", "en": "📋 My subscriptions"},
    "btn_new": {"ru": "🆕 Новое", "uk": "🆕 Нове", "en": "🆕 New"},
    "btn_cal": {"ru": "📅 Календарь", "uk": "📅 Календар", "en": "📅 Calendar"},
    "btn_settings": {"ru": "⚙️ Настройки", "uk": "⚙️ Налаштування", "en": "⚙️ Settings"},
    "btn_help": {"ru": "❓ Помощь", "uk": "❓ Допомога", "en": "❓ Help"},
    "cmd_my": {"ru": "Мои подписки", "uk": "Мої підписки", "en": "My subscriptions"},
    "cmd_new": {"ru": "Что вышло за неделю", "uk": "Що вийшло за тиждень", "en": "Released this week"},
    "cmd_calendar": {"ru": "Календарь выхода серий", "uk": "Календар виходу серій", "en": "Episode calendar"},
    "cmd_settings": {"ru": "Настройки", "uk": "Налаштування", "en": "Settings"},
    "cmd_help": {"ru": "Как пользоваться", "uk": "Як користуватися", "en": "How to use"},
    "hint": {
        "ru": "\n\n<i>Добавить ещё — напишите название. Список — «📋 Мои подписки».</i>",
        "uk": "\n\n<i>Додати ще — напишіть назву. Список — «📋 Мої підписки».</i>",
        "en": "\n\n<i>To add more, type a title. Your list is under “📋 My subscriptions”.</i>",
    },
    "start": {
        "ru": ("Привет! Я сообщу, когда выйдет новая серия.\n\n"
               "Напишите название — например, «слизь» — и выберите, за чем следить. Можно за одним сезоном, "
               "а можно за всей франшизой: тогда расскажу и о новых сезонах, фильмах, спин-оффах.\n\n"
               "Кнопки внизу — всё управление. 👇"),
        "uk": ("Привіт! Я повідомлю, коли вийде нова серія.\n\n"
               "Напишіть назву — наприклад, «слиз» — і виберіть, за чим стежити. Можна за одним сезоном, "
               "а можна за всією франшизою: тоді розповім і про нові сезони, фільми, спін-офи.\n\n"
               "Кнопки внизу — все керування. 👇"),
        "en": ("Hi! I'll let you know when a new episode is out.\n\n"
               "Type a title — for example “slime” — and choose what to follow. You can follow a single season "
               "or the whole franchise: then I'll also tell you about new seasons, films and spin-offs.\n\n"
               "The buttons below are all the controls. 👇"),
    },
    "help": {
        "ru": ("Слежу за выходом новых серий на HDREZKA и присылаю уведомления.\n\n"
               "<b>Как подписаться</b>\n"
               "• пришлите название — покажу, что сейчас выходит\n"
               "• или ссылку на страницу тайтла\n\n"
               "Подписаться можно на <b>один сезон</b> или на <b>всю франшизу</b> — тогда "
               "сообщу и о новых сезонах, фильмах и спин-оффах.\n\n"
               "Сезон уже вышел целиком, а франшизы нет? На его карточке есть «🔔 Сообщить о продолжении».\n\n"
               "Кнопки внизу — главное меню: поиск, подписки, что нового за неделю, календарь, настройки, помощь."),
        "uk": ("Стежу за виходом нових серій на HDREZKA і надсилаю сповіщення.\n\n"
               "<b>Як підписатися</b>\n"
               "• надішліть назву — покажу, що зараз виходить\n"
               "• або посилання на сторінку тайтлу\n\n"
               "Підписатися можна на <b>один сезон</b> або на <b>всю франшизу</b> — тоді "
               "повідомлю і про нові сезони, фільми та спін-офи.\n\n"
               "Сезон уже вийшов повністю, а франшизи немає? На його картці є «🔔 Повідомити про продовження».\n\n"
               "Кнопки внизу — головне меню: пошук, підписки, що нового за тиждень, календар, налаштування, допомога."),
        "en": ("I watch for new episodes on HDREZKA and send notifications.\n\n"
               "<b>How to subscribe</b>\n"
               "• send a title — I'll show what's airing now\n"
               "• or a link to a title page\n\n"
               "You can follow <b>one season</b> or the <b>whole franchise</b> — then I'll also "
               "tell you about new seasons, films and spin-offs.\n\n"
               "Season already finished and there's no franchise? Its card has “🔔 Notify about a sequel”.\n\n"
               "The buttons below are the main menu: search, subscriptions, this week's releases, calendar, settings, help."),
    },
    "busy": {"ru": "Сайт сейчас отвечает медленно или недоступен — попробуйте через пару минут.",
             "uk": "Сайт зараз відповідає повільно або недоступний — спробуйте за кілька хвилин.",
             "en": "The site is slow or unavailable right now — try again in a couple of minutes."},
    "too_fast": {"ru": "Слишком много запросов — подождите минуту.",
                 "uk": "Забагато запитів — зачекайте хвилину.",
                 "en": "Too many requests — wait a minute."},
    "share_text": {"ru": "Следить за «{title}» — новые серии в Telegram",
                   "uk": "Стежити за «{title}» — нові серії в Telegram",
                   "en": "Follow “{title}” — new episodes in Telegram"},
    "cal_note": {
        "ru": "\n\n<i>Даты — оригинального эфира. На HDREZKA серия появляется позже, обычно в тот же день или на следующий.</i>",
        "uk": "\n\n<i>Дати — оригінального ефіру. На HDREZKA серія з'являється пізніше, зазвичай того ж дня або наступного.</i>",
        "en": "\n\n<i>Dates are the original air dates. The episode appears on HDREZKA later, usually the same or the next day.</i>",
    },
    "today": {"ru": "сегодня", "uk": "сьогодні", "en": "today"},
    "tomorrow": {"ru": "завтра", "uk": "завтра", "en": "tomorrow"},
    "yesterday": {"ru": "вчера", "uk": "вчора", "en": "yesterday"},
    "in_days": {"ru": "через {n} дн.", "uk": "через {n} дн.", "en": "in {n} d."},
    "days_ago": {"ru": "{n} дн. назад", "uk": "{n} дн. тому", "en": "{n} d. ago"},
    # --- типы
    "sec_series": {"ru": "сериал", "uk": "серіал", "en": "series"},
    "sec_animation": {"ru": "аниме", "uk": "аніме", "en": "anime"},
    "sec_cartoons": {"ru": "мультсериал", "uk": "мультсеріал", "en": "cartoon"},
    "sec_films": {"ru": "фильм", "uk": "фільм", "en": "film"},
    "kind_film": {"ru": "фильм", "uk": "фільм", "en": "film"},
    "kind_series": {"ru": "сериал", "uk": "серіал", "en": "series"},
    "franchise_word": {"ru": "франшиза", "uk": "франшиза", "en": "franchise"},
    # --- старт, ввод
    "start_unknown": {"ru": "Эту страницу я пока не знаю. Напишите название — найду.",
                      "uk": "Цієї сторінки я поки не знаю. Напишіть назву — знайду.",
                      "en": "I don't know this page yet. Type a title and I'll find it."},
    "card_by_link": {"ru": "Карточка по ссылке 👇", "uk": "Картка за посиланням 👇", "en": "Card from the link 👇"},
    "find_prompt": {
        "ru": "Напишите название: например, <i>слизь</i> или <i>дом дракона</i>. Можно прислать ссылку на страницу HDREZKA.",
        "uk": "Напишіть назву: наприклад, <i>слиз</i> або <i>дім дракона</i>. Можна надіслати посилання на сторінку HDREZKA.",
        "en": "Type a title, e.g. <i>slime</i> or <i>house of the dragon</i>. You can also send a link to an HDREZKA page.",
    },
    "query_short": {
        "ru": "Напишите название хотя бы из двух букв — например «слизь» или «дом дракона». Или пришлите ссылку на страницу HDREZKA.",
        "uk": "Напишіть назву хоча б із двох літер — наприклад «слиз» або «дім дракона». Або надішліть посилання на сторінку HDREZKA.",
        "en": "Type at least two letters of a title — e.g. “slime” or “house of the dragon”. Or send a link to an HDREZKA page.",
    },
    "text_only": {"ru": "Я понимаю только текст: название сериала или ссылку на страницу HDREZKA.",
                  "uk": "Я розумію лише текст: назву серіалу або посилання на сторінку HDREZKA.",
                  "en": "I only understand text: a series title or a link to an HDREZKA page."},
    "searching": {"ru": "Ищу…", "uk": "Шукаю…", "en": "Searching…"},
    "searching_site": {"ru": "Ищу на сайте…", "uk": "Шукаю на сайті…", "en": "Searching the site…"},
    "reading_page": {"ru": "Читаю страницу…", "uk": "Читаю сторінку…", "en": "Reading the page…"},
    # --- выдача
    "parts_n": {"ru": ("{n} часть", "{n} части", "{n} частей"), "uk": ("{n} частина", "{n} частини", "{n} частин"),
                "en": ("{n} part", "{n} parts")},
    "ongoing_n": {"ru": ("выходит {n}", "выходят {n}", "выходят {n}"), "uk": ("виходить {n}", "виходять {n}", "виходять {n}"),
                  "en": ("{n} airing", "{n} airing")},
    "nothing_airing": {"ru": "ничего не выходит", "uk": "нічого не виходить", "en": "nothing airing"},
    "finished_word": {"ru": "завершён", "uk": "завершено", "en": "finished"},
    "btn_search_site": {"ru": "🔍 Искать на сайте", "uk": "🔍 Шукати на сайті", "en": "🔍 Search the site"},
    "found_n": {"ru": "Нашёл ({n}). Выберите:", "uk": "Знайшов ({n}). Виберіть:", "en": "Found {n}. Choose:"},
    "hidden_local": {"ru": "\n<i>Скрыто {n}: фильмы и тайтлы без вышедших серий.</i>",
                     "uk": "\n<i>Приховано {n}: фільми й тайтли без серій, що вийшли.</i>",
                     "en": "\n<i>{n} hidden: films and titles with no released episodes.</i>"},
    "query_stale": {"ru": "Запрос устарел — напишите название ещё раз.",
                    "uk": "Запит застарів — напишіть назву ще раз.",
                    "en": "The query has expired — type the title again."},
    "franchise_row": {"ru": "🎞 Франшиза «{name}»", "uk": "🎞 Франшиза «{name}»", "en": "🎞 Franchise “{name}”"},
    "nothing_airing_query": {"ru": "Сейчас ничего выходящего по этому запросу нет.",
                             "uk": "Зараз за цим запитом нічого не виходить.",
                             "en": "Nothing matching this query is airing right now."},
    "hidden_site": {"ru": " Скрыто {n}: фильмы и завершённые части франшиз.",
                    "uk": " Приховано {n}: фільми та завершені частини франшиз.",
                    "en": " {n} hidden: films and finished franchise parts."},
    "has_franchise_hint": {
        "ru": "\n\nЕсть франшиза: подпишитесь на неё — сообщу о новых сезонах, фильмах, спин-оффах.",
        "uk": "\n\nЄ франшиза: підпишіться на неї — повідомлю про нові сезони, фільми, спін-офи.",
        "en": "\n\nThere is a franchise: subscribe to it and I'll tell you about new seasons, films and spin-offs.",
    },
    "waiting_hint": {
        "ru": "\n\nСезон вышел целиком? Откройте карточку — там «🔔 Сообщить о продолжении».",
        "uk": "\n\nСезон вийшов повністю? Відкрийте картку — там «🔔 Повідомити про продовження».",
        "en": "\n\nSeason already complete? Open its card — there's “🔔 Notify about a sequel”.",
    },
    "link_hint": {
        "ru": "\n\nЕсли у тайтла есть франшиза — пришлите ссылку на любую его страницу, предложу подписку на всю франшизу.",
        "uk": "\n\nЯкщо в тайтлу є франшиза — надішліть посилання на будь-яку його сторінку, запропоную підписку на всю франшизу.",
        "en": "\n\nIf the title has a franchise, send a link to any of its pages and I'll offer a franchise subscription.",
    },
    "airing_now_n": {"ru": "Сейчас выходит ({n}). Выберите:", "uk": "Зараз виходить ({n}). Виберіть:", "en": "Airing now ({n}). Choose:"},
    # --- карточка страницы
    "head_wait_created": {"ru": "🔔 Сообщу о продолжении:", "uk": "🔔 Повідомлю про продовження:", "en": "🔔 I'll notify about a sequel:"},
    "head_waiting": {"ru": "🔔 Жду продолжения:", "uk": "🔔 Чекаю на продовження:", "en": "🔔 Waiting for a sequel:"},
    "head_subscribed_new": {"ru": "✅ Подписал:", "uk": "✅ Підписав:", "en": "✅ Subscribed:"},
    "head_in_subs": {"ru": "В подписках:", "uk": "У підписках:", "en": "In your subscriptions:"},
    "head_found": {"ru": "Найдено:", "uk": "Знайдено:", "en": "Found:"},
    "last_episode_finished": {"ru": "Последняя серия: {s}×{e} · <b>сериал завершён</b>",
                              "uk": "Остання серія: {s}×{e} · <b>серіал завершено</b>",
                              "en": "Last episode: {s}×{e} · <b>series finished</b>"},
    "now_airing": {"ru": "Сейчас: {s} сезон, {e} серия", "uk": "Зараз: {s} сезон, {e} серія", "en": "Now: season {s}, episode {e}"},
    "next_episode": {"ru": "Следующая серия: {d}", "uk": "Наступна серія: {d}", "en": "Next episode: {d}"},
    "wait_desc": {"ru": "Сообщу, когда появится продолжение: новый сезон, фильм или спин-офф.",
                  "uk": "Повідомлю, коли з'явиться продовження: новий сезон, фільм або спін-оф.",
                  "en": "I'll let you know when a sequel appears: a new season, film or spin-off."},
    "btn_stop_waiting": {"ru": "❌ Больше не ждать", "uk": "❌ Більше не чекати", "en": "❌ Stop waiting"},
    "voice_line": {"ru": "Озвучка: {v}", "uk": "Озвучка: {v}", "en": "Dub: {v}"},
    "btn_choose_voice": {"ru": "🎙 Выбрать озвучку", "uk": "🎙 Вибрати озвучку", "en": "🎙 Choose a dub"},
    "btn_unsubscribe": {"ru": "❌ Отписаться", "uk": "❌ Відписатися", "en": "❌ Unsubscribe"},
    "in_franchise_sub": {"ru": "Уже входит в вашу подписку на франшизу «{name}».",
                         "uk": "Уже входить до вашої підписки на франшизу «{name}».",
                         "en": "Already covered by your subscription to the “{name}” franchise."},
    "film_no_sub": {"ru": "Это фильм — на него подписаться нельзя, новых серий не будет.",
                    "uk": "Це фільм — на нього не підписатися, нових серій не буде.",
                    "en": "This is a film — no episodes to follow."},
    "finished_offer_wait": {"ru": "Сезон вышел целиком. Могу сообщить, когда появится продолжение.",
                            "uk": "Сезон вийшов повністю. Можу повідомити, коли з'явиться продовження.",
                            "en": "The season is complete. I can notify you when a sequel appears."},
    "btn_wait": {"ru": "🔔 Сообщить о продолжении", "uk": "🔔 Повідомити про продовження", "en": "🔔 Notify about a sequel"},
    "finished_in_franchise": {
        "ru": "Сезон вышел целиком — подписаться на него нельзя. О продолжении сообщит подписка на франшизу.",
        "uk": "Сезон вийшов повністю — підписатися на нього не можна. Про продовження повідомить підписка на франшизу.",
        "en": "The season is complete — it can't be followed. A franchise subscription will tell you about sequels.",
    },
    "btn_sub_season": {"ru": "➕ Подписаться на этот сезон", "uk": "➕ Підписатися на цей сезон", "en": "➕ Follow this season"},
    "btn_whole_franchise": {"ru": "🎞 Вся франшиза «{name}» ({n})", "uk": "🎞 Уся франшиза «{name}» ({n})", "en": "🎞 Whole franchise “{name}” ({n})"},
    "btn_schedule": {"ru": "📅 Расписание серий", "uk": "📅 Розклад серій", "en": "📅 Episode schedule"},
    "btn_open_site": {"ru": "▶ Открыть на сайте", "uk": "▶ Відкрити на сайті", "en": "▶ Open on the site"},
    "btn_share": {"ru": "🔗 Поделиться", "uk": "🔗 Поділитися", "en": "🔗 Share"},
    "voice_any_n": {"ru": "любая ({n} доступно)", "uk": "будь-яка ({n} доступно)", "en": "any ({n} available)"},
    "voice_any": {"ru": "любая", "uk": "будь-яка", "en": "any"},
    "max_subs": {"ru": "Не больше {n} подписок.", "uk": "Не більше {n} підписок.", "en": "No more than {n} subscriptions."},
    "card_stale": {"ru": "Карточка устарела — повторите поиск.", "uk": "Картка застаріла — повторіть пошук.", "en": "The card has expired — search again."},
    "cannot_sub": {"ru": "На это подписаться нельзя: фильм или завершённый сезон.",
                   "uk": "На це не підписатися: фільм або завершений сезон.",
                   "en": "This can't be followed: a film or a finished season."},
    # --- франшиза
    "st_finished": {"ru": "{kind} · завершён", "uk": "{kind} · завершено", "en": "{kind} · finished"},
    "st_airing": {"ru": "{kind} · идёт, {s}×{e}", "uk": "{kind} · триває, {s}×{e}", "en": "{kind} · airing, {s}×{e}"},
    "st_unread": {"ru": "ещё не смотрели", "uk": "ще не переглядали", "en": "not checked yet"},
    "fr_not_found": {"ru": "Франшиза не найдена.", "uk": "Франшизу не знайдено.", "en": "Franchise not found."},
    "fr_head": {"ru": "🎞 Франшиза <b>{name}</b> — {parts}", "uk": "🎞 Франшиза <b>{name}</b> — {parts}", "en": "🎞 Franchise <b>{name}</b> — {parts}"},
    "fr_subscribed": {"ru": "✅ Вы подписаны на всю франшизу.", "uk": "✅ Ви підписані на всю франшизу.", "en": "✅ You follow the whole franchise."},
    "fr_airing": {"ru": "\n<b>Сейчас выходят:</b>", "uk": "\n<b>Зараз виходять:</b>", "en": "\n<b>Airing now:</b>"},
    "fr_rest": {"ru": "\n<b>Остальные части:</b>", "uk": "\n<b>Інші частини:</b>", "en": "\n<b>Other parts:</b>"},
    "fr_more": {"ru": "  … и ещё {n}", "uk": "  … і ще {n}", "en": "  … and {n} more"},
    "fr_explain": {
        "ru": "\nПодписка на всю франшизу — это все выходящие сезоны плюс сообщения о новых частях: сезонах, фильмах, спин-оффах. Или выберите отдельные части ниже.",
        "uk": "\nПідписка на всю франшизу — це всі сезони, що виходять, плюс повідомлення про нові частини: сезони, фільми, спін-офи. Або виберіть окремі частини нижче.",
        "en": "\nA franchise subscription covers every airing season plus news about new parts: seasons, films, spin-offs. Or pick individual parts below.",
    },
    "btn_sub_franchise_all": {"ru": "✅ Подписаться на всю франшизу", "uk": "✅ Підписатися на всю франшизу", "en": "✅ Follow the whole franchise"},
    "btn_fr_card": {"ru": "📋 Карточка франшизы", "uk": "📋 Картка франшизи", "en": "📋 Franchise card"},
    "btn_back": {"ru": "« Назад", "uk": "« Назад", "en": "« Back"},
    "fc_head_new": {"ru": "✅ Подписал на франшизу", "uk": "✅ Підписав на франшизу", "en": "✅ Following the franchise"},
    "fc_head_in_subs": {"ru": "В подписках — франшиза", "uk": "У підписках — франшиза", "en": "In your subscriptions — franchise"},
    "fc_head": {"ru": "Франшиза", "uk": "Франшиза", "en": "Franchise"},
    "fc_airing": {"ru": "Сейчас выходят:", "uk": "Зараз виходять:", "en": "Airing now:"},
    "fc_desc": {"ru": "Сообщу о новых сериях, сезонах, фильмах и спин-оффах.",
                "uk": "Повідомлю про нові серії, сезони, фільми та спін-офи.",
                "en": "I'll tell you about new episodes, seasons, films and spin-offs."},
    "btn_sub_franchise": {"ru": "➕ Подписаться на франшизу", "uk": "➕ Підписатися на франшизу", "en": "➕ Follow the franchise"},
    "btn_fr_parts": {"ru": "🎞 Состав франшизы", "uk": "🎞 Склад франшизи", "en": "🎞 Franchise parts"},
    # --- расписание, календарь
    "sched_fr_head": {"ru": "📅 Франшиза <b>{name}</b>", "uk": "📅 Франшиза <b>{name}</b>", "en": "📅 Franchise <b>{name}</b>"},
    "btn_to_series": {"ru": "« К сериалу", "uk": "« До серіалу", "en": "« To the series"},
    "btn_to_franchise": {"ru": "« К франшизе", "uk": "« До франшизи", "en": "« To the franchise"},
    "sched_empty": {"ru": "Дат ближайших серий нет — либо сезон вышел целиком, либо сайт указывает только год.",
                    "uk": "Дат найближчих серій немає — або сезон вийшов повністю, або сайт вказує лише рік.",
                    "en": "No upcoming episode dates — either the season is complete or the site only lists a year."},
    "cal_no_subs": {"ru": "Подписок пока нет — календарь пуст. Напишите название сериала.",
                    "uk": "Підписок поки немає — календар порожній. Напишіть назву серіалу.",
                    "en": "No subscriptions yet — the calendar is empty. Type a series title."},
    "cal_empty": {
        "ru": "В ближайшие {n} дней по вашим подпискам серий не запланировано — или сайт не указывает точных дат (у части сериалов есть только год).",
        "uk": "У найближчі {n} днів за вашими підписками серій не заплановано — або сайт не вказує точних дат (у частини серіалів є лише рік).",
        "en": "No episodes scheduled for your subscriptions in the next {n} days — or the site gives no exact dates (some series only have a year).",
    },
    "cal_head": {"ru": "📅 <b>Ближайшие {n} дней</b>", "uk": "📅 <b>Найближчі {n} днів</b>", "en": "📅 <b>Next {n} days</b>"},
    # --- озвучки
    "sub_not_found": {"ru": "Подписка не найдена.", "uk": "Підписку не знайдено.", "en": "Subscription not found."},
    "voice_any_btn": {"ru": "Любая озвучка", "uk": "Будь-яка озвучка", "en": "Any dub"},
    "btn_done": {"ru": "« Готово", "uk": "« Готово", "en": "« Done"},
    "voices_hint": {
        "ru": "Отмечайте нужные озвучки — уведомлю, когда серия появится именно в них.\n«Любая» — сообщаю при первом появлении серии.",
        "uk": "Позначте потрібні озвучки — повідомлю, коли серія з'явиться саме в них.\n«Будь-яка» — повідомляю при першій появі серії.",
        "en": "Tick the dubs you want — I'll notify you when the episode appears in them.\n“Any” — I notify as soon as the episode first appears.",
    },
    "voices_unknown": {"ru": "Озвучек пока не знаю — страница ещё не прочитана. Попробуйте позже.",
                       "uk": "Озвучок поки не знаю — сторінку ще не прочитано. Спробуйте пізніше.",
                       "en": "I don't know the dubs yet — the page hasn't been read. Try later."},
    # --- мои подписки
    "my_empty": {"ru": "Подписок пока нет. Напишите название сериала или пришлите ссылку на тайтл.",
                 "uk": "Підписок поки немає. Напишіть назву серіалу або надішліть посилання на тайтл.",
                 "en": "No subscriptions yet. Type a series title or send a link."},
    "my_head": {"ru": "<b>Ваши подписки ({n}):</b>", "uk": "<b>Ваші підписки ({n}):</b>", "en": "<b>Your subscriptions ({n}):</b>"},
    "my_fr_line": {"ru": "🎞 <b>{name}</b> — франшиза, {parts}", "uk": "🎞 <b>{name}</b> — франшиза, {parts}", "en": "🎞 <b>{name}</b> — franchise, {parts}"},
    "my_airing": {"ru": "\n    выходит: {tail}", "uk": "\n    виходить: {tail}", "en": "\n    airing: {tail}"},
    "my_next": {"ru": " · след. {d}", "uk": " · наст. {d}", "en": " · next {d}"},
    "my_waiting": {"ru": " · завершён, жду продолжения", "uk": " · завершено, чекаю на продовження", "en": " · finished, waiting for a sequel"},
    "my_voice": {"ru": "\n    озвучка: {v}", "uk": "\n    озвучка: {v}", "en": "\n    dub: {v}"},
    "toast_unsubscribed": {"ru": "Отписал", "uk": "Відписав", "en": "Unsubscribed"},
    "toast_no_sub": {"ru": "Подписки уже нет", "uk": "Підписки вже немає", "en": "Already unsubscribed"},
    "btn_unsub_yes": {"ru": "🔕 Да, отписаться", "uk": "🔕 Так, відписатися", "en": "🔕 Yes, unsubscribe"},
    "btn_keep": {"ru": "Оставить", "uk": "Залишити", "en": "Keep"},
    "btn_unfollow": {"ru": "🔕 Не следить", "uk": "🔕 Не стежити", "en": "🔕 Unfollow"},
    "btn_unfollow_fr": {"ru": "🔕 Не следить за франшизой", "uk": "🔕 Не стежити за франшизою", "en": "🔕 Unfollow the franchise"},
    # --- новое
    "new_no_subs": {"ru": "Подписок пока нет. Напишите название — например, <i>слизь</i>.",
                    "uk": "Підписок поки немає. Напишіть назву — наприклад, <i>слиз</i>.",
                    "en": "No subscriptions yet. Type a title — e.g. <i>slime</i>."},
    "new_empty": {"ru": "За неделю новых серий по вашим подпискам не было.",
                  "uk": "За тиждень нових серій за вашими підписками не було.",
                  "en": "No new episodes for your subscriptions this week."},
    "new_head": {"ru": "🆕 <b>За неделю</b>", "uk": "🆕 <b>За тиждень</b>", "en": "🆕 <b>This week</b>"},
    "new_parts_head": {"ru": "\n<b>Новые части франшиз</b>", "uk": "\n<b>Нові частини франшиз</b>", "en": "\n<b>New franchise parts</b>"},
    # --- настройки
    "set_head": {"ru": "⚙️ <b>Настройки</b>", "uk": "⚙️ <b>Налаштування</b>", "en": "⚙️ <b>Settings</b>"},
    "on": {"ru": "вкл", "uk": "увімк", "en": "on"},
    "off": {"ru": "выкл", "uk": "вимк", "en": "off"},
    "set_photos": {"ru": "🖼 Картинки в уведомлениях: <b>{v}</b>", "uk": "🖼 Картинки у сповіщеннях: <b>{v}</b>", "en": "🖼 Pictures in notifications: <b>{v}</b>"},
    "set_quiet": {"ru": "🌙 Тихие часы: <b>{v}</b>", "uk": "🌙 Тихі години: <b>{v}</b>", "en": "🌙 Quiet hours: <b>{v}</b>"},
    "set_quiet_note": {"ru": " — ночью не пишу, отправлю утром", "uk": " — уночі не пишу, надішлю вранці", "en": " — no messages at night, delivered in the morning"},
    "set_tz": {"ru": "🕒 Часовой пояс: <b>{v}</b>", "uk": "🕒 Часовий пояс: <b>{v}</b>", "en": "🕒 Time zone: <b>{v}</b>"},
    "set_delivery": {"ru": "📨 Доставка: <b>{v}</b>", "uk": "📨 Доставка: <b>{v}</b>", "en": "📨 Delivery: <b>{v}</b>"},
    "delivery_digest": {"ru": "дайджест раз в день в {h:02d}:00", "uk": "дайджест раз на день о {h:02d}:00", "en": "daily digest at {h:02d}:00"},
    "delivery_now": {"ru": "сразу", "uk": "одразу", "en": "immediately"},
    "set_voice": {"ru": "🎙 Озвучка по умолчанию: <b>{v}</b> — для новых подписок",
                  "uk": "🎙 Озвучка за замовчуванням: <b>{v}</b> — для нових підписок",
                  "en": "🎙 Default dub: <b>{v}</b> — for new subscriptions"},
    "set_lang": {"ru": "🌐 Язык: <b>{v}</b>", "uk": "🌐 Мова: <b>{v}</b>", "en": "🌐 Language: <b>{v}</b>"},
    "btn_photos_on": {"ru": "🖼 Картинки: включить", "uk": "🖼 Картинки: увімкнути", "en": "🖼 Pictures: turn on"},
    "btn_photos_off": {"ru": "🖼 Картинки: выключить", "uk": "🖼 Картинки: вимкнути", "en": "🖼 Pictures: turn off"},
    "btn_quiet_on": {"ru": "🌙 Тихие часы: включить {f:02d}:00–{t:02d}:00", "uk": "🌙 Тихі години: увімкнути {f:02d}:00–{t:02d}:00", "en": "🌙 Quiet hours: turn on {f:02d}:00–{t:02d}:00"},
    "btn_quiet_off": {"ru": "🌙 Тихие часы: выключить", "uk": "🌙 Тихі години: вимкнути", "en": "🌙 Quiet hours: turn off"},
    "btn_quiet_edit": {"ru": "🌙 Изменить часы", "uk": "🌙 Змінити години", "en": "🌙 Change hours"},
    "btn_tz_minus": {"ru": "🕒 −1 ч", "uk": "🕒 −1 год", "en": "🕒 −1 h"},
    "btn_tz_plus": {"ru": "🕒 +1 ч", "uk": "🕒 +1 год", "en": "🕒 +1 h"},
    "btn_digest_now": {"ru": "📨 Присылать сразу", "uk": "📨 Надсилати одразу", "en": "📨 Send immediately"},
    "btn_digest": {"ru": "📨 Дайджест раз в день в {h}:00", "uk": "📨 Дайджест раз на день о {h}:00", "en": "📨 Daily digest at {h}:00"},
    "btn_default_voice": {"ru": "🎙 Озвучка по умолчанию", "uk": "🎙 Озвучка за замовчуванням", "en": "🎙 Default dub"},
    "btn_lang": {"ru": "🌐 Язык", "uk": "🌐 Мова", "en": "🌐 Language"},
    "tz_moscow": {"ru": "Москва", "uk": "Москва", "en": "Moscow"},
    "tz_kyiv_summer": {"ru": "Киев (лето)", "uk": "Київ (літо)", "en": "Kyiv (summer)"},
    "tz_kyiv_winter": {"ru": "Киев (зима)", "uk": "Київ (зима)", "en": "Kyiv (winter)"},
    "dv_head": {
        "ru": "🎙 <b>Озвучка по умолчанию</b>\nПрименяется к новым подпискам; у существующих меняется кнопкой 🎙 в «Мои подписки». Ниже — самые частые озвучки на сайте:",
        "uk": "🎙 <b>Озвучка за замовчуванням</b>\nЗастосовується до нових підписок; у наявних змінюється кнопкою 🎙 у «Мої підписки». Нижче — найчастіші озвучки на сайті:",
        "en": "🎙 <b>Default dub</b>\nApplies to new subscriptions; for existing ones use the 🎙 button in “My subscriptions”. Below are the most common dubs on the site:",
    },
    "btn_any": {"ru": "Любая", "uk": "Будь-яка", "en": "Any"},
    "quiet_head": {
        "ru": "🌙 <b>Тихие часы</b>\nС {f:02d}:00 до {t:02d}:00 по вашему поясу не пишу; накопившееся отправлю в {t:02d}:00.",
        "uk": "🌙 <b>Тихі години</b>\nЗ {f:02d}:00 до {t:02d}:00 за вашим поясом не пишу; накопичене надішлю о {t:02d}:00.",
        "en": "🌙 <b>Quiet hours</b>\nNo messages from {f:02d}:00 to {t:02d}:00 your time; anything pending is sent at {t:02d}:00.",
    },
    "quiet_from_label": {"ru": "с {h:02d}:00", "uk": "з {h:02d}:00", "en": "from {h:02d}:00"},
    "quiet_to_label": {"ru": "до {h:02d}:00", "uk": "до {h:02d}:00", "en": "until {h:02d}:00"},
    "btn_quiet_disable": {"ru": "Выключить тихие часы", "uk": "Вимкнути тихі години", "en": "Turn quiet hours off"},
    "lang_head": {"ru": "🌐 <b>Язык</b>\nВыберите язык бота:", "uk": "🌐 <b>Мова</b>\nВиберіть мову бота:", "en": "🌐 <b>Language</b>\nChoose the bot language:"},
    "lang_switched": {"ru": "Язык: Русский 🇷🇺", "uk": "Мова: Українська 🇺🇦", "en": "Language: English 🇬🇧"},
    # --- ошибки
    "error_msg": {"ru": "Что-то пошло не так. Попробуйте ещё раз чуть позже.",
                  "uk": "Щось пішло не так. Спробуйте ще раз трохи пізніше.",
                  "en": "Something went wrong. Try again a bit later."},
    "error_cb": {"ru": "Что-то пошло не так. Попробуйте ещё раз.", "uk": "Щось пішло не так. Спробуйте ще раз.", "en": "Something went wrong. Try again."},
    # --- уведомления (sender)
    "n_episode_body": {"ru": "{s} сезон, {e} серия", "uk": "{s} сезон, {e} серія", "en": "Season {s}, episode {e}"},
    "n_voice_suffix": {"ru": " · вышла в озвучке <b>{v}</b>", "uk": " · вийшла в озвучці <b>{v}</b>", "en": " · out in the <b>{v}</b> dub"},
    "btn_watch": {"ru": "▶ Смотреть {sxe}", "uk": "▶ Дивитися {sxe}", "en": "▶ Watch {sxe}"},
    "n_new_part": {"ru": "🆕 Новая часть франшизы «{f}»\n<b>{t}</b>", "uk": "🆕 Нова частина франшизи «{f}»\n<b>{t}</b>", "en": "🆕 New part of the “{f}” franchise\n<b>{t}</b>"},
    "n_new_part_line": {"ru": "• 🆕 «{f}»: <b>{t}</b>", "uk": "• 🆕 «{f}»: <b>{t}</b>", "en": "• 🆕 “{f}”: <b>{t}</b>"},
    "btn_open": {"ru": "▶ Открыть", "uk": "▶ Відкрити", "en": "▶ Open"},
    "btn_follow_part": {"ru": "➕ Следить за этой частью", "uk": "➕ Стежити за цією частиною", "en": "➕ Follow this part"},
    "btn_other_voices": {"ru": "🎙 Другие озвучки", "uk": "🎙 Інші озвучки", "en": "🎙 Other dubs"},
    "digest_episodes": {"ru": "🆕 Вышли новые серии", "uk": "🆕 Вийшли нові серії", "en": "🆕 New episodes are out"},
    "digest_parts": {"ru": "🆕 Новые части франшиз", "uk": "🆕 Нові частини франшиз", "en": "🆕 New franchise parts"},
}
