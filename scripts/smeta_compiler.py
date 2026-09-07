"""Сборка чернового документа сметы из подобранных позиций.

Формула и проценты — из Приложения 2/3 к Общим положениям (см. .claude/skills/smeta-compiler).
НР от ЗП = 70%, НП от ЗП = 10%, НР+НП от ЗПМ = 108% (78%+30%). Ничего сверх этого не придумывается.
"""
from __future__ import annotations

import re

from search import connect, fetch_rate

NR_ZP_RATE = 0.70
NR_ZP_RATE_ROAD_MARKING = 0.80
NP_ZP_RATE = 0.10
NR_NP_ZPM_RATE = 1.08

QUANTITY_PATTERN = re.compile(r"\d+(?:[.,]\d+)?")


def nr_zp_rate_for(name: str | None) -> float:
    """НР от ЗП — 80% для дорожной разметки, иначе 70% (п.10 Общих положений)."""
    if name and "размет" in name.lower():
        return NR_ZP_RATE_ROAD_MARKING
    return NR_ZP_RATE


def price_per_unit(zp: float, em: float, mr: float, zpm: float, name: str | None) -> float:
    nr_zp = zp * nr_zp_rate_for(name)
    np_zp = zp * NP_ZP_RATE
    nr_np_zpm = zpm * NR_NP_ZPM_RATE
    return zp + em + mr + nr_zp + np_zp + nr_np_zpm


def extract_quantity(volume_text: str | None) -> float | None:
    if not volume_text:
        return None
    match = QUANTITY_PATTERN.search(volume_text)
    if not match:
        return None
    return float(match.group().replace(",", "."))


def compile_smeta(task: dict, selected: list[dict]) -> dict:
    """selected — список {"full_code", "reason", ...} от rate_picker.pick_rates."""
    if not selected:
        return {"positions": [], "total": 0.0, "warnings": ["Нет подобранных позиций — считать нечего."]}

    quantity = extract_quantity(task.get("volume"))
    warnings = []
    if quantity is None:
        warnings.append(
            "Не смог определить количество единиц из «{}» — посчитал по 1 ед., уточни объём."
            .format(task.get("volume"))
        )
        quantity = 1.0

    conn = connect()
    try:
        positions = []
        total_smeta = 0.0
        for idx, item in enumerate(selected, start=1):
            row = fetch_rate(conn, item["full_code"])
            if row is None:
                warnings.append(f"Позиция {item['full_code']} не найдена в базе при сборке — пропущена.")
                continue

            zp = row["wages"] or 0.0
            em = row["machines_total"] or 0.0
            zpm = row["machines_wages"] or 0.0
            mr = row["materials"] or 0.0
            labor = row["labor_hours"] or 0.0

            if row["wages"] is None and row["machines_total"] is None and row["materials"] is None:
                warnings.append(f"У позиции {row['full_code']} нет цены в базе — сумма будет 0, проверь вручную.")

            total_per_unit = price_per_unit(zp, em, mr, zpm, row["name"])
            total_position = total_per_unit * quantity
            ztr_position = labor * quantity
            total_smeta += total_position

            positions.append(
                {
                    "no": idx,
                    "full_code": row["full_code"],
                    "name": row["name"],
                    "unit": row["unit"],
                    "quantity": quantity,
                    "price_per_unit": total_per_unit,
                    "total": total_position,
                    "ztr": ztr_position,
                    "reason": item.get("reason"),
                }
            )
    finally:
        conn.close()

    return {"positions": positions, "total": total_smeta, "warnings": warnings}


def format_smeta_text(compiled: dict) -> str:
    if not compiled["positions"]:
        lines = ["Смету составить не из чего."]
    else:
        lines = ["Черновик сметы:"]
        for p in compiled["positions"]:
            lines.append(
                f"{p['no']}. {p['full_code']} — {p['name']} ({p['unit'] or '—'})\n"
                f"   Кол-во: {p['quantity']:g} | Цена за ед. с начислениями: {p['price_per_unit']:.2f} руб. "
                f"| Сумма: {p['total']:.2f} руб. | ЗТР: {p['ztr']:.2f} чел.-ч"
            )
        lines.append(f"\nИТОГО по смете: {compiled['total']:.2f} руб. (без учёта неучтённых нормативом материалов)")

    if compiled["warnings"]:
        lines.append("\nОбрати внимание:")
        for w in compiled["warnings"]:
            lines.append(f"• {w}")

    return "\n".join(lines)
