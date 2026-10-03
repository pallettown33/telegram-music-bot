from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from music_bot import HELP_TEXT, START_TEXT

router = Router(name="general")


@router.message(Command("start"))
async def start(message: Message) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("그룹 음성채팅에서 사용하세요.")
        return
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(HELP_TEXT)
