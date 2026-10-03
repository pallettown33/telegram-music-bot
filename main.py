import asyncio
import os

from aiogram import Bot, Dispatcher

from config import bot_token
from handlers import general, music
from music_bot import MusicBot


async def main() -> None:
    os.environ["AIOGRAM_MODE"] = "true"
    player = MusicBot()
    await player.start_engine()
    music.configure(player)

    bot = Bot(token=bot_token())
    dispatcher = Dispatcher()
    dispatcher.include_router(general.router)
    dispatcher.include_router(music.router)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        await player.calls.stop()
        await player.assistant_client.disconnect()
        await player.bot_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
