"""Проверка доступа к сайту. Запускать при смене IP, прокси или зеркала.

    python -m app.check_access
"""
import asyncio, logging, sys

from app.config import cfg
from app.rezka.client import AccessBlocked, RezkaClient
from app.rezka.parser import parse_feed


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print(f"зеркала : {cfg.base_urls}")
    print(f"прокси  : {cfg.proxy or 'нет (прямое подключение)'}")

    client = RezkaClient()
    try:
        for section in cfg.feed_sections:
            items = parse_feed(await client.feed(section))
            with_eps = [i for i in items if i.has_episode]
            status = "OK" if items else "ПУСТО — верстка изменилась?"
            print(f"  {section:12} карточек={len(items):3} с сериями={len(with_eps):3}  {status}")
            for i in with_eps[:3]:
                print(f"      id={i.hdrezka_id:>6} s{i.season}e{i.episode} — {i.title[:40]}")
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
