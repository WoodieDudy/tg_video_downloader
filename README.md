# YouTube, Instagram Reels & TikTok Telegram Bot

Telegram-бот для скачивания видео с YouTube, YouTube Shorts, Instagram Reels и TikTok в Docker с поддержкой файлов до **2 ГБ** и автоматическим поддержанием авторизованной Google/YouTube-сессии в headless Chromium.

## Возможности

- **До 2 ГБ на файл** через локальный `telegram-bot-api` (`--local`).
- **Постоянная сессия Google в headless Chromium**: первичная авторизация выполняется один раз в запасной Google-аккаунт через встроенный **noVNC**; профиль сохраняется в Docker volume, а cookies регулярно обновляются через CDP (`Network.getAllCookies`) и автоматически перевыгружаются при ошибках авторизации.
- **Защита YouTube-аккаунта от блокировки**:
  - встроенный sidecar **`bgutil-pot-provider`** (PO Token): публичные видео сначала скачиваются **без cookies**, а Chrome-сессия подключается только при необходимости (18+, bot-check);
  - общая FIFO-очередь на все запросы (получение метаданных и скачивания от всех пользователей выполняются строго по одному через `MAX_CONCURRENT_DOWNLOADS=1` с паузой `QUEUE_TASK_GAP_SECONDS=2.0`, лимитом очереди `QUEUE_MAX_SIZE=15` и таймаутами `QUEUE_WAIT_TIMEOUT_SECONDS=120`, `FETCH_INFO_TIMEOUT_SECONDS=45`, `DOWNLOAD_TIMEOUT_SECONDS=300`, `--socket-timeout 20`);
  - переиспользование `info.json` (`--load-info-json`) — вдвое меньше обращений к YouTube;
  - фоновое обновление cookies раз в 4 часа (`COOKIE_REFRESH_INTERVAL_MINUTES=240`), паузы `--sleep-requests 1` и антиспам-лимит для не-админов (`USER_COOLDOWN_SECONDS=20`, `USER_HOURLY_LIMIT=20`).
- **Лимиты для групповых чатов**: в группах не скачиваются видео длиннее **2 минут** (`GROUP_MAX_DURATION_SECONDS=120`), прямые трансляции и файлы больше **150 МБ** (`GROUP_MAX_FILE_SIZE_MB=150`).
- **Автоуведомления админу**: бот присылает уведомление в ЛС, если слетела сессия Google в Chrome или на диске сервера осталось мало свободного места (`LOW_DISK_WARN_GB`).
- **Кэш Telegram `file_id`**: повторные запросы одного и того же видео (с тем же качеством/режимом) отдаются мгновенно без повторного скачивания и сжатия.
- **Instagram Reels и TikTok без аккаунта**:
  - публичные Instagram Reels (`/reel/...`, `/reels/...`, `/p/...`) через `kkinstagram` / `eeinstagram` (+ CDN fallback на `scontent.cdninstagram.com` и `yt-dlp`);
  - видео TikTok (`tiktok.com/@.../video/...`, `vm.tiktok.com/...`, `vt.tiktok.com/...`) без водяного знака через `yt-dlp`.
- **Отправка как видео или как файл (документ)**:
  - при отправке **как видео** ролик автоматически оптимизируется и сжимается для Telegram (`H.264 High`, `yuv420p`, `CRF 24`, `AAC 128k`, `+faststart`) — минимальный размер без заметной потери качества;
  - быстрая кнопка **«📄 Лучшее (файлом)»** и переключатель **«📄 Отправлять файлом: ❌ / ✅»** отправляют исходный файл без перекодирования.
- **YouTube Shorts** с сохранением вертикальных пропорций (`width`/`height` через `ffprobe`).
- **Выбор качества**: `2160p` … `360p`, `Лучшее`, `Аудио MP3`.
- **Обрезка по таймкодам**: инлайн в сообщении (`https://youtu.be/... 01:15-02:30`) или через кнопку **«✂️ Обрезать»**.
- **Работа в групповых чатах (с Privacy Mode и без)**:
  - **Активация группы**: бот активируется в группе только если его добавил админ или пользователь из белого списка (`/add @username`); если бота добавит посторонний, бот напишет обратиться к администратору и выйдет из чата.
  - **Без доступа ко всем сообщениям (Privacy Mode включён)**: Telegram доставляет боту команды `/dl <ссылка>` (или `/dl@your_bot <ссылка>`, а также reply командой `/dl` на чужое сообщение со ссылкой) и reply на сообщения бота.
  - **Автоматически без команды**: если выдать права администратора или отключить Privacy Mode в `@BotFather`, бот автоматически ловит и скачивает **YouTube Shorts**, **Instagram Reels** и **TikTok** (обычные длинные видео без команды/упоминания игнорируются).
