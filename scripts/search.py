"""Search the SN-2012 SQLite database (newest built release by default)."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import mosru


def newest_db() -> Path | None:
    candidates = sorted(mosru.DB_DIR.glob("sn2012_*.sqlite"))
    return candidates[-1] if candidates else None


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ")


def search_rates(conn: sqlite3.Connection, query: str, limit: int) -> None:
    like = f"%{query}%"
    fts = " ".join(f"{part}*" for part in query.split() if part)
    try:
        rows = conn.execute(
            """
            SELECT r.full_code, r.name, r.unit, r.direct_cost, r.wages, r.labor_hours, r.table_name
            FROM rates_fts f
            JOIN rates r ON r.id = f.rowid
            WHERE rates_fts MATCH ?
            ORDER BY (r.direct_cost IS NULL), r.full_code
            LIMIT ?
            """,
            (fts, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        rows = conn.execute(
            """
            SELECT full_code, name, unit, direct_cost, wages, labor_hours, table_name
            FROM rates
            WHERE full_code LIKE ? OR code LIKE ? OR name LIKE ? OR table_name LIKE ?
            ORDER BY (direct_cost IS NULL), full_code
            LIMIT ?
            """,
            (like, like, like, like, limit),
        ).fetchall()
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


def main() -> int:
    mosru.use_utf8_stdout()
    parser = argparse.ArgumentParser(description="Поиск по базе СН-2012")
    parser.add_argument("query")
    parser.add_argument("--kind", choices=["rates", "materials", "machines", "all"], default="rates")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--db", type=Path, help="Конкретная база (по умолчанию самая свежая в data/db)")
    args = parser.parse_args()

    db = args.db or newest_db()
    if db is None or not db.exists():
        print("Базы нет. Соберите её: python scripts/build_db.py")
        return 2

    conn = connect(db)
    level = conn.execute("SELECT value FROM meta WHERE key='price_level'").fetchone()
    print(f"База: {db.name} (уровень цен {level[0] if level else '?'})\n")
    if args.kind in {"rates", "all"}:
        search_rates(conn, args.query, args.limit)
    if args.kind in {"materials", "all"}:
        search_materials(conn, args.query, args.limit)
    if args.kind in {"machines", "all"}:
        search_machines(conn, args.query, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
