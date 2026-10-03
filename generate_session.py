import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()


async def main() -> None:
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError(".env에 TELEGRAM_API_ID와 TELEGRAM_API_HASH를 먼저 설정하세요.")

    async with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        await client.start()
        print("ASSISTANT_SESSION=" + client.session.save())


if __name__ == "__main__":
    asyncio.run(main())