- **Доступ по ролям и `@username`**: администраторы задаются в `.env` (`ADMIN_USER_IDS` и/или `ADMIN_USERNAMES`), остальных пользователей можно добавлять прямо в боте по `@username` или ID (`/add`, `/remove`, `/users`) — при первом сообщении боту `@username` автоматически привязывается к `user_id`.

---

## 1. Подготовка

1. Создай бота у [@BotFather](https://t.me/BotFather) и получи `BOT_TOKEN`.
2. Получи `API_ID` и `API_HASH` на [my.telegram.org/apps](https://my.telegram.org/apps) (нужны для Local Bot API сервера).
3. Узнай свой цифровой Telegram ID (например, через [@userinfobot](https://t.me/userinfobot)).
4. Скопируй конфиг и заполни переменные:

```bash
cp .env.example .env
nano .env
```

Основные переменные в `.env`:
- `BOT_TOKEN` — токен Telegram-бота
- `BOT_USERNAME` — *(опционально)* username бота без `@` (если не задан, определяется автоматически через `getMe()`)
- `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` — ключи с `my.telegram.org`
- `ADMIN_USER_IDS` — числовые Telegram ID администраторов через запятую
- `ADMIN_USERNAMES` — *(опционально)* Telegram `@username` администраторов через запятую (привязываются к ID при первом сообщении)
- `ALLOWED_USER_IDS` — начальный белый список ID (можно совпадать с `ADMIN_USER_IDS`)
- `VNC_PASSWORD` — пароль для одноразового входа в Chrome через noVNC

---

## 2. Сборка и первичная авторизация в Google (1 раз)

Собери образы:
```bash
docker compose build
```

Запусти временный контейнер `chrome-auth` для входа в запасной Google-аккаунт (бот в этот момент не должен быть запущен, чтобы не делить блокировку профиля `chrome_profile`):
```bash
docker compose stop bot
docker compose --profile auth up -d chrome-auth
```

Если бот работает на удалённом сервере, пробрось порт `6080` по SSH:
```bash
ssh -N -L 6080:127.0.0.1:6080 user@your-server-ip
```

Открой в браузере на своём компьютере:
```text
http://localhost:6080/vnc.html
```
1. Введи пароль из `VNC_PASSWORD` (если задан).
2. В открывшемся окне Chromium войди в запасной Google-аккаунт и открой `youtube.com` (прими cookie-баннер, если появится, и убедись, что отображается аватар аккаунта).
3. Останови `chrome-auth` (Chromium корректно сохранит профиль на диск):
```bash
docker compose --profile auth stop chrome-auth
```

---

## 3. Запуск бота

```bash
docker compose up -d
docker compose logs -f bot
```

Проверь в Telegram командой `/status`, что:
- **Headless Chrome:** 🟢 Работает
- **Сессия Google:** ✅ Авторизован

---

## 4. Повторный вход, если сессия слетит

```bash
docker compose stop bot
docker compose --profile auth up -d chrome-auth
# зайти на http://localhost:6080/vnc.html и пройти вход / проверку
docker compose --profile auth stop chrome-auth
docker compose start bot
```

---

## 5. Использование и команды

- Отправь ссылку на видео или Shorts:
  - `https://www.youtube.com/watch?v=dQw4w9WgXcQ`
  - `https://youtube.com/shorts/abcdefghijk`
- Или сразу с таймкодом:
  - `https://youtu.be/dQw4w9WgXcQ 00:30-01:45`

### Команды пользователя
- `/start`, `/help` — справка
- `/cancel` — отменить ввод таймкода

### Админ-команды
- `/status` — состояние headless Chrome, cookies, авторизации, кэша, очереди и диска
- `/add @username` (или `/add 123456789`) — разрешить доступ; `@username` привяжется к `user_id` при первом сообщении боту
- `/remove @username` (или ID) — забрать доступ
- `/users` — список админов, привязанных пользователей и ожидающих `@username`
- `/add_chat [chat_id]` — разрешить автоскачивание Shorts в групповом чате (или просто добавь бота в группу как админ)
- `/remove_chat [chat_id]` — отключить групповой чат
- `/chats` — список разрешённых групповых чатов
- `/refresh_cookies` — принудительно обновить страницу YouTube в Chrome и пересохранить `cookies.txt`
- `/clear_cache` — очистить кэш Telegram `file_id`

> **Режим работы в групповых чатах:**
> - При включённом Privacy Mode («не имеет доступа к сообщениям») Telegram не присылает боту обычные сообщения (даже с `@упоминанием`). В этом режиме используй `/dl <ссылка>` или отвечай командой `/dl` на сообщение со ссылкой.
> - Чтобы бот сам ловил ссылки на Shorts/Reels/TikTok без `/dl` — выдай ему права **администратора** в чате либо отключи Group Privacy в `@BotFather` (`/setprivacy` → `Disable`, после чего удали бота из чата и добавь снова).

---

## 6. Локальная разработка и тесты

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -v
```
