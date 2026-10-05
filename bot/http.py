import ssl

import aiohttp
import certifi

TIMEOUT = aiohttp.ClientTimeout(total=15)
WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Системне сховище сертифікатів Windows не знає ланцюжок open.spotify.com —
# використовуємо CA-бандл certifi для всіх з'єднань.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    """Одна сесія на весь застосунок: keep-alive з'єднання замість
    нового TCP+TLS-хендшейку на кожен запит."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=TIMEOUT,
            headers=WEB_HEADERS,
            connector=aiohttp.TCPConnector(ssl=_SSL_CONTEXT, ttl_dns_cache=300),
        )
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None
