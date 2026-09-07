"""Разбор текстового задания на смету через OpenRouter (модель DeepSeek)."""
from __future__ import annotations

import logging
import os

import httpx

from openrouter import OPENROUTER_URL, api_key, extract_json_object, raise_if_http_error

DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

SYSTEM_PROMPT = (
    "Ты помогаешь разобрать задание на составление сметы по сборнику СН-2012. "
    "Пользователь пишет как угодно: разговорно, с опечатками, голосом, длинно или коротко. "
    "Пойми смысл и верни только JSON-объект:\n"
    '{"work_type": "...", "volume": "...", "conditions": "...", '
    '"search_queries": ["...", "..."]}.\n'
    "work_type — суть работ обычными словами.\n"
    "volume — число и единица, если названы, иначе null.\n"
    "conditions — особые условия (вышка, зима, только днём и т.п.), иначе null.\n"
    "search_queries — 3–8 коротких поисковых фраз, как их пишут в сборнике СН: "
    "существительные и устойчивые словосочетания (сосульки, очистка кровли, снег, наледь), "
    "без слов вроде «надо», «сделай», «смета». Не выдумывай шифры расценок."
)


class TaskParseError(RuntimeError):
    pass


async def parse_task(text: str) -> dict:
    api_key_value = api_key()
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key_value}"},
            json={**payload, "response_format": {"type": "json_object"}},
        )
        if response.status_code >= 400:
            response = await client.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key_value}"},
                json=payload,
            )
    raise_if_http_error(response)
    message = response.json()["choices"][0]["message"]
    raw = message.get("content")
    try:
        parsed = extract_json_object(raw)
    except ValueError:
        logging.warning("parse_task content failed, raw=%r", raw)
        try:
            parsed = extract_json_object(message.get("reasoning"))
        except ValueError as exc:
            raise TaskParseError(str(exc)) from exc

    queries = parsed.get("search_queries") or []
    if isinstance(queries, str):
        queries = [queries]
    parsed["search_queries"] = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    parsed["original_text"] = text
    if not parsed.get("work_type"):
        parsed["work_type"] = text
    return parsed
