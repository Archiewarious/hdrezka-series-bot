"""Проверка доступа к сайту и источника событий. Запускать при смене IP, прокси или зеркала.

    python -m app.check_access
"""
import asyncio
import logging
import sys

from app.config import cfg
from app.rezka.client import AccessBlocked, RezkaClient
from app.rezka.parser import parse_updates


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print(f"зеркала : {cfg.base_urls}")
    print(f"прокси  : {cfg.proxy or 'нет (прямое подключение)'}")

    client = RezkaClient()
    try:
        items = parse_updates(await client.home())
        days = {i.day for i in items if i.day}
        print(f"  блок обновлений: событий={len(items)}, дней={len(days)}"
              + ("" if items else "  ПУСТО — вёрстка блока изменилась?"))
        for i in items[:3]:
            print(f"      id={i.hdrezka_id:>6} s{i.season}e{i.episode} {i.voice or ''} — {i.title[:40]}")
        if not items:
            return 1
        print("\nДоступ есть.")
        return 0
    except AccessBlocked as exc:
        print(f"\nДОСТУПА НЕТ: {exc}")
        print("Проверьте SSH-туннель:  ss -tln | grep 1080")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
