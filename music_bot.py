import asyncio
import html
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque

from dotenv import load_dotenv
from pytgcalls import PyTgCalls, filters as fl, idle
from pytgcalls.types import MediaStream, StreamEnded
from telethon import Button, TelegramClient, events
from telethon.errors import (
    BotMethodInvalidError,
    ChatAdminRequiredError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
    UserNotParticipantError,
    UserPrivacyRestrictedError,
)
from telethon.sessions import SQLiteSession, StringSession
from telethon.tl.functions.channels import (
    EditAdminRequest,
    EditBannedRequest,
    GetFullChannelRequest,
    InviteToChannelRequest,
    JoinChannelRequest,
)
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.functions.messages import (
    AddChatUserRequest,
    EditExportedChatInviteRequest,
    ExportChatInviteRequest,
    GetFullChatRequest,
    HideChatJoinRequestRequest,
    ImportChatInviteRequest,
    SendVoteRequest,
)
from telethon.tl.functions.phone import EditGroupCallParticipantRequest, GetGroupParticipantsRequest
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeDefault,
    Channel,
    ChatAdminRights,
    ChatBannedRights,
    InputGroupCall,
    InputPeerSelf,
    User,
)
from telethon.utils import get_peer_id
import yt_dlp

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("telegram_music_bot")


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    requested_by: str
    duration: float | None
    candidate_urls: tuple[str, ...] = ()


