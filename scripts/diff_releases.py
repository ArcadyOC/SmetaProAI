"""Compare two SN-2012 releases and report what changed between quarters."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import mosru

PRICE_LEVEL_LEN = len("01.07.2026")


def resolve_db(value: str) -> Path:
    if len(value) == PRICE_LEVEL_LEN and value.count(".") == 2:
        return mosru.db_path_for(value)
    return Path(value)


def load_rates(db: Path) -> dict[str, tuple[str, str, float | None]]:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT full_code, name, unit, direct_cost FROM rates WHERE full_code IS NOT NULL"
    ).fetchall()
    conn.close()
    return {code: (name or "", unit or "", cost) for code, name, unit, cost in rows}


def load_materials(db: Path) -> dict[str, tuple[str, str, float | None]]:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        """
        SELECT d.collection_code || ':' || m.pos_code, m.name, m.unit, m.price
        FROM materials m JOIN documents d ON d.id = m.document_id
        """
    ).fetchall()
    conn.close()
    return {code: (name or "", unit or "", price) for code, name, unit, price in rows}


def price_level(db: Path) -> str:
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT value FROM meta WHERE key='price_level'").fetchone()
    conn.close()
    return row[0] if row else db.stem


def money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ")


def compare(old: dict, new: dict) -> dict:
    old_keys, new_keys = set(old), set(new)
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    changed: list[tuple[str, str, float, float, float]] = []
    unchanged = 0
    for key in sorted(old_keys & new_keys):
        old_price, new_price = old[key][2], new[key][2]
        if old_price is None or new_price is None:
            continue
        if abs(new_price - old_price) < 0.005:
            unchanged += 1
            continue
        pct = (new_price / old_price - 1) * 100 if old_price else 0.0
        changed.append((key, new[key][0], old_price, new_price, pct))
    return {"added": added, "removed": removed, "changed": changed, "unchanged": unchanged}


def section(title: str, result: dict, source_new: dict, limit: int) -> list[str]:
    changed = result["changed"]
    growth = [c[4] for c in changed]
    avg = sum(growth) / len(growth) if growth else 0.0
    lines = [
        f"## {title}",
        "",
        f"- добавлено: {len(result['added'])}",
        f"- удалено: {len(result['removed'])}",
        f"- изменилась цена: {len(changed)}",
        f"- без изменений: {result['unchanged']}",
        f"- средний рост цены: {avg:+.2f}%",
        "",
    ]
    if changed:
        lines += [
            f"### Наибольшие изменения цены (топ {limit})",
            "",
            "| Шифр | Наименование | Было | Стало | Δ |",
            "| --- | --- | --- | --- | --- |",
        ]
        for code, name, old_price, new_price, pct in sorted(changed, key=lambda c: -abs(c[4]))[:limit]:
            short = (name[:70] + "…") if len(name) > 70 else name
            lines.append(
                f"| `{code}` | {short} | {money(old_price)} | {money(new_price)} | {pct:+.1f}% |"
            )
        lines.append("")
    if result["added"]:
        lines += [f"### Новые позиции (первые {limit})", ""]
        for code in result["added"][:limit]:
            name = source_new[code][0]
            short = (name[:80] + "…") if len(name) > 80 else name
            lines.append(f"- `{code}` — {short}")
        lines.append("")
    if result["removed"]:
        lines += [f"### Убранные позиции (первые {limit})", ""]
        for code in result["removed"][:limit]:
            lines.append(f"- `{code}`")
        lines.append("")
    return lines


def main() -> int:
    mosru.use_utf8_stdout()
    parser = argparse.ArgumentParser(description="Сравнить два выпуска СН-2012")
    parser.add_argument("--old", required=True, help="Путь к базе или уровень цен, например 01.04.2026")
    parser.add_argument("--new", required=True, help="Путь к базе или уровень цен, например 01.07.2026")
    parser.add_argument("--out", type=Path, help="Куда писать отчёт (по умолчанию data/reports/)")
    parser.add_argument("--limit", type=int, default=30, help="Сколько позиций показывать в каждом списке")
    args = parser.parse_args()

    old_db, new_db = resolve_db(args.old), resolve_db(args.new)
    for db in (old_db, new_db):
        if not db.exists():
            print(f"Нет базы: {db}")
            return 2

    old_level, new_level = price_level(old_db), price_level(new_db)
    rates = compare(load_rates(old_db), load_rates(new_db))
    materials = compare(load_materials(old_db), load_materials(new_db))

    lines = [
        f"# Изменения СН-2012: {old_level} → {new_level}",
        "",
        f"Сравнение баз `{old_db.name}` и `{new_db.name}`.",
        "",
    ]
    lines += section("Расценки", rates, load_rates(new_db), args.limit)
    lines += section("Материалы (глава 21)", materials, load_materials(new_db), args.limit)

    out = args.out or mosru.REPORTS / f"changes_{mosru.price_level_to_slug(new_level)}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    print(f"Отчёт: {out}")
    print(
        f"Расценки: +{len(rates['added'])} / -{len(rates['removed'])} / "
        f"цена изменилась у {len(rates['changed'])}"
    )
    print(
        f"Материалы: +{len(materials['added'])} / -{len(materials['removed'])} / "
        f"цена изменилась у {len(materials['changed'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
