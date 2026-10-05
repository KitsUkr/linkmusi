import asyncio
import logging
import re
import time
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

import aiohttp

from .http import get_session

if TYPE_CHECKING:
    from .models import TrackInfo

log = logging.getLogger(__name__)

DURATION_TOLERANCE_SEC = 7
_ytmusic = None


async def enrich_links(track: "TrackInfo") -> None:
    # Без тривалості перевірка кандидатів ненадійна — для YT-посилань
    # добираємо її з метаданих самого відео.
    if track.duration_sec is None:
        track.duration_sec = await fetch_youtube_duration(track)
    tasks = []
    if "youtubeMusic" not in track.links:
        tasks.append(_add_youtube_music(track))
    if "appleMusic" not in track.links:
        tasks.append(_add_apple_music(track))
    if "soundcloud" not in track.links:
        tasks.append(_add_soundcloud(track))
    if "deezer" not in track.links:
        tasks.append(_add_deezer(track))
    if tasks:
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, BaseException):
                log.warning("Помилка під час пошуку відсутніх платформ: %s", result)
    # Spotify — після решти: точний пошук за ISRC спирається на знайдений Deezer.
    if "spotify" not in track.links:
        try:
            await _add_spotify(track)
        except Exception as e:
            log.warning("Помилка під час пошуку в Spotify: %s", e)


_FEAT_RE = re.compile(r"[(\[](?:feat|ft|with|prod)\.?[^)\]]*[)\]]", re.IGNORECASE)


def _search_query(track: "TrackInfo") -> str:
    # (feat. …) у назві часто ламає пошук — прибираємо.
    title = _FEAT_RE.sub(" ", track.title)
    title = re.sub(r"\s+", " ", title).strip(" -–—")
    return f"{track.artist} {title}".strip()


_YT_VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/)([\w-]{11})")


async def fetch_youtube_duration(track: "TrackInfo") -> int | None:
    url = track.links.get("youtubeMusic") or track.links.get("youtube")
    if not url:
        return None
    match = _YT_VIDEO_ID_RE.search(url)
    if not match:
        return None
    try:
        song = await asyncio.to_thread(lambda: _get_ytmusic().get_song(match.group(1)))
        return int(song["videoDetails"]["lengthSeconds"])
    except Exception as e:
        log.warning("Не вдалося отримати тривалість з YouTube: %s", e)
        return None


async def _add_youtube_music(track: "TrackInfo") -> None:
    results = await asyncio.to_thread(_search_ytmusic, _search_query(track))
    for item in results or []:
        video_id = item.get("videoId")
        if not video_id:
            continue
        artists = ", ".join(a.get("name", "") for a in item.get("artists") or [])
        if _matches(track, item.get("title", ""), artists, item.get("duration_seconds")):
            track.links["youtubeMusic"] = f"https://music.youtube.com/watch?v={video_id}"
            if not track.duration_sec and item.get("duration_seconds"):
                track.duration_sec = item["duration_seconds"]
            return


def _get_ytmusic():
    global _ytmusic
    if _ytmusic is None:
        from ytmusicapi import YTMusic

        _ytmusic = YTMusic()
    return _ytmusic


def _search_ytmusic(query: str) -> list[dict]:
    return _get_ytmusic().search(query, filter="songs", limit=5)


async def _add_deezer(track: "TrackInfo") -> None:
    async with get_session().get(
        "https://api.deezer.com/search",
        params={"q": _search_query(track), "limit": "5"},
    ) as resp:
        if resp.status != 200:
            return
        data = await resp.json()

    for item in data.get("data", []):
        url = item.get("link")
        if not url:
            continue
        artist = (item.get("artist") or {}).get("name", "")
        if _matches(track, item.get("title", ""), artist, item.get("duration")):
            track.links["deezer"] = url
            if not track.duration_sec and item.get("duration"):
                track.duration_sec = int(item["duration"])
            return


async def _add_apple_music(track: "TrackInfo") -> None:
    params = {
        "term": _search_query(track),
        "media": "music",
        "entity": "song",
        "limit": "5",
    }
    async with get_session().get("https://itunes.apple.com/search", params=params) as resp:
        if resp.status != 200:
            return
        data = await resp.json(content_type=None)

    for item in data.get("results", []):
        url = item.get("trackViewUrl")
        if not url:
            continue
        millis = item.get("trackTimeMillis")
        duration = round(millis / 1000) if millis else None
        if _matches(track, item.get("trackName", ""), item.get("artistName", ""), duration):
            track.links["appleMusic"] = url
            if not track.duration_sec and duration:
                track.duration_sec = duration
            return


# --- SoundCloud ---------------------------------------------------------

_SC_ASSET_RE = re.compile(r"https://a-v2\.sndcdn\.com/assets/[^\"']+\.js")
_SC_CLIENT_ID_RE = re.compile(r'client_id\s*[:=]\s*"([A-Za-z0-9]{16,})"')
_SC_MAX_ASSETS = 8

_sc_client_id: str | None = None


