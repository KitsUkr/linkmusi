import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Необов'язково: без ключа дані беруться зі сторінки song.link.
ODESLI_API_KEY = os.getenv("ODESLI_API_KEY", "")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не задано. Скопіюйте .env.example у .env і вставте токен від @BotFather."
    )