def _format_duration(duration: int | None) -> str:
    if not duration or duration <= 0:
        return "길이 정보 없음"
    duration = int(duration)
    minutes, seconds = divmod(duration, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _format_track(track: Track) -> str:
    return f"{track.title} · {_format_duration(track.duration)} · 요청자 {track.requested_by}"


# 라이브/커버/리믹스 등 원곡이 아닌 버전을 뒤로 미는 검색 점수용 키워드.
_BAD_VERSION_RE = re.compile(
    r"\b(live|concert|cover|remix|acoustic|busking|fancam|karaoke|"
    r"instrumental|parody|reaction|slowed|nightcore|mashup|extended|"
    r"sped\s*up|speed\s*up|8d|bass\s*boosted|festival|unplugged)\b",
    re.IGNORECASE,
)
_BAD_VERSION_KO = (
    "라이브", "콘서트", "커버", "리믹스", "편곡", "어쿠스틱", "버스킹",
    "직캠", "노래방", "패러디", "리액션", "가요무대", "불후의 명곡",
    "스케치북", "연주", "1시간",
)
_GOOD_VERSION_RE = re.compile(
    r"\b(official|vevo|mv|m/v|music video)\b",
    re.IGNORECASE,
)
_GOOD_VERSION_KW = (
    "official audio", "official video",
    "audio", "오디오", "lyric", "가사",
)


class MusicBot:
    def __init__(self) -> None:
        api_id = os.getenv("TELEGRAM_API_ID")
        api_hash = os.getenv("TELEGRAM_API_HASH")
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not all((api_id, api_hash, bot_token)):
            raise RuntimeError(
                "TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN을 .env에 설정하세요."
            )
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg를 설치한 뒤 다시 실행하세요.")

        self.bot_token = bot_token
        self.admin_only = os.getenv("ADMIN_ONLY", "false").lower() == "true"
        # 설정하면 해당 채널 구독자만 봇을 사용할 수 있다 (필수 구독 채널).
        self.required_channel = (os.getenv("REQUIRED_CHANNEL") or "").strip() or None
        self.required_channel_link = (os.getenv("REQUIRED_CHANNEL_LINK") or "").strip() or None
        self._required_channel_ref = None
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.bot_client = TelegramClient("telegram_music_bot_bot", self.api_id, self.api_hash)
        self.assistant_client = self._build_assistant_client()
        self.calls = PyTgCalls(self.assistant_client)
        self.bot_id: int | None = None
        self.assistant_id: int | None = None
        self.assistant_ref = None
        self.queues: dict[int, Deque[Track]] = defaultdict(deque)
        self.current: dict[int, Track] = {}
        self.advance_tasks: dict[int, asyncio.Task[None]] = {}
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.started_at: dict[int, float] = {}
        self.remaining: dict[int, float | None] = {}
        self.paused: set[int] = set()
        self.media_dirs: dict[int, Path] = {}
        self.np_msgs: dict[int, int] = {}
        for stale in Path(tempfile.gettempdir()).glob("musicbot_*"):
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
        self.assistant_name = ""
        self.assistant_username = ""
        self._promoted: set[int] = set()
        self._promotion_warned: set[int] = set()
        self._kick_warned: set[int] = set()
        self._recent_joins: dict[int, float] = {}
        if os.getenv("AIOGRAM_MODE", "false").lower() != "true":
            self._register_handlers()
        self._register_call_handlers()
        self._register_assistant_handlers()

    def _build_assistant_client(self) -> TelegramClient:
        assistant_session = os.getenv("ASSISTANT_SESSION")
        if assistant_session and assistant_session != "replace_with_generated_session":
            return TelegramClient(StringSession(assistant_session), self.api_id, self.api_hash)

        assistant_session_file = os.getenv("ASSISTANT_SESSION_FILE")
        session_path = Path(
            assistant_session_file if assistant_session_file else "telegram_music_bot.session"
        ).expanduser()
        if session_path.exists():
            LOGGER.info("Using assistant session file: %s", session_path)
            return TelegramClient(SQLiteSession(str(session_path.with_suffix(""))), self.api_id, self.api_hash)

        LOGGER.warning(
            "ASSISTANT_SESSION이 없어서 새 세션 파일을 사용합니다. "
            "기존 계정으로 로그인하려면 generate_session.py로 StringSession을 생성하세요."
        )
        return TelegramClient(SQLiteSession(str(session_path.with_suffix(""))), self.api_id, self.api_hash)

    def _register_handlers(self) -> None:
        @self.bot_client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
        async def start_command(event: events.NewMessage.Event) -> None:
            if not event.is_group:
                return
            await event.respond(
                "🎵 음성채팅 노래봇\n\n"
                "재생하려면 /play 노래 제목 을 입력하세요.\n"
                "/join 으로 재생 계정을 음성채팅에 불러올 수 있습니다.\n"
                "전체 명령어는 /help 에서 확인할 수 있습니다."
            )

        @self.bot_client.on(events.NewMessage(pattern=r"^/help(?:@\w+)?$"))
        async def help_command(event: events.NewMessage.Event) -> None:
            if not event.is_group:
                return
            await event.respond(
                "🎵 노래봇 명령어\n\n"
                "[재생]\n"
                "/play <노래 제목 또는 YouTube 링크> - 재생 또는 대기열 추가\n"
                "/skip - 현재 곡 건너뛰기\n"
                "/pause - 일시정지\n"
                "/resume - 재생 재개\n\n"
                "[대기열 및 종료]\n"
                "/queue - 현재 곡과 대기열 확인\n"
                "/stop - 재생·대기열 종료 및 음성채팅 퇴장\n"
                "/join - 재생 계정을 이 그룹에 초대하고 음성채팅으로 불러오기\n\n"
                "/reload - 봇 프로세스 재시작 (그룹 관리자 전용)\n\n"
                "※ 재생 계정을 자동으로 들이려면 이 봇에 '사용자 초대' 권한이 필요합니다.\n"
                "※ '관리자 추가' 권한을 주면 관리봇 캡챠로부터 재생 계정을 자동으로 보호합니다."
            )

        @self.bot_client.on(events.NewMessage(pattern=r"^/play(?:@\w+)?(?:\s+(.+))?$"))
        async def play_command(event: events.NewMessage.Event) -> None:
            if not event.is_group or not await self._allowed(event):
                return
            query = event.pattern_match.group(1)
            if not query:
                await event.respond("사용법: /play 노래 제목 또는 YouTube 링크")
                return
            status = await event.respond("음원을 찾는 중입니다...")
            try:
                sender = await event.get_sender()
                requested_by = getattr(sender, "first_name", None) or "알 수 없음"
                track = await asyncio.to_thread(self._search_track, query.strip(), requested_by)
            except Exception as exc:
                LOGGER.info("Track lookup failed: %s", exc)
                await status.edit("음원을 찾지 못했습니다. 다른 검색어 또는 링크를 사용하세요.")
                return

            chat_id = event.chat_id
            async with self.locks[chat_id]:
                if chat_id in self.current:
                    self.queues[chat_id].append(track)
                    await status.edit(f"대기열 {len(self.queues[chat_id])}번에 추가: {track.title}")
                    return
                try:
                    pulled = await self._ensure_assistant_member(chat_id)
                    await self._promote_assistant(chat_id)
                    await self._play_track(chat_id, track)
                except Exception as exc:
                    LOGGER.exception("Could not start playback")
                    await status.edit(f"음성 채팅 재생을 시작하지 못했습니다: {exc}")
                    return
            pulled_note = "\n재생 계정을 이 그룹으로 불러왔습니다." if pulled else ""
            text, buttons = self._np_payload(chat_id)
            await status.edit(
                text + pulled_note,
                buttons=buttons,
                parse_mode="html",
                link_preview=False,
            )
            self.np_msgs[chat_id] = status.id

        @self.bot_client.on(events.NewMessage(pattern=r"^/skip(?:@\w+)?$"))
        async def skip_command(event: events.NewMessage.Event) -> None:
            if not event.is_group or not await self._allowed(event):
                return
            chat_id = event.chat_id
            async with self.locks[chat_id]:
                if chat_id not in self.current:
                    await event.respond("현재 재생 중인 곡이 없습니다.")
                    return
                next_track = await self._advance(chat_id)
            await self._np_refresh(chat_id)
            await event.respond(
                f"다음 곡 재생: {next_track.title}" if next_track else "대기열이 비어 있어 재생을 종료했습니다."
            )

        @self.bot_client.on(events.NewMessage(pattern=r"^/pause(?:@\w+)?$"))
        async def pause_command(event: events.NewMessage.Event) -> None:
            if not event.is_group or not await self._allowed(event):
                return
            chat_id = event.chat_id
            async with self.locks[chat_id]:
                if chat_id not in self.current:
                    await event.respond("현재 재생 중인 곡이 없습니다.")
                    return
                if chat_id in self.paused:
                    await event.respond("이미 일시정지 상태입니다.")
                    return
                await self._pause_playback(chat_id)
            await self._np_refresh(chat_id)
            await event.respond("일시정지했습니다.")

        @self.bot_client.on(events.NewMessage(pattern=r"^/resume(?:@\w+)?$"))
        async def resume_command(event: events.NewMessage.Event) -> None:
            if not event.is_group or not await self._allowed(event):
                return
            chat_id = event.chat_id
            async with self.locks[chat_id]:
                track = self.current.get(chat_id)
                if track is None:
                    await event.respond("현재 재생 중인 곡이 없습니다.")
                    return
                if chat_id not in self.paused:
                    await event.respond("일시정지 상태가 아닙니다.")
                    return
                await self._resume_playback(chat_id)
            await self._np_refresh(chat_id)
            await event.respond("재생을 이어갑니다.")

        @self.bot_client.on(events.NewMessage(pattern=r"^/stop(?:@\w+)?$"))
        async def stop_command(event: events.NewMessage.Event) -> None:
            if not event.is_group or not await self._allowed(event):
                return
            chat_id = event.chat_id
            async with self.locks[chat_id]:
                await self._stop_playback(chat_id)
            await self._np_refresh(chat_id)
            await event.respond("재생을 종료하고 음성 채팅에서 나갔습니다.")

        @self.bot_client.on(events.NewMessage(pattern=r"^/join(?:@\w+)?$"))
        async def join_command(event: events.NewMessage.Event) -> None:
            if not event.is_group or not await self._allowed(event):
                return
            chat_id = event.chat_id
            status = await event.respond("재생 계정을 그룹으로 부르는 중입니다...")
            try:
                async with self.locks[chat_id]:
                    await self._ensure_assistant_member(chat_id)
                    promotion = await self._promote_assistant(chat_id, force=True)
                    active_calls = await self.calls.calls
                    if chat_id not in self.current and chat_id not in active_calls:
                        await self.calls.play(chat_id)
                        unmuted = await self._try_unmute(chat_id)
                        if unmuted is False:
                            await self._notify_muted(chat_id)
            except Exception as exc:
                LOGGER.exception("Could not pull assistant into %s", chat_id)
                await status.edit(f"재생 계정을 불러오지 못했습니다: {exc}")
                return
            text = "재생 계정을 그룹에 들이고 음성채팅으로 불러왔습니다."
            if promotion == "failed":
                text += (
                    "\n⚠️ 재생 계정을 관리자로 지정하지 못했습니다. 관리봇의 신규 멤버 인증(캡챠)으로 "
                    "강퇴/뮤트될 수 있으니 봇에게 '관리자 추가' 권한을 주거나 재생 계정을 직접 "
                    "관리자로 지정해 주세요."
                )
            await status.edit(text)

        @self.bot_client.on(events.NewMessage(pattern=r"^/reload(?:@\w+)?$"))
        async def reload_command(event: events.NewMessage.Event) -> None:
            if not event.is_group:
                return
            # ADMIN_ONLY와 무관하게 항상 그룹 관리자 전용으로 둔다.
            if not await self._is_group_admin(event):
                await event.respond("이 명령은 그룹 관리자만 사용할 수 있습니다.")
                return
            await event.respond("봇을 재시작합니다...")
            asyncio.create_task(self._reload())

        @self.bot_client.on(events.ChatAction)
        async def on_bot_added(event: events.ChatAction.Event) -> None:
            if not event.is_group or self.bot_id is None:
                return
            if not (event.user_added or event.user_joined):
                return
            added_ids = set(event.user_ids or [])
            if event.user_id:
                added_ids.add(event.user_id)
            if self.bot_id not in added_ids:
                return
            try:
                await self._ensure_assistant_member(event.chat_id)
            except Exception as exc:
                LOGGER.info("Auto-invite failed for %s: %s", event.chat_id, exc)
                await event.respond(
                    "봇은 들어왔지만 재생 계정을 자동으로 들이지 못했습니다. "
                    "봇을 관리자로 지정하고 '사용자 초대' 권한을 준 뒤 /join 을 사용하세요. "
                    "'관리자 추가' 권한도 주면 관리봇의 신규 멤버 인증(캡챠)으로부터 재생 계정을 자동으로 보호합니다."
                )
                return
            await self._promote_assistant(event.chat_id, force=True, notify=True)
            await event.respond("재생 계정을 이 그룹으로 초대했습니다. /play 또는 /join 으로 음성채팅에 불러올 수 있습니다.")

        @self.bot_client.on(events.ChatAction)
        async def on_assistant_membership(event: events.ChatAction.Event) -> None:
            await self._on_assistant_membership_action(event)

        @self.bot_client.on(events.NewMessage(pattern=r"^/queue(?:@\w+)?$"))
        async def queue_command(event: events.NewMessage.Event) -> None:
            if not event.is_group:
                return
            current = self.current.get(event.chat_id)
            queued = self.queues[event.chat_id]
            if current is None and not queued:
                await event.respond("대기열이 비어 있습니다.")
                return
            lines = [f"재생 중: {_format_track(current)}" if current else "재생 중인 곡 없음"]
            lines.extend(f"{index}. {_format_track(track)}" for index, track in enumerate(queued, start=1))
            await event.respond("\n".join(lines))

        @self.bot_client.on(events.CallbackQuery(pattern=rb"^np:"))
        async def np_callback(event: events.CallbackQuery.Event) -> None:
            chat_id = event.chat_id
            action = event.data.decode("utf-8", "ignore").split(":", 1)[-1]
            if action != "close" and not await self._is_subscribed(event):
                await event.answer("채널을 구독해야 사용할 수 있습니다.", alert=True)
                return
            if action == "close":
                try:
                    await event.delete()
                except Exception:
                    pass
                self.np_msgs.pop(chat_id, None)
                return
            if action == "queue":
                current = self.current.get(chat_id)
                queued = self.queues[chat_id]
                if current is None and not queued:
                    await event.answer("대기열이 비어 있습니다.", alert=True)
                    return
                lines = [f"재생 중: {_format_track(current)}" if current else "재생 중인 곡 없음"]
                lines.extend(
                    f"{index}. {_format_track(track)}" for index, track in enumerate(queued, start=1)
                )
                await event.answer("\n".join(lines)[:190], alert=True)
                return
            async with self.locks[chat_id]:
                if action == "toggle":
                    if chat_id not in self.current:
                        await event.answer("현재 재생 중인 곡이 없습니다.")
                        return
                    if chat_id in self.paused:
                        await self._resume_playback(chat_id)
                        answer = "재생을 이어갑니다."
                    else:
                        await self._pause_playback(chat_id)
                        answer = "일시정지했습니다."
                elif action == "skip":
                    if chat_id not in self.current:
                        await event.answer("현재 재생 중인 곡이 없습니다.")
                        return
                    next_track = await self._advance(chat_id)
                    answer = (
                        f"다음 곡 재생: {next_track.title}" if next_track
                        else "대기열이 비어 있어 재생을 종료했습니다."
                    )
                elif action == "stop":
                    await self._stop_playback(chat_id)
                    answer = "재생을 종료하고 음성 채팅에서 나갔습니다."
                else:
                    return
            await self._np_refresh(chat_id)
            await event.answer(answer)

    def _register_call_handlers(self) -> None:
        @self.calls.on_update(fl.stream_end(StreamEnded.Type.AUDIO, fl.Device.MICROPHONE))
        async def on_stream_end(client: PyTgCalls, update: StreamEnded) -> None:
            chat_id = update.chat_id
            async with self.locks[chat_id]:
                track = self.current.get(chat_id)
                if track is None or chat_id in self.paused:
                    return
                # 곡 교체(/skip, 자동 전환) 직후 도착한 이전 스트림의 종료
                # 이벤트는 무시한다. 방금 시작한 곡을 다시 넘기지 않기 위함.
                started = self.started_at.get(chat_id, 0.0)
                if time.monotonic() - started < 3.0:
                    return
                next_track = await self._advance(chat_id)
            await self._announce_next(chat_id, next_track)

    async def _announce_next(self, chat_id: int, track: Track | None) -> None:
        if chat_id in self.np_msgs:
            await self._np_refresh(chat_id)
        elif track is not None:
            await self._np_send(chat_id)
        try:
            if track is None:
                await self.bot_client.send_message(chat_id, "대기열이 비어 있어 재생을 종료했습니다.")
        except Exception:
            LOGGER.info("Could not send queue update message to %s", chat_id)

    def _np_payload(self, chat_id: int) -> tuple[str, list | None]:
        track = self.current.get(chat_id)
        if track is None:
            return "⏹ 재생 중인 곡이 없습니다.", None
        paused = chat_id in self.paused
        icon = "⏸" if paused else "🎧"
        title = html.escape(track.title)
        if track.webpage_url:
            url = html.escape(track.webpage_url, quote=True)
            title = f'<a href="{url}">{title}</a>'
        text = (
            f"{icon} Now Playing{' (일시정지)' if paused else ''}\n\n"
            f"📌 곡명: {title}\n"
            f"⏱ 재생 시간: {_format_duration(track.duration)}\n"
            f"👤 신청자: {html.escape(track.requested_by)}\n\n"
            "💡 아래 버튼으로 제어할 수 있습니다."
        )
        buttons = [
            [
                Button.inline("⏯ 재생/일시정지", b"np:toggle"),
                Button.inline("⏭ 다음곡", b"np:skip"),
            ],
            [
                Button.inline("📄 대기열", b"np:queue"),
                Button.inline("⏹ 정지", b"np:stop"),
            ],
            [Button.inline("❌ 닫기", b"np:close")],
        ]
        return text, buttons

    async def _np_send(self, chat_id: int) -> None:
        text, buttons = self._np_payload(chat_id)
        try:
            message = await self.bot_client.send_message(
                chat_id, text, buttons=buttons, parse_mode="html", link_preview=False
            )
        except Exception:
            LOGGER.info("Could not send now-playing card to %s", chat_id)
            return
        self.np_msgs[chat_id] = message.id

    async def _np_refresh(self, chat_id: int) -> None:
        msg_id = self.np_msgs.get(chat_id)
        if msg_id is None:
            return
        text, buttons = self._np_payload(chat_id)
        try:
            await self.bot_client.edit_message(
                chat_id, msg_id, text, buttons=buttons, parse_mode="html", link_preview=False
            )
        except Exception:
            LOGGER.info("Could not refresh now-playing card in %s", chat_id)

    async def _pause_playback(self, chat_id: int) -> None:
        await self.calls.pause(chat_id)
        self._cancel_advance(chat_id)
        self.paused.add(chat_id)
        remaining = self.remaining.get(chat_id)
        started = self.started_at.get(chat_id)
        if remaining is not None and started is not None:
            self.remaining[chat_id] = max(remaining - (time.monotonic() - started), 0.0)

    async def _resume_playback(self, chat_id: int) -> None:
        await self.calls.resume(chat_id)
        self.paused.discard(chat_id)
        self.started_at[chat_id] = time.monotonic()
        remaining = self.remaining.get(chat_id)
        self._schedule_advance(chat_id, max(remaining, 1.0) if remaining is not None else None)

    async def _stop_playback(self, chat_id: int) -> None:
        self._cancel_advance(chat_id)
        self.queues.pop(chat_id, None)
        self.current.pop(chat_id, None)
        self._reset_playback_state(chat_id)
        self._cleanup_media(chat_id)
        try:
            await self.calls.leave_call(chat_id)
        except Exception:
            LOGGER.info("Voice chat already disconnected for %s", chat_id)

    async def _is_group_admin(self, event: events.NewMessage.Event) -> bool:
        # 익명 관리자가 보낸 메시지는 발신자가 유저가 아니라 그룹(채널) 자체로
        # 표시된다. 그룹 명의로 쓸 수 있는 사람은 관리자뿐이므로, 발신 채널이
        # 이 그룹 자체일 때만 통과시킨다(다른 채널 명의 발신은 제외).
        sender = event.sender
        if sender is not None and not isinstance(sender, User):
            chat_id = event.chat_id or 0
            channel_id = -chat_id - 10**12 if chat_id < -(10**12) else abs(chat_id)
            if getattr(sender, "id", None) == channel_id:
                return True
        try:
            permissions = await self.bot_client.get_permissions(event.chat_id, event.sender_id)
        except Exception as exc:
            LOGGER.info(
                "Permission check failed for user %s in %s: %s",
                event.sender_id, event.chat_id, exc,
            )
            return False
        return bool(getattr(permissions, "is_admin", False) or getattr(permissions, "is_creator", False))

    async def _is_subscribed(self, event) -> bool:
        """필수 구독 채널이 설정된 경우, 발신자의 구독 여부를 확인한다.

        채널이 비공개면 봇이 그 채널의 멤버(가능하면 관리자)여야 확인할 수 있다.
        확인 자체가 불가능하면 기능이 망가지지 않도록 통과시킨다.
        """
        if not self.required_channel:
            return True
        sender = event.sender
        if sender is not None and not isinstance(sender, User):
            return True
        try:
            if self._required_channel_ref is None:
                self._required_channel_ref = await self.bot_client.get_entity(self.required_channel)
            await self.bot_client.get_permissions(self._required_channel_ref, event.sender_id)
            return True
        except UserNotParticipantError:
            return False
        except Exception as exc:
            LOGGER.info("Subscription check failed for %s: %s", event.sender_id, exc)
            return True

    async def _respond_subscribe(self, event) -> None:
        text = "이 봇을 사용하려면 먼저 채널을 구독해주세요."
        if self.required_channel_link:
            await event.respond(
                text, buttons=[[Button.url("📢 채널 구독하기", self.required_channel_link)]]
            )
        else:
            await event.respond(text)

    async def _allowed(self, event: events.NewMessage.Event) -> bool:
        if not await self._is_subscribed(event):
            await self._respond_subscribe(event)
            return False
        if not self.admin_only:
            return True
        if await self._is_group_admin(event):
            return True
        await event.respond("이 명령은 그룹 관리자만 사용할 수 있습니다.")
        return False

    async def allowed_user(self, chat_id: int, user_id: int) -> bool:
        """aiogram Router가 재생 제어 권한을 확인할 때 사용하는 공용 검사."""
        if not self.admin_only:
            return True
        try:
            permissions = await self.bot_client.get_permissions(chat_id, user_id)
        except Exception:
            LOGGER.exception("Permission check failed for user %s in %s", user_id, chat_id)
            return False
        return bool(getattr(permissions, "is_admin", False) or getattr(permissions, "is_creator", False))

    async def _ensure_assistant_member(self, chat_id: int) -> bool:
        """재생 계정이 그룹에 없으면 봇이 직접 초대하고, 안 되면 일회용 링크로 입장시킨다.

        반환값은 이번 호출에서 새로 불러왔는지다.
        """
        if self.assistant_ref is None:
            raise RuntimeError("재생 계정을 봇이 찾지 못했습니다. 계정 username을 설정하거나 봇과 한 번 대화를 여세요.")
        chat = await self.bot_client.get_entity(chat_id)
        if await self._assistant_is_member(chat):
            await self._remember_chat_for_assistant(chat_id)
            return False
        await self._pull_assistant(chat)
        self._recent_joins[chat_id] = time.time()
        await self._remember_chat_for_assistant(chat_id)
        return True

    async def _assistant_is_member(self, chat) -> bool:
        try:
            permissions = await self.bot_client.get_permissions(chat, self.assistant_ref)
        except UserNotParticipantError:
            return False
        if permissions is not None and permissions.is_banned:
            await self._unban_assistant(chat)
            return False
        return True

    async def _unban_assistant(self, chat) -> None:
        if not isinstance(chat, Channel):
            return
        await self.bot_client(EditBannedRequest(
            channel=chat,
            participant=self.assistant_ref,
            banned_rights=ChatBannedRights(until_date=None, view_messages=False),
        ))

    async def _pull_assistant(self, chat) -> None:
        try:
            await self._invite_directly(chat)
        except UserAlreadyParticipantError:
            return
        except (BotMethodInvalidError, ChatAdminRequiredError, UserPrivacyRestrictedError) as exc:
            LOGGER.info("Direct invite unavailable (%s); joining by invite link", type(exc).__name__)
        except Exception as exc:
            LOGGER.info("Direct invite failed (%s); trying invite link", type(exc).__name__)

        if await self._assistant_is_member(chat):
            return

        username = getattr(chat, "username", None)
        if username and isinstance(chat, Channel):
            try:
                await self.assistant_client(JoinChannelRequest(username))
            except UserAlreadyParticipantError:
                return
            except Exception as exc:
                LOGGER.info("Public join failed: %s", type(exc).__name__)
            if await self._assistant_is_member(chat):
                return

        await self._join_with_fresh_invite(chat)
        if not await self._assistant_is_member(chat):
            raise RuntimeError(
                "재생 계정을 그룹에 들이지 못했습니다. "
                "봇을 관리자로 지정하고 '사용자 초대' 권한을 준 뒤 /join 을 다시 실행하세요. "
                "'관리자 추가' 권한도 함께 주면 관리봇의 신규 멤버 인증으로부터 재생 계정을 자동으로 보호합니다."
            )

    async def _invite_directly(self, chat) -> None:
        if isinstance(chat, Channel):
            await self.bot_client(InviteToChannelRequest(chat, [self.assistant_ref]))
            return
        await self.bot_client(AddChatUserRequest(chat.id, self.assistant_ref, fwd_limit=0))

    async def _join_with_fresh_invite(self, chat) -> None:
        link = await self._export_invite_link(chat)
        invite_hash = self._hash_from_link(link)
        if invite_hash is None:
            fallback = os.getenv("ASSISTANT_INVITE_LINK")
            invite_hash = self._hash_from_link(fallback) if fallback else None
        if not invite_hash:
            if link:
                await self._revoke_invite_link(chat, link)
            raise RuntimeError(
                "초대 링크를 만들지 못했습니다. 봇에 '사용자 초대' 권한이 필요합니다."
            )
        try:
            try:
                await self.assistant_client(ImportChatInviteRequest(invite_hash))
            except UserAlreadyParticipantError:
                return
            except InviteRequestSentError:
                await self._approve_join(chat)
            for _ in range(5):
                if await self._assistant_is_member(chat):
                    return
                await asyncio.sleep(0.3)
            await self._approve_join(chat)
        finally:
            if link:
                await self._revoke_invite_link(chat, link)

    async def _export_invite_link(self, chat) -> str | None:
        # usage_limit=1 링크는 입장 확인 과정에서 사용 횟수가 소진되어
        # InviteHashExpiredError를 유발하므로 일반 링크로 만들고,
        # 입장 처리 후 _revoke_invite_link로 해지한다.
        try:
            exported = await self.bot_client(ExportChatInviteRequest(
                peer=chat,
                title="music-assistant",
            ))
        except Exception as exc:
            LOGGER.info("Could not export invite link: %s", type(exc).__name__)
            return None
        return getattr(exported, "link", None)

    async def _revoke_invite_link(self, chat, link: str) -> None:
        try:
            await self.bot_client(EditExportedChatInviteRequest(
                peer=chat,
                link=link,
                revoked=True,
            ))
        except Exception as exc:
            LOGGER.info("Could not revoke invite link: %s", type(exc).__name__)

    @staticmethod
    def _hash_from_link(link: str | None) -> str | None:
        if not link:
            return None
        slug = link.strip().rstrip("/").rsplit("/", 1)[-1]
        if slug.startswith("+"):
            slug = slug[1:]
        return slug or None

    async def _approve_join(self, chat) -> None:
        try:
            await self.bot_client(HideChatJoinRequestRequest(
                peer=chat,
                user_id=self.assistant_ref,
                approved=True,
            ))
        except Exception as exc:
            LOGGER.info("Join approval failed: %s", type(exc).__name__)

    async def _remember_chat_for_assistant(self, chat_id: int) -> None:
        try:
            await self.assistant_client.get_entity(chat_id)
            return
        except ValueError:
            pass
        async for dialog in self.assistant_client.iter_dialogs():
            if dialog.id == chat_id:
                await self.assistant_client.get_entity(dialog.entity)
                return
        raise RuntimeError("재생 계정이 그룹에 들어왔지만 채팅 정보를 아직 읽지 못했습니다. 잠시 후 다시 시도하세요.")

    async def _on_assistant_membership_action(self, event: events.ChatAction.Event) -> None:
        """재생 계정의 입장(수동 추가 포함)과 강퇴를 감지해 자동 대응한다."""
        try:
            if self.assistant_id is None or not event.is_group:
                return
            added_ids = set(event.user_ids or [])
            if event.user_id:
                added_ids.add(event.user_id)
            if self.assistant_id not in added_ids:
                return
            chat_id = event.chat_id
            if event.user_joined or event.user_added:
                self._recent_joins[chat_id] = time.time()
                await self._promote_assistant(chat_id, force=True, notify=True)
            elif event.user_kicked:
                self._promoted.discard(chat_id)
                await self._notify_assistant_kicked(chat_id)
        except Exception:
            LOGGER.debug("Assistant membership action handling failed", exc_info=True)

    async def _promote_assistant(self, chat_id: int, *, force: bool = False, notify: bool = False) -> str:
        """재생 계정을 최소 권한 관리자로 지정한다.

        관리자는 Telewiki 같은 관리봇이 강퇴/뮤트할 수 없고 음성채팅 발언도
        항상 허용되므로, 신규 멤버 캡챠로 재생이 끊기는 문제를 막을 수 있다.
        반환값은 "already" | "promoted" | "failed" 이다.
        """
        if self.assistant_ref is None or self.assistant_id is None:
            return "failed"
        if not force and chat_id in self._promoted:
            return "already"
        try:
            permissions = await self.bot_client.get_permissions(chat_id, self.assistant_ref)
            if permissions.is_admin or permissions.is_creator:
                self._promoted.add(chat_id)
                self._promotion_warned.discard(chat_id)
                return "already"
        except Exception:
            pass
        try:
            await self.bot_client(EditAdminRequest(
                channel=chat_id,
                user_id=self.assistant_ref,
                admin_rights=ChatAdminRights(manage_call=True),
                rank="음악 재생",
            ))
        except Exception as exc:
            LOGGER.warning("Assistant promotion failed in %s: %s", chat_id, exc)
            if notify:
                await self._notify_promotion_failure(chat_id)
            return "failed"
        self._promoted.add(chat_id)
        self._promotion_warned.discard(chat_id)
        LOGGER.info("Promoted assistant to admin in %s", chat_id)
        return "promoted"

    async def _notify_promotion_failure(self, chat_id: int) -> None:
        if chat_id in self._promotion_warned:
            return
        self._promotion_warned.add(chat_id)
        try:
            await self.bot_client.send_message(
                chat_id,
                "⚠️ 재생 계정을 관리자로 지정하지 못했습니다. 관리봇의 신규 멤버 인증(캡챠)으로 "
                "재생 계정이 강퇴/뮤트될 수 있습니다. 봇에게 '관리자 추가' 권한을 주면 재생 계정이 "
                "들어올 때마다 자동으로 관리자로 지정되며, 그 전까지는 재생 계정을 직접 관리자로 "
                "지정해 주세요.",
            )
        except Exception:
            LOGGER.info("Could not send promotion warning to %s", chat_id)

    async def _notify_assistant_kicked(self, chat_id: int) -> None:
        if chat_id in self._kick_warned:
            return
        self._kick_warned.add(chat_id)
        try:
            await self.bot_client.send_message(
                chat_id,
                "⚠️ 재생 계정이 그룹에서보내졌습니다. 관리봇의 신규 멤버 인증(캡챠) 때문일 수 있습니다. "
                "재생 계정을 관리자로 지정하거나 봇에게 '관리자 추가' 권한을 주면 자동으로 보호됩니다. "
                "재입장은 /join 을 사용하세요.",
            )
        except Exception:
            LOGGER.info("Could not send kick warning to %s", chat_id)

    def _register_assistant_handlers(self) -> None:
        """재생 계정 측 핸들러. 입장 추적과 관리봇 캡챠 자동 응답을 담당한다."""

        @self.assistant_client.on(events.ChatAction)
        async def on_self_join(event: events.ChatAction.Event) -> None:
            try:
                if self.assistant_id is None or not event.is_group:
                    return
                if not (event.user_joined or event.user_added):
                    return
                added_ids = set(event.user_ids or [])
                if event.user_id:
                    added_ids.add(event.user_id)
                if self.assistant_id in added_ids:
                    self._recent_joins[event.chat_id] = time.time()
            except Exception:
                LOGGER.debug("Assistant join tracking failed", exc_info=True)

        @self.assistant_client.on(events.NewMessage(incoming=True))
        async def on_possible_captcha(event: events.NewMessage.Event) -> None:
            try:
                await self._maybe_solve_captcha(event)
            except Exception:
                LOGGER.debug("Captcha auto-response failed", exc_info=True)

    async def _maybe_solve_captcha(self, event: events.NewMessage.Event) -> None:
        """입장 직후 관리봇이 보낸 캡챠에 최선 노력으로 응답한다.

        봇이 재생 계정을 관리자로 지정할 수 없는 그룹을 위한 예비 수단이다.
        버튼형 캡챠는 버튼을 누르고, 간단한 산수 퀴즈 투표형 캡챠는 계산해서
        투표한다. 입장 후 120초 동안만 동작한다.
        """
        chat_id = event.chat_id
        if chat_id is None:
            return
        joined_at = self._recent_joins.get(chat_id)
        if joined_at is None:
            return
        if time.time() - joined_at > 120:
            self._recent_joins.pop(chat_id, None)
            return
        message = event.message
        if message is None:
            return
        try:
            sender = await event.get_sender()
        except Exception:
            return
        if not getattr(sender, "bot", False):
            return
        text = message.raw_text or ""
        if not self._captcha_targets_assistant(text):
            return
        if await self._try_click_captcha_button(chat_id, message):
            return
        await self._try_answer_captcha_poll(chat_id, message, text)

    def _captcha_targets_assistant(self, text: str) -> bool:
        if self.assistant_username and f"@{self.assistant_username}" in text.lower():
            return True
        return bool(self.assistant_name) and self.assistant_name in text

    @staticmethod
    async def _try_click_captcha_button(chat_id: int, message) -> bool:
        keywords = (
            "인증", "확인", "동의", "사람", "로봇", "통과",
            "verify", "human", "robot", "captcha", "✅", "✔",
        )
        flat = []
        for row_i, row in enumerate(message.buttons or []):
            for col_i, button in enumerate(row):
                flat.append((row_i, col_i, getattr(button, "text", "") or ""))
        if not flat:
            return False
        matches = [b for b in flat if any(k in b[2].lower() for k in keywords)]
        choice = matches[0] if len(matches) == 1 else (flat[0] if len(flat) == 1 else None)
        if choice is None:
            return False
        try:
            await message.click(i=choice[0], j=choice[1])
        except Exception as exc:
            LOGGER.warning("Captcha button click failed in %s: %s", chat_id, exc)
        else:
            LOGGER.info("Clicked captcha button '%s' in %s", choice[2], chat_id)
        return True

    async def _try_answer_captcha_poll(self, chat_id: int, message, text: str) -> bool:
        poll_media = getattr(message, "poll", None)
        if poll_media is None:
            return False
        expr = re.search(r"(\d+)\s*([+\-−×*xX÷/])\s*(\d+)", text)
        if expr is None:
            return False
        left, op, right = int(expr.group(1)), expr.group(2), int(expr.group(3))
        if op == "+":
            value = left + right
        elif op in "-−":
            value = left - right
        elif op in "×*xX":
            value = left * right
        else:
            if right == 0 or left % right:
                return False
            value = left // right
        matches = [
            answer for answer in poll_media.poll.answers
            if self._poll_answer_matches(answer, value)
        ]
        if len(matches) != 1:
            return False
        try:
            await self.assistant_client(SendVoteRequest(
                peer=chat_id,
                msg_id=message.id,
                options=[matches[0].option],
            ))
        except Exception as exc:
            LOGGER.warning("Captcha poll vote failed in %s: %s", chat_id, exc)
            return False
        LOGGER.info("Answered captcha poll in %s with %s", chat_id, value)
        return True

    @staticmethod
    def _poll_answer_matches(answer, value: int) -> bool:
        raw = getattr(answer, "text", "")
        raw = getattr(raw, "text", raw)
        found = re.search(r"-?\d+", str(raw))
        return bool(found) and int(found.group()) == value

    async def _bind_assistant(self) -> None:
        assistant = await self.assistant_client.get_me()
        bot = await self.bot_client.get_me()
        self.assistant_id = assistant.id
        self.bot_id = bot.id
        self.assistant_name = (getattr(assistant, "first_name", "") or "").strip()
        self.assistant_username = (getattr(assistant, "username", "") or "").lower()
        self.assistant_ref = await self._resolve_assistant_for_bot(assistant, bot)

    async def _resolve_assistant_for_bot(self, assistant, bot):
        try:
            return await self.bot_client.get_input_entity(assistant.id)
        except ValueError:
            pass
        if assistant.username:
            try:
                return await self.bot_client.get_input_entity(assistant.username)
            except ValueError:
                pass
        if not bot.username:
            return None
        try:
            await self.assistant_client.send_message(bot.username, "/start")
        except Exception as exc:
            LOGGER.info("Assistant could not open the bot chat: %s", type(exc).__name__)
            return None
        for _ in range(10):
            try:
                return await self.bot_client.get_input_entity(assistant.id)
            except ValueError:
                await asyncio.sleep(0.3)
        return None

    @staticmethod
    def _search_track(query: str, requested_by: str) -> Track:
        options = MusicBot._youtube_options()
        is_url = query.startswith(("http://", "https://"))
        # SoundCloud는 커버/리믹스/리포스트가 많아 YouTube를 먼저 검색하고,
        # 재생 가능한 결과가 없을 때만 폴백으로 사용한다.
        if not is_url:
            options.update(extract_flat=True, ignoreerrors=True)
            query = f"ytsearch10:{query}"
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(query, download=False)
        entries = info.get("entries") if isinstance(info, dict) else None
        candidates = tuple(
            entry for entry in (entries or [info])
            if entry and (entry.get("webpage_url") or entry.get("url"))
        )
        if not candidates:
            raise ValueError("검색 결과가 없습니다")
        candidates = sorted(candidates, key=MusicBot._originality_score, reverse=True)
        urls = tuple(entry.get("webpage_url") or entry.get("url") for entry in candidates)
        for entry, url in zip(candidates, urls):
            track = Track(
                title=entry.get("title", "제목 없음"),
                webpage_url=url,
                requested_by=requested_by,
                duration=entry.get("duration"),
                candidate_urls=(url,),
            )
            try:
                MusicBot._stream_info(track)
                return Track(
                    title=track.title,
                    webpage_url=track.webpage_url,
                    requested_by=track.requested_by,
                    duration=track.duration,
                    candidate_urls=urls,
                )
            except Exception:
                continue
        if not is_url:
            return MusicBot._search_soundcloud_track(query.removeprefix("ytsearch10:"), requested_by)
        raise ValueError("YouTube에서 재생 가능한 영상을 찾지 못했습니다")

    @staticmethod
    def _originality_score(entry) -> int:
        """검색 결과가 원곡 음원에 가까울수록 높은 점수를 준다.

        라이브/커버/편곡/리믹스 류는 감점, 공식 음원(Topic 채널, Official
        Audio 등)은 가점. 너무 짧거나 비정상적으로 긴 결과도 감점한다.
        """
        title = str(entry.get("title") or "")
        uploader = str(entry.get("channel") or entry.get("uploader") or "")
        text = f"{title} {uploader}".lower()
        score = -2 * len(_BAD_VERSION_RE.findall(text))
        score -= sum(2 for kw in _BAD_VERSION_KO if kw in text)
        score += min(len(_GOOD_VERSION_RE.findall(text)), 2)
        score += sum(1 for kw in _GOOD_VERSION_KW if kw in text)
        if "topic" in uploader.lower():
            score += 3
        duration = entry.get("duration") or 0
        if duration > 600:
            score -= 2
        elif 0 < duration < 45:
            score -= 1
        return score

    @staticmethod
    def _search_soundcloud_track(query: str, requested_by: str) -> Track:
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extract_flat": True,
            "ignoreerrors": True,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"scsearch5:{query}", download=False)
        entries = info.get("entries", []) if isinstance(info, dict) else []
        for entry in sorted(
            (e for e in entries if e), key=MusicBot._originality_score, reverse=True
        ):
            if not entry:
                continue
            url = entry.get("webpage_url") or entry.get("url")
            if not url:
                continue
            track = Track(
                title=entry.get("title", "제목 없음"),
                webpage_url=url,
                requested_by=requested_by,
                duration=entry.get("duration"),
                candidate_urls=(url,),
            )
            try:
                MusicBot._stream_info(track)
                return track
            except Exception:
                continue
        raise ValueError("YouTube와 SoundCloud에서 재생 가능한 음원을 찾지 못했습니다")

    @staticmethod
    def _stream_info(track: Track) -> tuple[str, dict[str, str]]:
        """재생 가능한 스트림 URL과 그 URL에 필요한 HTTP 헤더를 반환한다."""
        options = MusicBot._youtube_options("bestaudio/best")
        candidates = tuple(dict.fromkeys((track.webpage_url, *track.candidate_urls)))
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(candidate, download=False)
                if url := info.get("url"):
                    headers = dict(info.get("http_headers") or {})
                    return url, headers
            except Exception as exc:
                last_error = exc
                LOGGER.info("Stream candidate failed: %s", type(exc).__name__)
        if last_error:
            raise last_error
        raise ValueError("오디오 스트림 URL을 가져오지 못했습니다")

    @staticmethod
    def _download_track(track: Track) -> Path:
        """곡 오디오를 임시 디렉터리에 내려받아 그 디렉터리 경로를 반환한다.

        반환된 디렉터리의 정리는 호출 측 책임이다 (_cleanup_media 참조).
        """
        job_dir = Path(tempfile.mkdtemp(prefix="musicbot_"))
        options = MusicBot._youtube_options("bestaudio/best")
        options["outtmpl"] = str(job_dir / "audio.%(ext)s")
        candidates = tuple(dict.fromkeys((track.webpage_url, *track.candidate_urls)))
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.extract_info(candidate, download=True)
                if any(
                    p.is_file() and p.suffix not in {".part", ".ytdl"}
                    for p in job_dir.iterdir()
                ):
                    MusicBot._normalize_media(job_dir)
                    return job_dir
            except Exception as exc:
                last_error = exc
                LOGGER.info("Download candidate failed: %s", type(exc).__name__)
        shutil.rmtree(job_dir, ignore_errors=True)
        if last_error:
            raise last_error
        raise ValueError("오디오를 다운로드하지 못했습니다")

    def _cleanup_media(self, chat_id: int) -> None:
        media_dir = self.media_dirs.pop(chat_id, None)
        if media_dir is not None:
            shutil.rmtree(media_dir, ignore_errors=True)

    @staticmethod
    def _normalize_media(job_dir: Path) -> None:
        """다운로드된 오디오를 48kHz 스테레오 opus로 재인코딩한다.

        원본의 깨진 타임스탬프나 VBR 타이밍 때문에 재생이 빨라졌다
        느려졌다 하거나 끊기는 것을 막기 위함. 실패하면 원본을 그대로 둔다.
        """
        src = next(
            (
                p for p in job_dir.iterdir()
                if p.is_file() and p.suffix not in {".part", ".ytdl"}
            ),
            None,
        )
        if src is None:
            return
        dst = job_dir / "normalized.opus"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-i", str(src), "-vn",
                    "-ar", "48000", "-ac", "2",
                    "-c:a", "libopus", "-b:a", "128k",
                    str(dst),
                ],
                check=True,
                timeout=300,
            )
        except Exception as exc:
            LOGGER.info("Media normalization failed, using original: %s", type(exc).__name__)
            dst.unlink(missing_ok=True)
            return
        src.unlink(missing_ok=True)

    async def _play_track(self, chat_id: int, track: Track) -> None:
        # ntgcalls가 원격 URL을 소리 없이 조기 종료하는 환경이 있어
        # 곡을 로컬 파일로 내려받은 뒤 그 파일을 재생한다.
        LOGGER.info("Playing '%s' (%s) in %s", track.title, track.webpage_url, chat_id)
        media_dir = await asyncio.to_thread(self._download_track, track)
        media_files = [
            p for p in media_dir.iterdir()
            if p.is_file() and p.suffix not in {".part", ".ytdl"}
        ]
        if not media_files:
            shutil.rmtree(media_dir, ignore_errors=True)
            raise ValueError("다운로드된 오디오 파일이 없습니다")
        previous_dir = self.media_dirs.pop(chat_id, None)
        try:
            await self.calls.play(
                chat_id,
                MediaStream(
                    str(media_files[0]),
                    audio_flags=MediaStream.Flags.REQUIRED,
                    video_flags=MediaStream.Flags.IGNORE,
                ),
            )
        except Exception:
            shutil.rmtree(media_dir, ignore_errors=True)
            if previous_dir is not None:
                self.media_dirs[chat_id] = previous_dir
            raise
        self.media_dirs[chat_id] = media_dir
        if previous_dir is not None:
            shutil.rmtree(previous_dir, ignore_errors=True)
        unmuted = await self._try_unmute(chat_id)
        if unmuted is False:
            await self._notify_muted(chat_id)
        self.current[chat_id] = track
        self._reset_playback_state(chat_id)
        self.started_at[chat_id] = time.monotonic()
        self.remaining[chat_id] = float(track.duration) if track.duration else None
        self._schedule_advance(chat_id, track.duration)

    async def _try_unmute(self, chat_id: int) -> bool | None:
        """재생 계정이 실제로 음성채팅에서 음소거인지 확인하고 자동으로 해제한다.

        pytgcalls의 unmute()는 텔레그램 음소거가 아니라 송출 스트림의 상태만
        바꾸고, 원래 음소거가 아니면 False를 반환하므로 그 반환값으로 음소거를
        판단하면 안 된다. 반드시 그룹콜 참가자 상태를 직접 읽어 확인한다.

        반환값: True=정상 송출 가능, False=참가하지 않았거나 음소거, None=음성채팅 비활성.
        """
        in_call = True
        try:
            await self.calls.unmute(chat_id)  # 송출 스트림 음소거 해제
        except Exception:
            in_call = False
            LOGGER.info("Voice chat is not active or stream unmute failed for %s", chat_id)

        muted = await self._assistant_mute_state(chat_id)
        if muted is False:
            return True if in_call else False
        if muted is None:
            LOGGER.warning("Playback account is not visible in voice chat for %s", chat_id)
            return False

        # 1) 재생 계정이 스스로 음소거 해제를 시도한다.
        if await self._self_unmute(chat_id):
            return True
        # 2) 관리자가 뮤트한 경우 스스로 해제할 수 없으므로, 재생 계정을
        #    관리자로 승격한 뒤 봇의 관리자 권한으로 해제를 시도한다.
        await self._promote_assistant(chat_id, force=True)
        if await self._bot_unmute_assistant(chat_id):
            return True
        # 3) 최종 상태를 다시 확인해 그래도 음소거면 False를 반환한다.
        return await self._assistant_mute_state(chat_id) is False

    async def _get_input_group_call(self, client: TelegramClient, chat_id: int):
        """해당 채팅에서 진행 중인 그룹콜을 InputGroupCall로 반환한다."""
        try:
            chat = await client.get_entity(chat_id)
            if isinstance(chat, Channel):
                full = await client(GetFullChannelRequest(channel=chat))
            else:
                full = await client(GetFullChatRequest(chat_id=chat.id))
        except Exception as exc:
            LOGGER.info("Could not resolve group call for %s: %s", chat_id, exc)
            return None
        call = getattr(full.full_chat, "call", None)
        if call is None:
            return None
        return InputGroupCall(id=call.id, access_hash=call.access_hash)

    async def _assistant_mute_state(self, chat_id: int) -> bool | None:
        """그룹콜 참가자 목록에서 재생 계정의 음소거 여부를 읽는다.

        반환값: True=음소거, False=음소거 아님, None=확인 불가.
        """
        if self.assistant_id is None:
            return None
        call = await self._get_input_group_call(self.assistant_client, chat_id)
        if call is None:
            return None
        for attempt in range(4):
            try:
                result = await self.assistant_client(GetGroupParticipantsRequest(
                    call=call,
                    ids=[],
                    sources=[],
                    offset="",
                    limit=200,
                ))
            except Exception as exc:
                LOGGER.info("Could not fetch group call participants for %s: %s", chat_id, exc)
                return None
            for participant in result.participants:
                try:
                    peer_id = get_peer_id(participant.peer)
                except Exception:
                    continue
                if peer_id == self.assistant_id:
                    return bool(getattr(participant, "muted", False))
            if attempt < 3:
                # 입장 직후에는 참가자 목록에 아직 반영되지 않을 수 있다.
                await asyncio.sleep(0.7)
        return None

    async def _self_unmute(self, chat_id: int) -> bool:
        """재생 계정이 스스로 음소거를 해제한다. 관리자가 뮤트했다면 실패한다."""
        call = await self._get_input_group_call(self.assistant_client, chat_id)
        if call is None:
            return False
        try:
            await self.assistant_client(EditGroupCallParticipantRequest(
                call=call,
                participant=InputPeerSelf(),
                muted=False,
            ))
        except Exception as exc:
            LOGGER.info("Assistant could not unmute itself in %s: %s", chat_id, exc)
            return False
        LOGGER.info("Assistant unmuted itself in %s", chat_id)
        return True

    async def _bot_unmute_assistant(self, chat_id: int) -> bool:
        """봇의 관리자 권한으로 재생 계정의 음소거를 해제한다."""
        if self.assistant_ref is None:
            return False
        call = await self._get_input_group_call(self.bot_client, chat_id)
        if call is None:
            return False
        try:
            await self.bot_client(EditGroupCallParticipantRequest(
                call=call,
                participant=self.assistant_ref,
                muted=False,
            ))
        except Exception as exc:
            LOGGER.info("Bot could not unmute assistant in %s: %s", chat_id, exc)
            return False
        LOGGER.info("Bot unmuted assistant in %s", chat_id)
        return True

    async def _notify_muted(self, chat_id: int) -> None:
        try:
            await self.bot_client.send_message(
                chat_id,
                "⚠️ 재생 계정이 음성채팅에 없거나 음소거 상태라 소리가 나오지 않습니다.\n"
                "자동 해제에 실패했습니다. 다음 중 하나를 해주세요:\n"
                "1) 음성채팅 참가자 목록에서 재생 계정의 음소거를 직접 해제\n"
                "2) 재생 계정을 그룹 관리자로 지정\n"
                "3) 이 봇을 관리자로 지정하고 '관리자 추가'와 '음성채팅 관리' 권한 부여 "
                "(다음부터 자동으로 해제됨)",
            )
        except Exception:
            LOGGER.info("Could not send mute warning to %s", chat_id)

    def _reset_playback_state(self, chat_id: int) -> None:
        self.paused.discard(chat_id)
        self.started_at.pop(chat_id, None)
        self.remaining.pop(chat_id, None)

    async def _advance(self, chat_id: int) -> Track | None:
        self._cancel_advance(chat_id)
        self.current.pop(chat_id, None)
        self._reset_playback_state(chat_id)
        self._cleanup_media(chat_id)
        while self.queues[chat_id]:
            track = self.queues[chat_id].popleft()
            try:
                await self._play_track(chat_id, track)
            except Exception:
                LOGGER.exception(
                    "Failed to play queued track '%s' in %s; trying next",
                    track.title, chat_id,
                )
                continue
            return track
        try:
            await self.calls.leave_call(chat_id)
        except Exception:
            LOGGER.info("Voice chat already disconnected for %s", chat_id)
        self.queues.pop(chat_id, None)
        return None

    def _schedule_advance(self, chat_id: int, duration: float | None) -> None:
        self._cancel_advance(chat_id)
        if duration and duration > 0:
            self.advance_tasks[chat_id] = asyncio.create_task(
                self._advance_after(chat_id, float(duration))
            )

    def _cancel_advance(self, chat_id: int) -> None:
        task = self.advance_tasks.pop(chat_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    async def _advance_after(self, chat_id: int, duration: float) -> None:
        try:
            await asyncio.sleep(duration + 2)
            async with self.locks[chat_id]:
                next_track = await self._advance(chat_id)
            await self._announce_next(chat_id, next_track)
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.exception("Automatic queue advance failed for %s", chat_id)

    async def _register_bot_commands(self) -> None:
        await self.bot_client(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="",
            commands=[
                BotCommand("start", "봇 시작 및 안내"),
                BotCommand("help", "명령어 도움말"),
                BotCommand("play", "노래 재생 또는 대기열 추가"),
                BotCommand("join", "재생 계정을 음성채팅에 입장"),
                BotCommand("skip", "현재 곡 건너뛰기"),
                BotCommand("pause", "재생 일시정지"),
                BotCommand("resume", "재생 재개"),
                BotCommand("queue", "대기열 확인"),
                BotCommand("stop", "재생 종료 및 퇴장"),
                BotCommand("reload", "봇 재시작 (그룹 관리자 전용)"),
            ],
        ))

    async def _reload(self) -> None:
        await asyncio.sleep(0.5)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    @staticmethod
    def _youtube_options(format_name: str | None = None) -> dict:
        options = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "default_search": "ytsearch1",
            "extractor_args": {"youtube": {"player_client": ["mweb"]}},
        }
        # 설치된 JS 런타임을 자동 선택한다. 없으면 비워 두어 yt-dlp의
        # "런타임 없음" 경고와 잘못된 deno 호출을 피한다.
        js_runtime = next((r for r in ("deno", "node", "bun") if shutil.which(r)), None)
        if js_runtime:
            options["js_runtimes"] = {js_runtime: {}}
        else:
            options["js_runtimes"] = {}
        if format_name:
            options["format"] = format_name
        cookies_file = os.getenv("YOUTUBE_COOKIES_FILE")
        if not cookies_file and Path("youtube_cookies.txt").is_file():
            cookies_file = "youtube_cookies.txt"
        if cookies_file:
            options["cookiefile"] = cookies_file
        return options

    async def start_engine(self) -> None:
        await self.bot_client.start(bot_token=self.bot_token)
        await self.assistant_client.connect()
        if not await self.assistant_client.is_user_authorized():
            raise RuntimeError("ASSISTANT_SESSION이 유효하지 않습니다. generate_session.py로 다시 만드세요.")
        await self._bind_assistant()
        if self.assistant_ref is None:
            raise RuntimeError(
                "봇이 재생 계정을 찾지 못했습니다. 재생 계정으로 이 봇에게 /start 를 보낸 뒤 다시 실행하세요."
            )
        await self.calls.start()
        LOGGER.info("Music bot started")

    async def run(self) -> None:
        await self.start_engine()
        await self._register_bot_commands()
        await idle()


async def main() -> None:
    await MusicBot().run()


if __name__ == "__main__":
    asyncio.run(main())