async def _get_soundcloud_client_id(session: aiohttp.ClientSession, force: bool = False) -> str | None:
    global _sc_client_id
    if _sc_client_id and not force:
        return _sc_client_id

    async with session.get("https://soundcloud.com/") as resp:
        if resp.status != 200:
            return None
        html = await resp.text()

    for asset_url in reversed(_SC_ASSET_RE.findall(html)[-_SC_MAX_ASSETS:]):
        async with session.get(asset_url) as resp:
            if resp.status != 200:
                continue
            js = await resp.text()
        match = _SC_CLIENT_ID_RE.search(js)
        if match:
            _sc_client_id = match.group(1)
            log.info("Отримано SoundCloud client_id")
            return _sc_client_id

    log.warning("Не вдалося витягти SoundCloud client_id")
    return None


async def _add_soundcloud(track: "TrackInfo") -> None:
    query = _search_query(track)
    session = get_session()
    client_id = await _get_soundcloud_client_id(session)
    if not client_id:
        return
    data = await _search_soundcloud(session, query, client_id)
    if data is None:
        client_id = await _get_soundcloud_client_id(session, force=True)
        if not client_id:
            return
        data = await _search_soundcloud(session, query, client_id)
    if data is None:
        return

    duration_fallback: str | None = None
    for item in data.get("collection", []):
        url = item.get("permalink_url")
        if not url:
            continue
        # У монетизованих треків duration — це 30-секундне прев'ю,
        # справжня довжина лежить у full_duration.
        millis = item.get("full_duration") or item.get("duration")
        duration = round(millis / 1000) if millis else None
        title = item.get("title", "")
        uploader = (item.get("user") or {}).get("username", "")
        publisher = (item.get("publisher_metadata") or {}).get("artist") or ""
        # Мешапи/ремікси/кавери відсіюємо одразу, як і треки з тривалістю,
        # що помітно відрізняється від оригіналу (інша версія пісні).
        if _is_other_version(track.title, title):
            continue
        if (
            duration
            and track.duration_sec
            and abs(track.duration_sec - duration) > DURATION_TOLERANCE_SEC
        ):
            continue
        if _similar(track.title, title) and (
            _similar(track.artist, publisher) or _similar(track.artist, uploader)
        ):
            track.links["soundcloud"] = url
            return
        if (
            duration_fallback is None
            and duration
            and track.duration_sec
            and abs(track.duration_sec - duration) <= DURATION_TOLERANCE_SEC
            and _similar(track.title, title)
        ):
            duration_fallback = url
    if duration_fallback:
        track.links["soundcloud"] = duration_fallback


async def _search_soundcloud(
    session: aiohttp.ClientSession, query: str, client_id: str
) -> dict | None:
    params = {"q": query, "client_id": client_id, "limit": "10"}
    async with session.get("https://api-v2.soundcloud.com/search/tracks", params=params) as resp:
        if resp.status in (401, 403):
            return None
        if resp.status != 200:
            return {}
        return await resp.json()


# --- Обкладинка -----------------------------------------------------------

_DEEZER_TRACK_ID_RE = re.compile(r"deezer\.com/(?:\w+/)?track/(\d+)")
_GUSERCONTENT_SIZE_RE = re.compile(r"=w\d+-h\d+[^=]*$")


async def improve_thumbnail(track: "TrackInfo") -> None:
    """Замінює дрібну YT-мініатюру (120×120) на повноцінну обкладинку."""
    url = track.thumbnail_url or ""
    if url and "googleusercontent.com" not in url and "ytimg.com" not in url:
        return
    if "deezer" in track.links:
        cover = await _deezer_cover(track.links["deezer"])
        if cover:
            track.thumbnail_url = cover
            return
    if "googleusercontent.com" in url:
        track.thumbnail_url = _GUSERCONTENT_SIZE_RE.sub("=w544-h544-l90-rj", url)


async def _deezer_cover(deezer_url: str) -> str | None:
    match = _DEEZER_TRACK_ID_RE.search(deezer_url)
    if not match:
        return None
    async with get_session().get(f"https://api.deezer.com/track/{match.group(1)}") as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    album = data.get("album") or {}
    return album.get("cover_xl") or album.get("cover_big") or None


# Безключовий пошук Spotify: embed-сторінка віддає анонімний accessToken,
# з яким працює офіційний пошук api.spotify.com — Premium не потрібен.

_SPOTIFY_TOKEN_RE = re.compile(r'"accessToken":"([^"]+)"')
_SPOTIFY_TOKEN_EXP_RE = re.compile(r'"accessTokenExpirationTimestampMs":(\d+)')
# Сторінка потрібна лише заради токена — підійде будь-який постійний трек.
_SPOTIFY_TOKEN_PAGE = "https://open.spotify.com/embed/track/4cOdK2wGLETKBW3PvgPWqT"

_spotify_token: tuple[str, float] | None = None


