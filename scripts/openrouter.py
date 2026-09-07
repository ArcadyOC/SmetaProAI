"""Общий доступ к OpenRouter: ключ и понятные ошибки."""
from __future__ import annotations

import json
import os
import re

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def api_key() -> str:
    key = (os.getenv("OPENROUTER_API_KEY") or "").strip().strip('"').strip("'")
    if not key:
        raise RuntimeError("Не найден OPENROUTER_API_KEY в переменных окружения. Добавь его в .env")
    if key.startswith("v1-"):
        key = "sk-or-" + key
    return key


def raise_if_http_error(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise RuntimeError(
            "Ключ к ИИ не принят. В .env нужен полный OPENROUTER_API_KEY, он начинается с sk-or-v1-"
        )
    if response.status_code == 404:
        raise RuntimeError("Такой модели ИИ нет. Проверь OPENROUTER_MODEL в .env")
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError("ИИ сейчас не ответил. Попробуй ещё раз чуть позже.") from exc


def extract_json_object(raw: str | None) -> dict:
    """Достаёт JSON-объект даже если модель добавила текст вокруг."""
    if not raw or not str(raw).strip():
        raise ValueError("пустой ответ модели")
    text = str(raw).strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    candidates = [text]
    match = _JSON_OBJECT.search(text)
    if match:
        candidates.append(match.group(0))
    for chunk in candidates:
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"ответ не JSON: {text[:400]!r}")
