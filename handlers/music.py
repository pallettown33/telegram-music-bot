import asyncio

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from music_bot import MusicBot, _format_track
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
    await message.answer("이 명령은 그룹 관리자만 사용할 수 있습니다.")
    return False


@router.message(Command("play"))
async def play(message: Message, command: CommandObject) -> None:
    if not _group(message) or not await _admin(message) or player is None:
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer("사용법: /play 노래 제목 또는 YouTube 링크")
        return
    status = await message.answer("음원을 찾는 중입니다...")
    try:
        requested_by = message.from_user.full_name if message.from_user else "알 수 없음"
        track = await find_track(query, requested_by)
    except Exception:
        await status.edit_text("음원을 찾지 못했습니다. 다른 검색어 또는 링크를 사용하세요.")
        return
    chat_id = message.chat.id
    async with player.locks[chat_id]:
        if chat_id in player.current:
            player.queues[chat_id].append(track)
            await status.edit_text(f"대기열 {len(player.queues[chat_id])}번에 추가: {track.title}")
            return
        try:
            pulled = await player._ensure_assistant_member(chat_id)
            await player._promote_assistant(chat_id)
            await player._play_track(chat_id, track)
        except Exception as exc:
            await status.edit_text(f"음성 채팅 재생을 시작하지 못했습니다: {exc}")
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
            await player._ensure_assistant_member(chat_id)
            await player._promote_assistant(chat_id, force=True)
            active_calls = await player.calls.calls
            if chat_id not in player.current and chat_id not in active_calls:
                await player.calls.play(chat_id)
                await player._try_unmute(chat_id)
        await status.edit_text("재생 계정을 음성채팅으로 불러왔습니다.")
    except Exception as exc:
        await status.edit_text(f"재생 계정을 불러오지 못했습니다: {exc}")


@router.message(Command("queue"))
async def queue(message: Message) -> None:
    if not _group(message) or player is None:
        return
    current = player.current.get(message.chat.id)
    queued = player.queues[message.chat.id]
    if current is None and not queued:
        await message.answer("대기열이 비어 있습니다.")
        return
    lines = [_format_track(current) if current else "재생 중인 곡 없음"]
    lines[0] = f"재생 중: {lines[0]}" if current else lines[0]
    lines.extend(f"{i}. {_format_track(track)}" for i, track in enumerate(queued, 1))
    await message.answer("\n".join(lines))


@router.message(Command("skip"))
async def skip(message: Message) -> None:
    if not _group(message) or not await _admin(message) or player is None:
        return
    async with player.locks[message.chat.id]:
        track = await player._advance(message.chat.id)
    await message.answer(f"다음 곡 재생: {track.title}" if track else "대기열이 비어 있어 재생을 종료했습니다.")


@router.message(Command("pause"))
async def pause(message: Message) -> None:
    if not _group(message) or not await _admin(message) or player is None:
        return
    chat_id = message.chat.id
    async with player.locks[chat_id]:
        if chat_id not in player.current:
            await message.answer("현재 재생 중인 곡이 없습니다.")
            return
        await player.calls.pause(chat_id)
        player._cancel_advance(chat_id)
        player.paused.add(chat_id)
    await message.answer("일시정지했습니다.")


@router.message(Command("resume"))
async def resume(message: Message) -> None:
    if not _group(message) or not await _admin(message) or player is None:
        return
    chat_id = message.chat.id
    async with player.locks[chat_id]:
        if chat_id not in player.current:
            await message.answer("현재 재생 중인 곡이 없습니다.")
            return
        await player.calls.resume(chat_id)
        player.paused.discard(chat_id)
        player.started_at[chat_id] = asyncio.get_running_loop().time()
        player._schedule_advance(chat_id, player.remaining.get(chat_id))
    await message.answer("재생을 이어갑니다.")


@router.message(Command("stop"))
async def stop(message: Message) -> None:
    if not _group(message) or not await _admin(message) or player is None:
        return
    chat_id = message.chat.id
    async with player.locks[chat_id]:
        player._cancel_advance(chat_id)
        player.queues.pop(chat_id, None)
        player.current.pop(chat_id, None)
        player._reset_playback_state(chat_id)
        try:
            await player.calls.leave_call(chat_id)
        except Exception:
            pass
    await message.answer("재생을 종료하고 음성채팅에서 나갔습니다.")
