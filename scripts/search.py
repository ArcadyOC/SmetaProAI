"""Search the SN-2012 SQLite database (newest built release by default)."""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_DIR = ROOT / "data" / "db"


def newest_db() -> Path | None:
    candidates = sorted(DB_DIR.glob("sn2012_*.sqlite"))
    if candidates:
        return candidates[-1]
    legacy = ROOT / "data" / "sn2012_2026.sqlite"
    return legacy if legacy.exists() else None


def connect() -> sqlite3.Connection:
    db = newest_db()
    if db is None or not db.exists():
        raise FileNotFoundError("Базы нет. Соберите её: python scripts/build_db.py")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ")


def fetch_rate(conn: sqlite3.Connection, full_code: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT full_code, name, unit, wages, machines_total, machines_wages, materials, labor_hours
        FROM rates WHERE full_code = ?
        """,
        (full_code,),
    ).fetchone()


STOPWORDS = {
    "или", "при", "над", "под", "для", "это", "как", "что", "чтобы", "надо",
    "нужно", "сделать", "сделай", "смета", "сметы", "пожалуйста", "мне", "нам",
    "хочу", "составить", "посчитай", "посчитать", "только", "работа", "работы",
    "штук", "метр", "метра", "метров", "кв", "куб", "около", "примерно", "весь",
    "вся", "все", "там", "тут", "есть", "нет", "уже", "ещё", "еще", "надо",
}


def stems(query: str) -> list[str]:
    """Основы слов: в сборнике «сосулек», а спрашивают «сосульки» — ищем по началу слова."""
    out: list[str] = []
    for word in re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", query):
        low = word.lower().replace("ё", "е")
        if len(low) < 3 or low.isdigit() or low in STOPWORDS:
            continue
        stem = low if len(low) <= 4 else low[: max(4, len(low) - 3)]
        if stem not in out:
            out.append(stem)
    return out


def fetch_rates(conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    parts = stems(query)
    if not parts:
        return []
    fts = " OR ".join(f"{part}*" for part in parts)
    try:
        rows = conn.execute(
            """
            SELECT r.full_code, r.name, r.unit, r.direct_cost, r.wages, r.labor_hours, r.table_name
            FROM rates_fts f
            JOIN rates r ON r.id = f.rowid
            WHERE rates_fts MATCH ?
            ORDER BY f.rank, (r.direct_cost IS NULL)
            LIMIT ?
            """,
            (fts, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        where = " OR ".join(["name LIKE ?", "table_name LIKE ?", "full_code LIKE ?"] * len(parts))
        params: list[str] = []
        for part in parts:
            params += [f"%{part}%"] * 3
        rows = conn.execute(
            f"""
            SELECT full_code, name, unit, direct_cost, wages, labor_hours, table_name
            FROM rates
            WHERE {where}
            ORDER BY (direct_cost IS NULL), full_code
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return rows


def search_rates(conn: sqlite3.Connection, query: str, limit: int) -> None:
    rows = fetch_rates(conn, query, limit)
    print(f"Расценки: {len(rows)}")
    for r in rows:
        print(
            f"{r['full_code']}\n  {r['name']}\n  "
            f"{r['unit'] or '—'} | прямые {fmt_money(r['direct_cost'])} руб. | "
            f"ЗП {fmt_money(r['wages'])} | {r['labor_hours'] or '—'} чел.-ч"
        )


def search_materials(conn: sqlite3.Connection, query: str, limit: int) -> None:
    like = f"%{query}%"
    rows = conn.execute(
        """
        SELECT m.pos_code, m.okp, m.okpd2, m.name, m.unit, m.price, d.collection_code
        FROM materials m
        JOIN documents d ON d.id = m.document_id
        WHERE m.pos_code LIKE ? OR m.okp LIKE ? OR m.okpd2 LIKE ? OR m.name LIKE ?
        LIMIT ?
        """,
        (like, like, like, like, limit),
    ).fetchall()
    print(f"Материалы: {len(rows)}")
    for r in rows:
        print(
            f"{r['collection_code']} {r['pos_code']}  {r['name']}\n"
            f"  {r['unit'] or '—'} | {fmt_money(r['price'])} руб. | ОКП {r['okp']} | ОКПД2 {r['okpd2']}"
        )


def search_machines(conn: sqlite3.Connection, query: str, limit: int) -> None:
    like = f"%{query}%"
    rows = conn.execute(
        """
        SELECT pos_code, code, name, price_mach_hour, wages
        FROM machines
        WHERE pos_code LIKE ? OR code LIKE ? OR name LIKE ?
        LIMIT ?
        """,
        (like, like, like, limit),
    ).fetchall()
    print(f"Машины: {len(rows)}")
    for r in rows:
        print(
            f"{r['pos_code']} ({r['code']})  {r['name']}\n"
            f"  {fmt_money(r['price_mach_hour'])} руб./маш.-ч | ЗП {fmt_money(r['wages'])}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Поиск по базе СН-2012")
    parser.add_argument("query")
    parser.add_argument("--kind", choices=["rates", "materials", "machines", "all"], default="rates")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    conn = connect()
    if args.kind in {"rates", "all"}:
        search_rates(conn, args.query, args.limit)
    if args.kind in {"materials", "all"}:
        search_materials(conn, args.query, args.limit)
    if args.kind in {"machines", "all"}:
        search_machines(conn, args.query, args.limit)


if __name__ == "__main__":
    main()
