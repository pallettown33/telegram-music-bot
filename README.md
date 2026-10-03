# Telegram Voice Chat Music Bot

명령 수신은 `aiogram 3` Router와 Long Polling, 음성채팅 송출은 기존 Telethon 재생 계정과 PyTgCalls를 사용합니다.

그룹 음성 채팅에 참여해 YouTube 검색 또는 URL의 오디오를 재생합니다. 명령용 봇 계정과 음성 채팅 재생용 사용자 계정을 함께 사용합니다. Telegram은 음성 채팅 참여를 일반 봇 계정에 허용하지 않습니다.

## 준비

1. [my.telegram.org/apps](https://my.telegram.org/apps)에서 `API ID`, `API Hash`를 만듭니다.
2. `@BotFather`에서 명령용 봇 토큰을 만들고 봇을 대상 그룹에 추가합니다. 재생 계정을 자동으로 들이려면 이 봇을 관리자로 지정하고 **사용자 초대** 권한을 줍니다. **관리자 추가** 권한까지 주면 봇이 재생 계정을 자동으로 관리자로 지정해 Telewiki 같은 관리봇의 신규 멤버 인증(캡챠)으로부터 보호합니다.
3. 재생 전용 Telegram 사용자 계정을 준비합니다. 그룹에 미리 넣지 않아도 됩니다. 봇이 `/play` 또는 `/join` 때 그 계정을 직접 초대하고, Telegram이 봇의 직접 초대를 막으면 봇이 만든 1회용 초대 링크로 입장시킵니다. 가입 승인이 필요한 그룹이면 봇이 요청을 승인합니다. 봇에는 **사용자 초대** 권한이 필요합니다.
4. `/join` 은 음성채팅이 없으면 새로 만들고, 있으면 재생 계정을 그 통화로 넣습니다. `/play` 도 통화를 연 뒤 재생합니다.
5. 서버에 Python 3.10 이상과 `ffmpeg`를 설치합니다.

## 설치 및 실행

```bash
cd telegram_music_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env의 API ID, API Hash를 실제 값으로 변경
python generate_session.py
# 출력된 ASSISTANT_SESSION=... 값을 .env에 붙여 넣기
# .env의 TELEGRAM_BOT_TOKEN도 실제 값으로 변경
```

현재 실행 파일은 `main.py`입니다.

```bash
python main.py
```

이미 `telegram_music_bot.session` 같은 Telethon 세션 파일이 있으면 `ASSISTANT_SESSION` 대신
`ASSISTANT_SESSION_FILE=telegram_music_bot.session`을 써도 됩니다.

Ubuntu/Debian에서 `ffmpeg` 설치:

```bash
sudo apt update && sudo apt install -y ffmpeg
```

## 명령

- `/play 노래 제목` 또는 `/play YouTube URL`: 검색 후 재생하거나 대기열에 추가. YouTube 결과가 차단되면 다음 결과 또는 SoundCloud를 시도합니다.
- `/skip`: 현재 곡 건너뛰기
- `/pause`, `/resume`: 일시정지와 재개
- `/stop`: 대기열을 비우고 음성 채팅 퇴장
- `/join`: 재생 계정을 이 그룹에 초대하고 음성 채팅으로 불러오기. 통화가 없으면 새로 엽니다.
- `/reload`: 봇 프로세스 재시작. `ADMIN_ONLY`와 무관하게 항상 그룹 관리자만 사용할 수 있습니다.
- `/queue`: 재생 중인 곡과 대기열 보기
- `/help`: 명령어 요약 보기

기본값(`ADMIN_ONLY=false`)에서는 그룹 멤버 누구나 명령을 실행할 수 있습니다. 관리자만 쓰게 하려면 `ADMIN_ONLY=true`로 설정하세요.

`REQUIRED_CHANNEL`을 설정하면 해당 채널을 구독한 멤버만 봇 명령과 버튼을 사용할 수 있습니다(필수 구독). `REQUIRED_CHANNEL_LINK`에는 구독 안내 버튼에 표시할 가입 링크를 적습니다. 비공개 채널을 쓸 경우 봇이 그 채널의 멤버(가능하면 관리자)여야 구독 여부를 확인할 수 있습니다.

## 문제 해결

### "재생 중"이라고 뜨는데 소리가 안 나올 때

1. **재생 계정의 발언 권한을 확인하세요.** 가장 흔한 원인입니다. 음성채팅이 "관리자만 발언 가능"으로 설정되어 있으면 재생 계정은 소리를 낼 수 없습니다. 재생 계정을 그룹 관리자로 지정하거나, 음성채팅 권한에서 모든 참가자의 발언을 허용하세요. 봇이 음소거를 감지하면 그룹에 경고 메시지를 보냅니다.
2. 봇 로그에 `Stream candidate failed`가 반복되면 YouTube가 스트림을 거부하는 것입니다. `youtube_cookies.txt`가 최신인지 확인하고, JS 런타임(`deno`/`node`/`bun`) 중 하나가 설치되어 있는지 확인하세요.
3. 재생 계정이 음성채팅에 참여해 있는데도 계속 무음이면 `/stop` 후 `/play`로 다시 시도하세요.

### 관리봇(Telewiki, GroupHelp 등)이 재생 계정을 강퇴하거나 뮤트할 때

관리봇은 새로 들어온 멤버인 재생 계정에 캡챠(퀴즈/버튼 인증)를 보내고, 응답이 없으면 강퇴하거나 뮤트합니다. 해결 방법은 다음과 같습니다.

1. **봇에게 '관리자 추가' 권한을 주세요(권장).** 그러면 봇이 재생 계정이 들어올 때마다(수동으로 추가된 경우도 포함) 자동으로 최소 권한 관리자로 지정합니다. 관리자는 다른 관리봇이 강퇴/뮤트할 수 없고 음성채팅 발언도 항상 허용됩니다. 그룹당 한 번만 설정하면 이후에는 매번 손댈 필요가 없습니다.
2. 권한을 줄 수 없다면 **재생 계정을 그룹 관리자로 직접 지정**하세요. 역시 관리봇의 제한 대상에서 제외됩니다.
3. 둘 다 어려운 경우 봇이 최선 노력으로 캡챠에 자동 응답합니다. 버튼형 인증은 인증 버튼을 누르고, `3 + 4` 같은 간단한 산수 퀴즈 투표는 계산해서 투표합니다(재생 계정 입장 후 120초 동안만 동작). 복잡한 캡챠는 실패할 수 있으므로 1~2번 방법을 우선하세요.
4. 재생 계정이 이미 강퇴된 상태라면 봇이 경고 메시지를 보냅니다. 관리자 지정 후 `/join`으로 다시 불러오세요.

## 운영 주의사항

- YouTube와 음원 권리자의 약관 및 저작권 규정을 준수하는 콘텐츠만 재생하세요.
- `TELEGRAM_BOT_TOKEN`, `API ID`, `API Hash`, `ASSISTANT_SESSION`은 `.env`에만 보관하고 저장소에 커밋하지 마세요. 세션 문자열을 아는 사람은 재생 계정에 접근할 수 있습니다.
- `ASSISTANT_SESSION_FILE`을 쓰는 경우에도 해당 `.session` 파일은 재생 계정 로그인 정보이므로 같은 수준으로 취급하세요.
- `ASSISTANT_INVITE_LINK`는 봇이 1회용 링크를 만들 수 없을 때만 쓰는 예비 수단입니다. 자동 초대가 실패하면 봇을 관리자로 지정하고 **사용자 초대** 권한을 준 뒤 `/join`을 다시 실행하세요.
- YouTube 차단 대응을 위해 yt-dlp PO Token provider를 사용합니다. VPS에는 `/opt/bgutil-ytdlp-pot-provider`와 Deno provider가 필요합니다.
- yt-dlp JS 런타임은 `deno` → `node` → `bun` 순으로 설치된 것을 자동 선택합니다. 셋 중 하나는 설치해 두는 것이 안전합니다.
- `youtube_cookies.txt`를 작업 디렉터리에 두면 자동으로 쿠키를 사용합니다. 다른 경로를 쓰려면 `.env`의 `YOUTUBE_COOKIES_FILE`로 지정하세요.
- 시작 시 봇이 재생 계정을 찾지 못하면 재생 계정으로 봇에게 `/start`를 보낸 뒤 다시 시작하세요.
- 현재 버전은 곡 메타데이터의 길이를 기준으로 자동으로 다음 곡을 시작합니다. 라이브 스트림처럼 길이가 없는 소스는 `/skip`으로 넘길 수 있습니다.
