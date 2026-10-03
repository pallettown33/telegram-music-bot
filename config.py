import os

from dotenv import load_dotenv

load_dotenv()


def bot_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN을 .env에 설정하세요.")
    return token
