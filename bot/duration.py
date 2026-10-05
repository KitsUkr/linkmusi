import asyncio
import logging

import aiohttp

from .http import get_session

log = logging.getLogger(__name__)


async def fetch_duration(odesli_data: dict) -> int | None:
    entities = odesli_data.get("entitiesByUniqueId") or {}
    deezer_id = itunes_id = None
    for entity in entities.values():
        provider = entity.get("apiProvider")
        if provider == "deezer" and deezer_id is None:
            deezer_id = entity.get("id")
        elif provider == "itunes" and itunes_id is None:
            itunes_id = entity.get("id")

    try:
        if deezer_id:
            duration = await _from_deezer(deezer_id)
            if duration:
                return duration
        if itunes_id:
            return await _from_itunes(itunes_id)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Не вдалося отримати тривалість: %s", e)
    return None


async def _from_deezer(track_id: str) -> int | None:
    async with get_session().get(f"https://api.deezer.com/track/{track_id}") as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    duration = data.get("duration")
    return int(duration) if duration else None


async def _from_itunes(track_id: str) -> int | None:
    params = {"id": track_id}
    async with get_session().get("https://itunes.apple.com/lookup", params=params) as resp:
        if resp.status != 200:
            return None
        data = await resp.json(content_type=None)
    for result in data.get("results", []):
        millis = result.get("trackTimeMillis")
        if millis:
            return round(millis / 1000)
    return None