async def _get_spotify_token(session: aiohttp.ClientSession) -> str | None:
    global _spotify_token
    if _spotify_token and _spotify_token[1] > time.time():
        return _spotify_token[0]
    async with session.get(_SPOTIFY_TOKEN_PAGE) as resp:
        if resp.status != 200:
            log.warning("Spotify embed HTTP %s — токен не отримано", resp.status)
            return None
        html = await resp.text()
    token_match = _SPOTIFY_TOKEN_RE.search(html)
    if not token_match:
        log.warning("В embed-сторінці Spotify не знайшовся accessToken")
        return None
    exp_match = _SPOTIFY_TOKEN_EXP_RE.search(html)
    expires = int(exp_match.group(1)) / 1000 - 60 if exp_match else time.time() + 15 * 60
    _spotify_token = (token_match.group(1), expires)
    return _spotify_token[0]


# Після 429 Spotify каже, скільки чекати — не смикаємо його до того часу.
_spotify_blocked_until = 0.0
_SPOTIFY_BLOCK_CAP_SEC = 6 * 60 * 60


async def _spotify_search(
    session: aiohttp.ClientSession, headers: dict, query: str
) -> dict | None:
    global _spotify_blocked_until
    for attempt in range(2):
        async with session.get(
            "https://api.spotify.com/v1/search",
            params={"q": query, "type": "track", "limit": "5"},
            headers=headers,
        ) as resp:
            if resp.status == 429:
                retry_after = int(resp.headers.get("Retry-After") or 2)
                if attempt == 0 and retry_after <= 5:
                    await asyncio.sleep(retry_after)
                    continue
                _spotify_blocked_until = time.time() + min(
                    retry_after, _SPOTIFY_BLOCK_CAP_SEC
                )
                log.warning(
                    "Spotify 429: пошук призупинено на %d хв",
                    (min(retry_after, _SPOTIFY_BLOCK_CAP_SEC)) // 60,
                )
                return None
            if resp.status != 200:
                log.warning("Spotify search HTTP %s", resp.status)
                return None
            return await resp.json()
    return None


async def _deezer_isrc(session: aiohttp.ClientSession, deezer_url: str | None) -> str | None:
    if not deezer_url:
        return None
    match = _DEEZER_TRACK_ID_RE.search(deezer_url)
    if not match:
        return None
    async with session.get(f"https://api.deezer.com/track/{match.group(1)}") as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    return data.get("isrc") or None


async def _add_spotify(track: "TrackInfo") -> None:
    if time.time() < _spotify_blocked_until:
        return
    session = get_session()
    # Токен і ISRC не залежать один від одного — запитуємо паралельно.
    token, isrc = await asyncio.gather(
        _get_spotify_token(session),
        _deezer_isrc(session, track.links.get("deezer")),
    )
    if not token:
        return
    headers = {"Authorization": f"Bearer {token}"}
    # ISRC (міжнародний код запису) — точний збіг без евристик.
    queries = ([f"isrc:{isrc}"] if isrc else []) + [_search_query(track)]
    for query in queries:
        data = await _spotify_search(session, headers, query)
        if data is None:
            return
        for item in (data.get("tracks") or {}).get("items", []):
            url = (item.get("external_urls") or {}).get("spotify")
            if not url:
                continue
            artist = ", ".join(a.get("name", "") for a in item.get("artists", []))
            millis = item.get("duration_ms")
            duration = round(millis / 1000) if millis else None
            if query.startswith("isrc:") or _matches(
                track, item.get("name", ""), artist, duration
            ):
                track.links["spotify"] = url
                if not track.duration_sec and duration:
                    track.duration_sec = duration
                return


# Слова, що вказують на іншу версію пісні. Кандидат відкидається, якщо
# таке слово є в його назві, але відсутнє в назві оригіналу.
_VERSION_WORDS_RE = re.compile(
    r"\b(mash[\s-]?up|remix|rmx|remake|rework|re-?edit|edit|bootleg|flip|cover|"
    r"tribute|karaoke|instrumental|a\s?cappella|acapella|nightcore|sped[\s-]?up|"
    r"slowed|reverb|8d|bass[\s-]?boost\w*|lo[\s-]?fi|parody|medley|megamix|vip|"
    r"live|acoustic|cv|кавер\w*|мешап\w*|меш-ап|рем[иі]кс\w*|найткор|пароді\w*)\b",
    re.IGNORECASE,
)


def _is_other_version(original_title: str, candidate_title: str) -> bool:
    original = original_title.casefold()
    for match in _VERSION_WORDS_RE.finditer(candidate_title.casefold()):
        if match.group(0) not in original:
            return True
    return False


def _matches(
    track: "TrackInfo", title: str, artist: str, duration_sec: int | None
) -> bool:
    if _is_other_version(track.title, title):
        return False
    if track.duration_sec and duration_sec:
        if abs(track.duration_sec - duration_sec) <= DURATION_TOLERANCE_SEC:
            return True
    return _similar(track.title, title) and _similar(track.artist, artist)


def _similar(a: str, b: str) -> bool:
    a, b = a.casefold().strip(), b.casefold().strip()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.6
