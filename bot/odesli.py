import logging
from urllib.parse import parse_qs, urlsplit

import aiohttp

from . import cache
from .duration import fetch_duration
from .enrich import enrich_links, improve_thumbnail
from .models import MAIN_PLATFORMS, TrackInfo

log = logging.getLogger(__name__)

API_URL = "https://api.song.link/v1-alpha.1/links"


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
    params = {"url": url, "userCountry": "UA", "songIfSingle": "true"}
    async with aiohttp.ClientSession() as session:
        async with session.get(API_URL, params=params) as resp:
            if resp.status == 429:
                raise RateLimitError
            if resp.status in (400, 404):
                return None
            resp.raise_for_status()
            return await resp.json()


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
