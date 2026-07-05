from dataclasses import dataclass, field

# Основні платформи картки; використовується і для кнопок,
# і для рішення про короткий TTL кешу при неповному результаті.
MAIN_PLATFORMS = ("youtubeMusic", "appleMusic", "spotify", "soundcloud", "deezer")


@dataclass
class TrackInfo:
    title: str
    artist: str
    thumbnail_url: str | None
    duration_sec: int | None
    page_url: str
    links: dict[str, str] = field(default_factory=dict)
