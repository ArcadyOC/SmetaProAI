"""Сохранение черновика сметы в reports/ по правилам нейминга проекта."""
from __future__ import annotations

import datetime
import re
from pathlib import Path

REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(text: str | None) -> str:
    if not text:
        return "zadanie"
    lowered = text.lower()
    translit = "".join(_TRANSLIT.get(ch, ch) for ch in lowered)
    slug = re.sub(r"[^a-z0-9]+", "-", translit).strip("-")
    return slug or "zadanie"


def build_report_markdown(task: dict, compiled: dict) -> str:
    lines = [
        f"# Черновик сметы — {task.get('work_type') or 'без названия'}",
        "",
        f"Дата составления: {datetime.date.today().isoformat()}",
        f"Объём: {task.get('volume') or 'не указан'}",
        f"Условия: {task.get('conditions') or 'не указаны'}",
        "",
        "| № | Шифр | Наименование | Ед. изм. | Кол-во | Цена за ед. с начислениями | Сумма | ЗТР, чел.-ч |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for p in compiled["positions"]:
        lines.append(
            f"| {p['no']} | {p['full_code']} | {p['name']} | {p['unit'] or '—'} | "
            f"{p['quantity']:g} | {p['price_per_unit']:.2f} | {p['total']:.2f} | {p['ztr']:.2f} |"
        )
    lines.append("")
    lines.append(f"**ИТОГО: {compiled['total']:.2f} руб.** (без учёта неучтённых нормативом материалов)")

    if compiled["warnings"]:
        lines.append("")
        lines.append("## Обрати внимание")
        for w in compiled["warnings"]:
            lines.append(f"- {w}")

    lines.append("")
    lines.append("_Черновик. Не отправлять заказчику без отдельного подтверждения._")
    return "\n".join(lines)


def save_report(task: dict, compiled: dict) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.date.today().isoformat()
    slug = slugify(task.get("work_type"))
    filename = f"{date_str}-{slug}-smeta.md"
    path = REPORTS_DIR / filename

    counter = 2
    while path.exists():
        path = REPORTS_DIR / f"{date_str}-{slug}-smeta-{counter}.md"
        counter += 1

    path.write_text(build_report_markdown(task, compiled), encoding="utf-8")
    return path
