from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from music_bot import (
    MSG_ADMIN_ONLY,
    MSG_ALREADY_PAUSED,
    MSG_NO_TRACK,
    MSG_NOT_PAUSED,
    MSG_PAUSED,
    MSG_PLAY_USAGE,
    MSG_QUEUE_EMPTY,
    MSG_QUEUE_ENDED,
    MSG_RESUMED,
    MSG_STOPPED,
    MSG_TRACK_NOT_FOUND,
    MusicBot,
    _queue_text,
)
from services.downloader import find_track

router = Router(name="music")
player: MusicBot | None = None


def configure(service: MusicBot) -> None:
    global player
    player = service


def _group(message: Message) -> bool:
    return message.chat.type in {"group", "supergroup"}


async def _admin(message: Message) -> bool:
    if player is None or not message.from_user:
        return False
    if await player.allowed_user(message.chat.id, message.from_user.id):
        return True
    await message.answer(MSG_ADMIN_ONLY)
    return False


@router.message(Command("play"))
async def play(message: Message, command: CommandObject) -> None:
    if not _group(message) or not await _admin(message) or player is None:
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer(MSG_PLAY_USAGE)
        return
    status = await message.answer("음원을 찾는 중입니다...")
    try:
        requested_by = message.from_user.full_name if message.from_user else "알 수 없음"
        track = await find_track(query, requested_by)
    except Exception:
        await status.edit_text(MSG_TRACK_NOT_FOUND)
        return
    chat_id = message.chat.id
    try:
        async with player.locks[chat_id]:
            queued, pulled = await player._enqueue_or_play(chat_id, track)
    except Exception as exc:
        await status.edit_text(f"음성 채팅 재생을 시작하지 못했습니다: {exc}")
        return
    if queued:
        await status.edit_text(f"대기열 {len(player.queues[chat_id])}번에 추가: {track.title}")
        return
    note = "\n재생 계정을 이 그룹으로 불러왔습니다." if pulled else ""
    await status.edit_text(f"재생 중: {track.title}{note}")


@router.message(Command("join"))
async def join(message: Message) -> None:
    if not _group(message) or not await _admin(message) or player is None:
        return
    chat_id = message.chat.id
    status = await message.answer("재생 계정을 그룹으로 부르는 중입니다...")
    try:
        async with player.locks[chat_id]:
            await player._join_voice_chat(chat_id)
        await status.edit_text("재생 계정을 음성채팅으로 불러왔습니다.")
    except Exception as exc:
        await status.edit_text(f"재생 계정을 불러오지 못했습니다: {exc}")


@router.message(Command("queue"))
async def queue(message: Message) -> None:
    if not _group(message) or player is None:
        return
    text = _queue_text(player.current.get(message.chat.id), player.queues[message.chat.id])
    await message.answer(text if text is not None else MSG_QUEUE_EMPTY)


@router.message(Command("skip"))
async def skip(message: Message) -> None:
    if not _group(message) or not await _admin(message) or player is None:
        return
    async with player.locks[message.chat.id]:
        track = await player._advance(message.chat.id)
    await message.answer(f"다음 곡 재생: {track.title}" if track else MSG_QUEUE_ENDED)


@router.message(Command("pause"))
async def pause(message: Message) -> None:
    if not _group(message) or not await _admin(message) or player is None:
        return
    chat_id = message.chat.id
    async with player.locks[chat_id]:
        if chat_id not in player.current:
            await message.answer(MSG_NO_TRACK)
            return
        if chat_id in player.paused:
            await message.answer(MSG_ALREADY_PAUSED)
            return
        await player._pause_playback(chat_id)
    await message.answer(MSG_PAUSED)


@router.message(Command("resume"))
async def resume(message: Message) -> None:
    if not _group(message) or not await _admin(message) or player is None:
        return
    chat_id = message.chat.id
    async with player.locks[chat_id]:
        if chat_id not in player.current:
            await message.answer(MSG_NO_TRACK)
            return
        if chat_id not in player.paused:
            await message.answer(MSG_NOT_PAUSED)
            return
        await player._resume_playback(chat_id)
    await message.answer(MSG_RESUMED)


@router.message(Command("stop"))
async def stop(message: Message) -> None:
    if not _group(message) or not await _admin(message) or player is None:
        return
    chat_id = message.chat.id
    async with player.locks[chat_id]:
        await player._stop_playback(chat_id)
    await message.answer(MSG_STOPPED)
