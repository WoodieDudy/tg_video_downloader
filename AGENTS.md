# AGENTS.md

Руководство для разработчиков и AI-агентов, работающих с репозиторием `yt-tg-bot`.

## Назначение и архитектура

`yt-tg-bot` — асинхронный Telegram-бот (`aiogram` 3.x, Python 3.12+) для скачивания видео/Shorts/аудио с YouTube через `yt-dlp` + `ffmpeg` + `deno` и отправки файлов до 2 ГБ через локальный сервер `telegram-bot-api`.

### Сервисы (`docker-compose.yml`)
- `telegram-bot-api` (`aiogram/telegram-bot-api:latest`, `TELEGRAM_LOCAL=1`): локальный Bot API на `:8081` в Docker-сети; разделяет том `downloads_data:/data/downloads` с `bot`.
- `bot`: основной процесс (`python -m bot.main`), внутри которого работает долгоживущий headless Chromium (`ChromeSessionManager`) на постоянном томе `chrome_profile:/data/chrome-profile`.
- `chrome-auth` (профиль `auth`): одноразовый интерактивный контейнер (`docker/auth-entrypoint.sh` с `Xvfb` + `openbox` + `x11vnc` + `noVNC` на `127.0.0.1:6080`) для первичной авторизации в Google без флагов автоматизации.

### Структура кода
- `bot/config.py` — настройки `Settings` (`pydantic-settings`), парсинг `ADMIN_USER_IDS` / `ALLOWED_USER_IDS`.
- `bot/main.py` — сборка `Bot` (через `TelegramAPIServer.from_base(..., is_local=...)` и увеличенный `bot_api_timeout_seconds`), регистрация роутеров и middleware, lifecycle `ChromeSessionManager`.
- `bot/middlewares/auth.py` — `WhitelistMiddleware`, пропускающий запросы авторизованных пользователей в ЛС и разрешённых групповых чатов (`allowed_chats`, неавторизованные группы игнорируются молча).
- `bot/services/access.py` — `AccessManager`: роли админов, динамическое добавление/удаление пользователей по `@username` или `user_id`, привязка `@username -> user_id`, список разрешённых групповых чатов (`allowed_chats`), атомарное сохранение в `/data/state/users.json`.
- `bot/services/chrome_cookies.py` — `ChromeSessionManager`: persistent Playwright Chromium (`channel="chromium"`), очистка зависших `Singleton*` lock-файлов при старте, навигация на `youtube.com`, экспорт cookies через CDP `Network.getAllCookies` в Netscape-формат (`/data/cookies/youtube.txt`), проверка `has_authenticated_cookies`.
- `bot/services/downloader.py` — `YouTubeDownloader` и общие утилиты (`probe_media`, `compress_video_for_telegram`, `build_telegram_ffmpeg_cmd`): `yt-dlp` метаданные и скачивание, определение вертикальных Shorts (`is_portrait`), обрезка по таймкодам (`--download-sections`, `--force-keyframes-at-cuts`), `ffprobe` метаданные, прогресс-коллбэк и автоповтор (`_with_cookie_retry`) с обновлением cookies при auth/bot ошибках.
- `bot/services/instagram.py` — `InstagramDownloader`: скачивание публичных Instagram Reels без аккаунта через `kkinstagram.com` → `eeinstagram.com` (с CDN fallback `*.fbcdn.net` → `scontent.cdninstagram.com`) → `yt-dlp` fallback, обрезка и MP3.
- `bot/services/tiktok.py` — `TikTokDownloader`: скачивание TikTok без водяного знака через `yt-dlp` (с поддержкой коротких `vm.tiktok.com` / `vt.tiktok.com`).
- `bot/services/cache.py` — `MediaCache`: персистентный кэш Telegram `file_id` в `/data/state/media_cache.json` (с автоинвалидацией при `TelegramBadRequest`).
- `bot/services/alerts.py` — `AdminAlertMonitor`: уведомления администраторам при слёте сессии Google или нехватке места на диске с кулдауном.
- `bot/services/jobs.py` — `PendingJobStore` (TTL-хранилище интерактивных запросов, чтобы `callback_data` оставался < 64 байт) и `asyncio.Semaphore` для ограничения параллельных скачиваний.
- `bot/handlers/commands.py` — `/start`, `/help`, `/status`, `/refresh_cookies`, `/clear_cache`, `/add`, `/remove`, `/users`, `/add_chat`, `/remove_chat`, `/chats`, `/cancel` + `my_chat_member` автоактивация группы при добавлении админом или пользователем из белого списка (иначе бот выходит из группы).
- `bot/handlers/youtube.py` — обработка групповых ссылок (`handle_group_shorts`: Shorts/Reels/TikTok без тега + любая поддерживаемая ссылка при `@упоминании` бота, работает и при включённом Privacy Mode), интерактивное меню в ЛС (качество, MP3, обрезка, отправка видео vs файлом `send_document`), кэш `file_id`, FSM `TrimStates.waiting_for_range`.
- `bot/utils/parsers.py` — нормализация URL YouTube/Shorts, Instagram Reels/Posts и TikTok, парсинг таймкодов и форматирование размеров/длительности.

