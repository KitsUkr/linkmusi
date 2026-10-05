import asyncio
import hashlib
import logging
import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
    Message,
)

from .card import build_caption, build_keyboard
from .odesli import RateLimitError, fetch_track

log = logging.getLogger(__name__)
router = Router()

MUSIC_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    r"open\.spotify\.com|spotify\.link|"
    r"music\.apple\.com|itunes\.apple\.com|"
    r"music\.youtube\.com|youtube\.com/watch|youtu\.be|"
    r"(?:www\.)?deezer\.com|deezer\.page\.link|"
    r"soundcloud\.com|"
    r"(?:listen\.)?tidal\.com|"
    r"music\.yandex\.(?:ru|com|by|kz|ua)|"
    r"music\.amazon\.[a-z.]+|"
    r"pandora\.com|"
    r"(?:song|album|odesli)\.link"
    r")/\S+",
    re.IGNORECASE,
)

START_TEXT = (
    "Привіт! 🎧\n\n"
    "Надішли мені посилання на трек зі Spotify, Apple Music, YouTube Music, "
    "Deezer, Tidal чи SoundCloud — я знайду його на інших "
    "платформах і надішлю картку з кнопками.\n\n"
    "Також працюю inline: у будь-якому чаті напиши мій нік і встав посилання."
)

NOT_A_LINK_TEXT = (
    "Не бачу посилання на музику 🤔\n"
    "Надішли посилання на трек, наприклад:\n"
    "https://open.spotify.com/track/..."
)

NOT_FOUND_TEXT = "Не зміг знайти цей трек на інших платформах 😔 Перевір, що посилання веде на трек."
RATE_LIMIT_TEXT = "Забагато запитів, сервіс пошуку просить зачекати. Спробуй ще раз за хвилину 🙏"
ERROR_TEXT = "Щось пішло не так під час пошуку треку. Спробуй ще раз трохи згодом."


def _consume_exception(task: asyncio.Task) -> None:
    # Фонові задачі без await: забираємо виняток, щоб не було warning'а.
    if not task.cancelled():
        task.exception()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(F.text)
async def handle_link(message: Message) -> None:
    match = MUSIC_URL_RE.search(message.text or "")
    is_private = message.chat.type == ChatType.PRIVATE
    if not match:
        # У групах мовчимо на звичайні повідомлення, щоб не спамити.
        if is_private:
            await message.answer(NOT_A_LINK_TEXT)
        return

    url = match.group(0)
    # Індикатор "надсилає фото" — у фоні, щоб не затримувати пошук.
    action = asyncio.create_task(
        message.bot.send_chat_action(message.chat.id, "upload_photo")
    )
    action.add_done_callback(_consume_exception)

    try:
        track = await fetch_track(url)
    except RateLimitError:
        await message.reply(RATE_LIMIT_TEXT)
        return
    except Exception:
        log.exception("Помилка під час обробки посилання %s", url)
        await message.reply(ERROR_TEXT)
        return

    if track is None:
        await message.reply(NOT_FOUND_TEXT)
        return

    caption = build_caption(track)
    keyboard = build_keyboard(track)
    if track.thumbnail_url:
        try:
            await message.reply_photo(
                photo=track.thumbnail_url, caption=caption, reply_markup=keyboard
            )
            return
        except Exception:
            log.warning("Не вдалося надіслати обкладинку %s", track.thumbnail_url)
    await message.reply(caption, reply_markup=keyboard)


# Telegram чекає відповідь на inline-запит лише кілька секунд.
INLINE_TIMEOUT_SEC = 8


async def _answer_inline(query: InlineQuery, results: list, cache_time: int) -> None:
    try:
        await query.answer(results, cache_time=cache_time, is_personal=False)
    except TelegramBadRequest as e:
        # Запит уже протух (юзер продовжив набирати) — це не помилка.
        log.info("Inline-відповідь не доставлено: %s", e.message)


@router.inline_query()
async def inline_link(query: InlineQuery) -> None:
    match = MUSIC_URL_RE.search(query.query or "")
    if not match:
        await _answer_inline(query, [], cache_time=10)
        return

    url = match.group(0)
    task = asyncio.create_task(fetch_track(url))
    done, _ = await asyncio.wait({task}, timeout=INLINE_TIMEOUT_SEC)
    if not done:
        # Збір триває у фоні й ляже в кеш — повторна спроба буде миттєвою.
        task.add_done_callback(_consume_exception)
        await _answer_inline(query, [], cache_time=5)
        return

    try:
        track = task.result()
    except RateLimitError:
        await _answer_inline(query, [], cache_time=5)
        return
    except Exception:
        log.exception("Помилка inline-запиту %s", url)
        await _answer_inline(query, [], cache_time=5)
        return

    if track is None:
        await _answer_inline(query, [], cache_time=60)
        return

    caption = build_caption(track)
    keyboard = build_keyboard(track)
    result_id = hashlib.md5(url.encode()).hexdigest()
    if track.thumbnail_url:
        result = InlineQueryResultPhoto(
            id=result_id,
            photo_url=track.thumbnail_url,
            thumbnail_url=track.thumbnail_url,
            title=track.title,
            description=track.artist,
            caption=caption,
            reply_markup=keyboard,
        )
    else:
        result = InlineQueryResultArticle(
            id=result_id,
            title=track.title,
            description=track.artist,
            input_message_content=InputTextMessageContent(message_text=caption),
            reply_markup=keyboard,
        )
    await _answer_inline(query, [result], cache_time=3600)
