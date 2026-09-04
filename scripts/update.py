"""Quarterly update: find a new SN-2012 release, download it, rebuild, diff.

SN-2012 is reissued every quarter (01.01, 01.04, 01.07, 01.10). Run this after
a quarter starts:

    python scripts/update.py            # проверить и обновить
    python scripts/update.py --check    # только проверить, ничего не менять
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime

import mosru
import fetch_release

SCRIPTS = mosru.ROOT / "scripts"


def run(script: str, *script_args: str) -> int:
    cmd = [sys.executable, str(SCRIPTS / script), *script_args]
    print(f"\n$ {' '.join(cmd[1:])}", flush=True)
    return subprocess.call(cmd, cwd=mosru.ROOT)


def previous_level(state: dict, current: str) -> str | None:
    levels = sorted(
        (r["price_level"] for r in state.get("releases", []) if r.get("price_level") != current),
        key=mosru.price_level_to_slug,
    )
    return levels[-1] if levels else None


def main() -> int:
    mosru.use_utf8_stdout()
    parser = argparse.ArgumentParser(description="Квартальное обновление базы СН-2012")
    parser.add_argument("--check", action="store_true", help="Только проверить наличие нового выпуска")
    parser.add_argument("--doc-id", type=int, help="Взять конкретный документ mos.ru")
    parser.add_argument("--since", help="Искать документы с даты YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="Перекачать и пересобрать известный выпуск")
    parser.add_argument("--no-diff", action="store_true", help="Не сравнивать с прошлым кварталом")
    args = parser.parse_args()

    state = mosru.read_releases()
    known = mosru.known_levels(state)
    print(f"Известные выпуски: {', '.join(sorted(known, key=mosru.price_level_to_slug)) or 'нет'}")

    if args.doc_id:
        release = mosru.load_release(args.doc_id)
    else:
        since = (
            datetime.strptime(args.since, "%Y-%m-%d")
            if args.since
            else mosru.last_published(state) or fetch_release.DEFAULT_SINCE
        )
        print(f"Ищу выпуски, опубликованные с {since:%d.%m.%Y}…")
        release = mosru.latest_release(since)

    if release is None:
        print("Нового выпуска на mos.ru нет — данные актуальны.")
        return 0

    print(release.describe())
    if not release.is_plausible:
        print("Документ не похож на полный сборник — остановился, данные не тронуты.")
        return 2

    is_new = release.price_level not in known
    if not is_new and not args.force:
        print(f"Выпуск {release.price_level} уже в базе. Для пересборки добавьте --force.")
        return 0
    if args.check:
        print(f"Доступен новый выпуск: {release.price_level}. Запустите без --check, чтобы обновить.")
        return 0

    previous = previous_level(state, release.price_level) if is_new else None

    fetch_args = ["--doc-id", str(release.document_id)]
    if args.force:
        fetch_args.append("--force")
    if code := run("fetch_release.py", *fetch_args):
        return code
    if code := run("build_db.py"):
        return code

    if previous and not args.no_diff:
        old_db = mosru.db_path_for(previous)
        if old_db.exists():
            run("diff_releases.py", "--old", previous, "--new", release.price_level)
        else:
            print(f"\nБазы прошлого квартала ({previous}) нет — сравнение пропущено.")

    print(f"\nОбновление завершено. Текущий уровень цен: {release.price_level}")
    print(f"База: {mosru.db_path_for(release.price_level)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
