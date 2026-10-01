import json
import logging
import re
from urllib.parse import parse_qs, quote, urlsplit

import aiohttp

from . import cache
from .config import ODESLI_API_KEY as API_KEY
from .duration import fetch_duration
from .enrich import enrich_links, improve_thumbnail
from .models import MAIN_PLATFORMS, TrackInfo

log = logging.getLogger(__name__)

API_URL = "https://api.song.link/v1-alpha.1/links"
PAGE_URL = "https://song.link/"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
_WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


class RateLimitError(Exception):
    pass


def normalize_url(url: str) -> str:
    """Прибирає трекінгові параметри (?si=, &list=), щоб кеш не дублювався."""
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    if host == "open.spotify.com":
        return f"https://open.spotify.com{parts.path}"
    if host in ("music.youtube.com", "youtube.com", "m.youtube.com"):
        video = parse_qs(parts.query).get("v", [""])[0]
        if video:
            return f"https://{host}/watch?v={video}"
    if host == "youtu.be":
        return f"https://www.youtube.com/watch?v={parts.path.lstrip('/')}"
    return url


async def _request_odesli(url: str) -> dict | None:
    if API_KEY:
        return await _request_api(url)
    return await _request_page(url)


async def _request_api(url: str) -> dict | None:
    params = {"url": url, "userCountry": "UA", "songIfSingle": "true", "key": API_KEY}
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.get(API_URL, params=params) as resp:
            if resp.status == 429:
                raise RateLimitError
            if resp.status in (400, 404):
                return None
            resp.raise_for_status()
            return await resp.json()


# З 31.07.2026 публічний API без ключа відповідає 401 PUBLIC_API_ACCESS_DEPRECATED.
# Сторінка song.link/<url> віддає ті самі дані в __NEXT_DATA__ — перекладаємо
# їх у формат відповіді API, щоб решта коду не змінювалась.
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


async def _request_page(url: str) -> dict | None:
    page_url = PAGE_URL + quote(url, safe="")
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers=_WEB_HEADERS) as session:
        async with session.get(page_url) as resp:
            if resp.status == 429:
                raise RateLimitError
            if resp.status in (400, 404):
                return None
            resp.raise_for_status()
            html = await resp.text()

    match = _NEXT_DATA_RE.search(html)
    if not match:
        raise ValueError("На сторінці song.link немає __NEXT_DATA__ — змінилась розмітка?")
    page_data = json.loads(match.group(1)).get("props", {}).get("pageProps", {}).get("pageData")
    if not page_data or not page_data.get("entityData"):
        return None
    return _page_to_api(page_data)


def _page_to_api(page_data: dict) -> dict:
    entity_id = page_data.get("entityUniqueId")
    entity = dict(page_data["entityData"])
    entity["apiProvider"] = entity.get("provider")
    entities = {entity_id: entity}
    links: dict[str, dict] = {}
    for section in page_data.get("sections") or []:
        for link in section.get("links") or []:
            platform, link_url = link.get("platform"), link.get("url")
            if not platform or not link_url:
                continue
            links.setdefault(platform, {"url": link_url})
            # uniqueId має вигляд "deezer|song|781592622" — з нього fetch_duration
            # бере id треку в Deezer/iTunes.
            unique_id = link.get("uniqueId") or ""
            provider, _, provider_id = unique_id.partition("|song|")
            if provider_id and unique_id not in entities:
                entities[unique_id] = {"apiProvider": provider, "id": provider_id}
    return {
        "entityUniqueId": entity_id,
        "entitiesByUniqueId": entities,
        "linksByPlatform": links,
        "pageUrl": page_data.get("pageUrl", ""),
    }


async def fetch_track(url: str) -> TrackInfo | None:
    url = normalize_url(url)
    hit, cached_track = cache.get(url)
    if hit:
        return cached_track

    data = await _request_odesli(url)
    if data is None:
        cache.put(url, None)
        return None

    track = _parse_response(data)
    ttl = cache.CACHE_TTL
    if track is not None:
        track.duration_sec = await fetch_duration(data)
        await enrich_links(track)
        if "spotify" not in track.links:
            await _merge_missing_platforms(track)
        await improve_thumbnail(track)
        if any(platform not in track.links for platform in MAIN_PLATFORMS):
            ttl = cache.SHORT_TTL
    cache.put(url, track, ttl)
    return track


async def _merge_missing_platforms(track: TrackInfo) -> None:
    """Odesli міг не зв'язати вхідне посилання з кластером пісні (типово для
    YouTube) — повторний запит через Deezer/Apple повертає повний набір."""
    source = track.links.get("deezer") or track.links.get("appleMusic")
    if not source:
        return
    try:
        data = await _request_odesli(source)
    except RateLimitError:
        log.warning("Ліміт Odesli на другому запиті — пропускаю злиття платформ")
        return
    if not data:
        return
    for platform, info in (data.get("linksByPlatform") or {}).items():
        if info.get("url"):
            track.links.setdefault(platform, info["url"])
    if track.duration_sec is None:
        track.duration_sec = await fetch_duration(data)


def _parse_response(data: dict) -> TrackInfo | None:
    entities = data.get("entitiesByUniqueId") or {}
    entity = entities.get(data.get("entityUniqueId"))
    if entity is None and entities:
        entity = next(iter(entities.values()))
    if entity is None or not entity.get("title"):
        return None

    links = {
        platform: info["url"]
        for platform, info in (data.get("linksByPlatform") or {}).items()
        if info.get("url")
    }
    return TrackInfo(
        title=entity.get("title", ""),
        artist=entity.get("artistName", ""),
        thumbnail_url=entity.get("thumbnailUrl"),
        duration_sec=None,
        page_url=data.get("pageUrl", ""),
        links=links,
    )
