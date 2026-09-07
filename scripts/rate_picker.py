"""Подбор расценок СН под разобранное задание.

Двухступенчатая схема:
1. Поиск кандидатов в базе (детерминированно, без ИИ) — scripts/search.py.
2. Отбор и обоснование моделью (DeepSeek через OpenRouter) — строго из
   присланных кандидатов, без права придумывать шифры.
"""
from __future__ import annotations

import os
import re

import httpx

from openrouter import OPENROUTER_URL, api_key, extract_json_object, raise_if_http_error
from search import STOPWORDS, connect, fetch_rates

DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
CANDIDATE_LIMIT = 40

SYSTEM_PROMPT = """Ты — эксперт по сметному делу, подбираешь расценки из сборника СН-2012 \
(база про эксплуатацию/ремонт зданий и сооружений, не про новое строительство).

Тебе даётся исходная фраза пользователя (она может быть разговорной) и список кандидатов-расценок, \
уже найденных в базе (шифр full_code, название, ед. изм., прямые затраты).

Правила:
1. Выбирай позиции ТОЛЬКО из присланного списка кандидатов. Никогда не придумывай shifr \
(full_code), которого нет в списке.
2. Расценка должна реально покрывать нужные операции по названию, а не просто быть похожей.
3. Если задание описывает несколько этапов (осмотр → ТО → ремонт → замена) — учти все нужные, \
а не только один.
4. Проверь, не нужны ли отдельные позиции, которые норматив НЕ покрывает: подъёмная техника, \
вертикальный транспорт материалов, сами материалы для замены (трубы, кабели и т.п.), возврат \
материалов от разборки. Если задание похоже на такой случай — предупреди об этом в warnings.
5. Если для демонтажа/разборки нет прямого норматива — можно предложить норматив на монтаж \
того же элемента с явной пометкой в reason "коэффициент 0,2 к ЗП и эксплуатации машин (демонтаж)".
6. Если среди кандидатов нет ничего подходящего — верни пустой selected и объясни в note.
7. Никогда не выдумывай суммы, названия или составы работ, которых нет в кандидатах.

Ответь строго в формате JSON, без пояснений вне JSON:
{"selected": [{"full_code": "...", "reason": "..."}], "warnings": ["..."], "note": "..."}
"""


class RatePickError(RuntimeError):
    pass


def _search_queries(task: dict) -> list[str]:
    queries = [q for q in (task.get("search_queries") or []) if isinstance(q, str) and q.strip()]
    if queries:
        return queries
    blob = " ".join(
        part for part in (task.get("work_type"), task.get("original_text")) if part
    )
    tokens = [
        token
        for token in re.findall(r"[0-9A-Za-zА-Яа-яЁё/-]{3,}", blob, flags=re.I)
        if token.lower() not in STOPWORDS
    ]
    return tokens[:8]


def _collect_candidates(task: dict):
    seen: dict[str, object] = {}
    conn = connect()
    try:
        for query in _search_queries(task):
            for row in fetch_rates(conn, query, 20):
                seen[row["full_code"]] = row
                if len(seen) >= CANDIDATE_LIMIT:
                    return list(seen.values())
    finally:
        conn.close()
    return list(seen.values())


def _format_candidates(rows) -> str:
    lines = []
    for r in rows:
        cost = f"{r['direct_cost']:.2f}" if r["direct_cost"] is not None else "—"
        lines.append(f"{r['full_code']} | {r['name']} | {r['unit'] or '—'} | {cost} руб.")
    return "\n".join(lines)


async def pick_rates(task: dict) -> dict:
    """Возвращает {"selected": [{"full_code", "reason"}], "warnings": [...], "note": ..., "dropped": [...]}."""
    rows = _collect_candidates(task)
    if not rows:
        hint = ", ".join(_search_queries(task)[:5]) or "пустой запрос"
        return {
            "selected": [],
            "warnings": [],
            "note": f"Не нашёл в базе СН расценок по запросам: {hint}.",
            "dropped": [],
        }

    api_key_value = api_key()
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)

    candidates_text = _format_candidates(rows)
    known_codes = {r["full_code"] for r in rows}
    by_code = {r["full_code"]: r for r in rows}
    original = task.get("original_text") or task.get("work_type") or ""

    user_content = (
        f"Исходное задание пользователя:\n{original}\n\n"
        f"Разобранное:\nВид работ: {task.get('work_type')}\n"
        f"Объём: {task.get('volume')}\nУсловия: {task.get('conditions')}\n\n"
        f"Кандидаты из базы СН:\n{candidates_text}"
    )

    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key_value}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            },
        )
    raise_if_http_error(response)
    message = response.json()["choices"][0]["message"]
    try:
        result = extract_json_object(message.get("content"))
    except ValueError:
        try:
            result = extract_json_object(message.get("reasoning"))
        except ValueError as exc:
            raise RatePickError(str(exc)) from exc

    selected = []
    for s in result.get("selected", []):
        row = by_code.get(s.get("full_code"))
        if row is None:
            continue
        selected.append(
            {
                "full_code": row["full_code"],
                "name": row["name"],
                "unit": row["unit"],
                "reason": s.get("reason"),
            }
        )
    dropped = [s for s in result.get("selected", []) if s.get("full_code") not in known_codes]

    return {
        "selected": selected,
        "warnings": result.get("warnings") or [],
        "note": result.get("note"),
        "dropped": dropped,
    }


def format_picked_text(result: dict) -> str:
    lines = []
    if result["selected"]:
        lines.append("Подобранные позиции:")
        for s in result["selected"]:
            lines.append(f"• {s['full_code']} — {s.get('name') or ''} ({s.get('unit') or '—'})")
            if s.get("reason"):
                lines.append(f"  Почему: {s['reason']}")
    else:
        lines.append("Не смог однозначно подобрать позиции из найденных кандидатов.")

    if result["warnings"]:
        lines.append("\nЧто не учтено нормативом — проверь отдельно:")
        for w in result["warnings"]:
            lines.append(f"• {w}")

    if result["dropped"]:
        lines.append(
            "\n(Модель предложила позиции вне списка кандидатов — отброшены как неподтверждённые.)"
        )

    if result.get("note"):
        lines.append(f"\n{result['note']}")

    return "\n".join(lines)
