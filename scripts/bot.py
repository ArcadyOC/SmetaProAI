"""Telegram-бот SmetaProAI."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject
from dotenv import load_dotenv

from file_reader import extract_text
from rate_picker import RatePickError, format_picked_text, pick_rates
from report_saver import save_report
from smeta_compiler import compile_smeta, format_smeta_text
from smeta_validator import ValidatorError, extract_positions, format_validation_report, validate_smeta
from speech_to_text import SpeechToTextError, transcribe
from task_parser import TaskParseError, parse_task

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN в переменных окружения. Добавь его в .env")

_allowed = os.getenv("ALLOWED_USER_ID")
if not _allowed:
    raise RuntimeError("Не найден ALLOWED_USER_ID в переменных окружения. Добавь его в .env")
try:
    ALLOWED_USER_ID = int(_allowed)
except ValueError as exc:
    raise RuntimeError("ALLOWED_USER_ID должен быть числом — id пользователя в Telegram") from exc

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


class AllowlistMiddleware(BaseMiddleware):
    """Чужие сообщения бот не обрабатывает — ключ ИИ не тратится."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, "from_user", None)
        if user is None or user.id != ALLOWED_USER_ID:
            if isinstance(event, CallbackQuery):
                await event.answer()
            return None
        return await handler(event, data)


dp.message.middleware(AllowlistMiddleware())
dp.callback_query.middleware(AllowlistMiddleware())


@dp.error()
async def on_error(event: ErrorEvent) -> None:
    logging.exception("Необработанная ошибка бота")
    update = event.update
    target = None
    if update.message:
        target = update.message
    elif update.callback_query and update.callback_query.message:
        target = update.callback_query.message
    if target is not None:
        await target.answer("Что-то сломалось при обработке. Попробуй ещё раз — если повторится, я посмотрю журнал.")

# Черновики смет, ждущие подтверждения "сохранить в reports/". Ключ — короткий id.
pending_drafts: dict[str, dict] = {}

# Текст присланного файла, ждущий выбора "составить смету" / "проверить готовую". Ключ — короткий id.
pending_docs: dict[str, str] = {}

TMP_DIR = ROOT / "tmp"

HELP_TEXT = (
    "Пришли задание на смету текстом, голосом или файлом.\n"
    "Например: сбить сосульки 120 п.м., работа только с вышки."
)
GREETINGS = {"привет", "здравствуй", "здравствуйте", "хай", "hello", "hi", "добрый день", "добрый вечер"}


def fmt_field(value: str | None) -> str:
    return value if value else "не указано"


@dp.message(F.document)
async def handle_document(message: Message) -> None:
    document = message.document
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    local_path = TMP_DIR / f"{uuid.uuid4().hex}_{document.file_name}"
    await bot.download(document, destination=local_path)

    try:
        text = extract_text(local_path)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    finally:
        local_path.unlink(missing_ok=True)

    if not text.strip():
        await message.answer("Открыл файл, но текста внутри не нашёл.")
        return

    doc_id = uuid.uuid4().hex[:12]
    pending_docs[doc_id] = text
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Это задание — составить смету", callback_data=f"doc_task:{doc_id}"),
                InlineKeyboardButton(text="Это готовая смета — проверить", callback_data=f"doc_check:{doc_id}"),
            ]
        ]
    )
    await message.answer("Прочитал файл. Что с ним делать?", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("doc_task:"))
