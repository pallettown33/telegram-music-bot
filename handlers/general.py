from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="general")


@router.message(Command("start"))
async def start(message: Message) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("그룹 음성채팅에서 사용하세요.")
        return
    await message.answer(
        "🎵 음성채팅 노래봇\n\n"
        "/play 노래 제목 또는 YouTube 링크\n"
        "/join 음성채팅 입장\n"
        "/queue 대기열\n"
        "/skip 다음 곡\n"
        "/pause 일시정지\n"
        "/resume 재개\n"
        "/stop 종료"
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await start(message)
