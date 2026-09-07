"""Распознавание голосовых через OpenRouter — та же схема, что у Аркаши."""
from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx

from openrouter import OPENROUTER_URL, api_key, raise_if_http_error

DEFAULT_STT_MODEL = "google/gemini-2.5-flash-lite"

_FORMAT_BY_EXT = {
    "ogg": "ogg",
    "oga": "ogg",
    "opus": "ogg",
    "mp3": "mp3",
    "m4a": "m4a",
    "wav": "wav",
    "webm": "webm",
    "flac": "flac",
    "aac": "aac",
    "mp4": "mp4",
}

_TRANSCRIBE_PROMPT = (
    "Распознай голосовое сообщение на русском языке. "
    "Верни только дословный текст транскрипции, без комментариев и пояснений."
)


class SpeechToTextError(RuntimeError):
    pass


def _audio_format(path: Path) -> str:
    ext = path.suffix.lstrip(".").lower()
    return _FORMAT_BY_EXT.get(ext, "ogg")


async def transcribe(path: Path) -> str:
    key = api_key()
    model = os.getenv("STT_MODEL") or DEFAULT_STT_MODEL
    encoded = base64.b64encode(path.read_bytes()).decode("utf-8")

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _TRANSCRIBE_PROMPT},
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": encoded,
                                    "format": _audio_format(path),
                                },
                            },
                        ],
                    }
                ],
            },
        )
    raise_if_http_error(response)
    try:
        content = (response.json()["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise SpeechToTextError("Сервис распознавания не ответил") from exc
    if not content:
        raise SpeechToTextError("Распознавание вернуло пустой текст")
    return content
