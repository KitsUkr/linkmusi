from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .models import MAIN_PLATFORMS, TrackInfo

_LABELS = {
    "youtubeMusic": "YouTube Music",
    "appleMusic": "Apple Music",
    "spotify": "Spotify",
    "soundcloud": "SoundCloud",
    "deezer": "Deezer",
}
PLATFORMS: list[tuple[str, str]] = [(key, _LABELS[key]) for key in MAIN_PLATFORMS]


def build_caption(track: TrackInfo) -> str:
    name = f"{track.artist} – {track.title}" if track.artist else track.title
    lines = [f"🎵 <b>{escape(name)}</b>"]
    if track.duration_sec:
        minutes, seconds = divmod(track.duration_sec, 60)
        lines.append(f"⏱ {minutes}:{seconds:02d}")
    return "\n".join(lines)


def build_keyboard(track: TrackInfo) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=label, url=track.links[key])
        for key, label in PLATFORMS
        if key in track.links
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    if track.page_url:
        rows.append([InlineKeyboardButton(text="🔗 Всі платформи", url=track.page_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