## Критичные инварианты и подводные камни

1. **Блокировка профиля Chromium (`chrome_profile`)**:
   - `bot` и `chrome-auth` используют один `--user-data-dir=/data/chrome-profile`. Не запускать их одновременно: перед `docker compose --profile auth up -d chrome-auth` обязательно останавливать `bot`.
   - Для контейнеров обязательны `--no-sandbox`, `--disable-dev-shm-usage` и `shm_size: "1gb"`.
   - При наличии `libatomic1` в `Dockerfile` работает встроенный node-драйвер Playwright — не удалять этот пакет из `apt-get install`.

2. **Cookies и конкурентность `yt-dlp`**:
   - `cookies.txt` всегда пишется атомарно (`tmp` -> `os.replace`).
   - Для каждой задачи `YouTubeDownloader` копирует `cookies.txt` во временную папку задачи (`get_cookies_snapshot`), чтобы параллельные запуски `yt-dlp` не перезаписывали общий файл cookies.
   - `yt-dlp` для YouTube требует JS-рантайм (`deno` установлен в `/usr/local/bin/deno` и передаётся через `--js-runtimes`).

3. **Вертикальные YouTube Shorts**:
   - У 1080p Shorts `height=1920`, `width=1080`. Фильтр формата должен проверять `width<=H` при `is_portrait=True` и `height<=H` при `is_portrait=False`, а короткая сторона для меню считается как `min(width, height)`.
   - При отправке `send_video` передаются `width`/`height` из `ffprobe` (с учётом rotation-тегов) и `supports_streaming=True`.

4. **Сжатие для Telegram vs отправка файлом (`send_document`)**:
   - При отправке как видео (`send_video`, включая автоскачивание Shorts в группах) видео перекодируется через `ffmpeg` (`build_telegram_ffmpeg_cmd`: `libx264`, `-profile:v high`, `-pix_fmt yuv420p`, `-crf 24`, `-preset medium`, `scale=trunc(iw/2)*2:trunc(ih/2)*2`, `aac 128k stereo`, `-movflags +faststart`). Если исходник уже совместим с Telegram и сжатый файл не меньше, сохраняется исходник.
   - Кнопка `dl:<job_id>:best_doc` или переключатель `toggledoc:<job_id>` (`job.as_document`) пропускает перекодирование (`compress=False`) и отправляет файл через `bot.send_document` с очищенным Unicode-именем (`sanitize_telegram_filename`).

5. **Безопасность и секреты**:
   - Никогда не коммитить `.env`, `users.json`, дампы cookies и профиль Chrome.

## Проверка изменений

После любых правок в Python-коде запускать unit-тесты:

```bash
.venv/bin/pytest -v
```

При изменении зависимостей, `Dockerfile` или `docker/auth-entrypoint.sh` проверять сборку и старт контейнеров через `docker compose build && docker compose up -d` и контролировать `Refreshed Chrome cookies ... authenticated=True` в логах `bot`.
