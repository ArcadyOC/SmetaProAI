"""Проверка готовой сметы (присланной файлом) на полноту и корректность.

Схема: модель только вытаскивает таблицу позиций из текста файла как есть,
ничего не считая. Дальше все проверки и пересчёт — код, без ИИ, по формуле
из smeta_compiler.py (см. .claude/skills/smeta-validator).
"""
from __future__ import annotations

import json
import os

import httpx

from openrouter import OPENROUTER_URL, api_key, raise_if_http_error
from search import connect, fetch_rate
from smeta_compiler import price_per_unit
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

EXTRACT_SYSTEM_PROMPT = """Тебе дан текст, извлечённый из файла со сметой (таблица позиций).

Извлеки построчно ВСЕ позиции сметы, какие найдёшь. Ничего не считай и не исправляй —
только достань то, что реально написано в тексте.

Для каждой позиции верни поля: full_code (шифр норматива как в документе), name (наименование),
unit (единица измерения), quantity (количество, число), price_per_unit (цена за единицу
с начислениями, число, если есть в документе), total (сумма по позиции, число, если есть).
Если поле не нашёл — null. Не выдумывай значения.

Ответь строго JSON-массивом без пояснений:
[{"full_code": "...", "name": "...", "unit": "...", "quantity": null, "price_per_unit": null, "total": null}]
"""

TOTAL_TOLERANCE_RATIO = 0.01
TOTAL_TOLERANCE_ABS = 1.0


class ValidatorError(RuntimeError):
    pass


async def extract_positions(raw_text: str) -> list[dict]:
    api_key_value = api_key()
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)

    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key_value}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                    {"role": "user", "content": raw_text[:12000]},
                ],
            },
        )
    raise_if_http_error(response)
    content = response.json()["choices"][0]["message"]["content"]

    try:
        cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValidatorError(f"Не смог разобрать таблицу позиций как JSON: {content!r}") from exc

    if not isinstance(result, list):
        raise ValidatorError("Модель вернула не список позиций.")
    return result


def _check_position(conn, pos: dict) -> dict:
    issues: list[str] = []
    full_code = pos.get("full_code")

    row = fetch_rate(conn, full_code) if full_code else None
    if row is None:
        issues.append("Шифр не найден в базе СН — проверить вручную, не выдуман ли или не устарел.")
        return {"full_code": full_code, "name": pos.get("name"), "issues": issues, "checked": False}

    submitted_name = (pos.get("name") or "").strip().lower()
    db_name = (row["name"] or "").strip().lower()
    if submitted_name and db_name and submitted_name not in db_name and db_name not in submitted_name:
        issues.append(f"Наименование не совпадает с базой: в смете «{pos.get('name')}», в базе «{row['name']}».")

    submitted_unit = (pos.get("unit") or "").strip().lower()
    db_unit = (row["unit"] or "").strip().lower()
    if submitted_unit and db_unit and submitted_unit != db_unit:
        issues.append(f"Единица измерения не совпадает: в смете «{pos.get('unit')}», в базе «{row['unit']}».")

    quantity = pos.get("quantity")
    submitted_total = pos.get("total")
    if quantity is not None and submitted_total is not None:
        zp = row["wages"] or 0.0
        em = row["machines_total"] or 0.0
        zpm = row["machines_wages"] or 0.0
        mr = row["materials"] or 0.0
        recomputed_total = price_per_unit(zp, em, mr, zpm, row["name"]) * quantity
        diff = abs(recomputed_total - submitted_total)
        tolerance = max(TOTAL_TOLERANCE_ABS, recomputed_total * TOTAL_TOLERANCE_RATIO)
        if diff > tolerance:
            issues.append(
                f"Сумма не сходится: в смете {submitted_total:.2f} руб., "
                f"по формуле должно быть {recomputed_total:.2f} руб."
            )
    else:
        issues.append("Не хватает количества или суммы в документе — пересчёт не проведён, сверить вручную.")

    return {"full_code": full_code, "name": row["name"], "issues": issues, "checked": True}


def validate_smeta(positions: list[dict]) -> dict:
    if not positions:
        return {"checks": [], "duplicates": [], "summary": "Не нашёл в файле ни одной позиции сметы."}

    conn = connect()
    try:
        checks = [_check_position(conn, pos) for pos in positions]
    finally:
        conn.close()

    codes = [c["full_code"] for c in checks if c["full_code"]]
    duplicates = sorted({c for c in codes if codes.count(c) > 1})

    ok_count = sum(1 for c in checks if c["checked"] and not c["issues"])
    problem_count = sum(1 for c in checks if c["issues"])

    summary = f"Проверено позиций: {len(checks)}. Без замечаний: {ok_count}. С замечаниями: {problem_count}."
    return {"checks": checks, "duplicates": duplicates, "summary": summary}


def format_validation_report(result: dict) -> str:
    lines = ["Проверка сметы:", result["summary"]]

    for c in result["checks"]:
        if c["issues"]:
            lines.append(f"\n{c['full_code'] or '(шифр не указан)'} — {c.get('name') or ''}")
            for issue in c["issues"]:
                lines.append(f"  • {issue}")

    if result["duplicates"]:
        lines.append("\nДубли позиций (один и тот же шифр встречается несколько раз):")
        for code in result["duplicates"]:
            lines.append(f"• {code}")

    if not any(c["issues"] for c in result["checks"]) and not result["duplicates"]:
        lines.append("\nЯвных проблем не нашёл. Это не значит, что состав работ полный — состав и охват задания сверь отдельно.")
    else:
        lines.append("\nЭто не окончательный вердикт — по местам с замечаниями лучше пройтись вместе.")

    return "\n".join(lines)