async def handle_doc_task(callback: CallbackQuery) -> None:
    doc_id = callback.data.removeprefix("doc_task:")
    text = pending_docs.pop(doc_id, None)
    await callback.message.edit_reply_markup(reply_markup=None)
    if text is None:
        await callback.answer("Файл уже не актуален, пришли заново.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer("Разбираю задание...")
    await process_task_text(callback.message, text)


@dp.callback_query(F.data.startswith("doc_check:"))
async def handle_doc_check(callback: CallbackQuery) -> None:
    doc_id = callback.data.removeprefix("doc_check:")
    text = pending_docs.pop(doc_id, None)
    await callback.message.edit_reply_markup(reply_markup=None)
    if text is None:
        await callback.answer("Файл уже не актуален, пришли заново.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer("Проверяю смету...")

    try:
        positions = await extract_positions(text)
    except ValidatorError:
        await callback.message.answer("Не смог разобрать таблицу позиций в файле — попробуй другой формат.")
        return
    except RuntimeError as exc:
        await callback.message.answer(f"Ошибка настройки проверки: {exc}")
        return

    result = validate_smeta(positions)
    await callback.message.answer(format_validation_report(result))


@dp.message(F.voice)
async def handle_voice(message: Message) -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    local_path = TMP_DIR / f"{uuid.uuid4().hex}.ogg"
    await bot.download(message.voice, destination=local_path)

    await message.answer("Слушаю голосовое...")
    try:
        text = await transcribe(local_path)
    except SpeechToTextError:
        await message.answer("Не разобрал голосовое — попробуй ещё раз или напиши текстом.")
        return
    except RuntimeError as exc:
        await message.answer(f"Ошибка настройки голоса: {exc}")
        return
    finally:
        local_path.unlink(missing_ok=True)

    if not text:
        await message.answer("Не разобрал голосовое — попробуй ещё раз или напиши текстом.")
        return

    await message.answer(f"Распознал: «{text}»")
    await process_task_text(message, text)


@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(HELP_TEXT)


@dp.message()
async def handle_any_message(message: Message) -> None:
    text = message.text or message.caption
    if not text:
        await message.answer("Пока умею читать текст или файлы Word (.docx), Excel (.xlsx), PDF.")
        return
    if text.strip().lower() in GREETINGS:
        await message.answer(HELP_TEXT)
        return
    await process_task_text(message, text)


async def process_task_text(message: Message, text: str) -> None:
    await message.answer("Разбираю задание...")
    try:
        parsed = await parse_task(text)
    except TaskParseError as exc:
        logging.warning("Разбор не строгий JSON, иду по исходному тексту: %s", exc)
        parsed = {
            "work_type": text,
            "volume": None,
            "conditions": None,
            "search_queries": [],
            "original_text": text,
        }
    except RuntimeError as exc:
        await message.answer(f"Ошибка настройки: {exc}")
        return

    reply = (
        "Вот как я понял задание:\n"
        f"• Вид работ: {fmt_field(parsed.get('work_type'))}\n"
        f"• Объём: {fmt_field(parsed.get('volume'))}\n"
        f"• Условия: {fmt_field(parsed.get('conditions'))}"
    )
    await message.answer(reply)

    try:
        picked = await pick_rates(parsed)
    except RatePickError:
        await message.answer("Не смог разобрать подбор расценок — попробуй переформулировать задание.")
        return
    except RuntimeError as exc:
        await message.answer(f"Ошибка настройки подбора расценок: {exc}")
        return

    await message.answer(format_picked_text(picked))

    if not picked["selected"]:
        return

    compiled = compile_smeta(parsed, picked["selected"])
    await message.answer(format_smeta_text(compiled))

    if not compiled["positions"]:
        return

    draft_id = uuid.uuid4().hex[:12]
    pending_drafts[draft_id] = {"task": parsed, "compiled": compiled}
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сохранить в reports/", callback_data=f"save:{draft_id}"),
                InlineKeyboardButton(text="Не сохранять", callback_data=f"discard:{draft_id}"),
            ]
        ]
    )
    await message.answer("Сохранить этот черновик в reports/?", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("save:"))
async def handle_save(callback: CallbackQuery) -> None:
    draft_id = callback.data.removeprefix("save:")
    draft = pending_drafts.pop(draft_id, None)
    if draft is None:
        await callback.answer("Черновик уже не актуален.", show_alert=True)
        return

    path = save_report(draft["task"], draft["compiled"])
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"Сохранил черновик: {path.name}\n"
        "Это черновик в reports/ — заказчику или в канал сам по себе никуда не уходит."
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("discard:"))
async def handle_discard(callback: CallbackQuery) -> None:
    draft_id = callback.data.removeprefix("discard:")
    pending_drafts.pop(draft_id, None)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Хорошо, не сохраняю.")
    await callback.answer()


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
