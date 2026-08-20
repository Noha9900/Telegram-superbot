# Telegram Community Super-Bot

Production-grade, modular Telegram group-management, media, filestore, and
VIP-billing bot built on Hydrogram (async Pyrogram fork), Motor (async
MongoDB), APScheduler, FFmpeg, and Pillow.

## 1. Directory Tree

```
telegram-superbot/
├── bot.py                     # Entry point: client init, plugin loader, scheduler
├── config.py                  # Fail-fast env-var configuration loader
├── database.py                # Motor async MongoDB access layer
├── requirements.txt
├── .env.example                # Copy to .env and fill in
├── Dockerfile
├── docker-compose.yml
├── telegram-superbot.service   # systemd unit for bare-VPS deployment
├── plugins/
│   ├── moderation.py           # Module A: welcome, cleaner, FSub, warn/ban/mute, rules, purge, anti-spy
│   ├── media.py                # Module B.1-B.3: screenshots, renamer/thumbnail, duplicate hash cleaner
│   ├── filestore.py            # Module B.4: encoded permanent file-store links
│   ├── vip.py                  # Module C: paywall, PIL invoice generator, expiry cron
│   ├── broadcast.py            # Module D: button-grid broadcast + scheduler, channel migrator
│   └── analytics.py            # Module E: activity tracker, /audit report
├── utils/
│   ├── decorators.py           # admin_only / owner_only / group_only guards
│   ├── helpers.py               # token encode/decode, duration parsing, etc.
│   ├── ffmpeg_utils.py          # async ffmpeg/ffprobe wrapper
│   └── invoice.py               # PIL invoice/receipt renderer
└── assets/                     # welcome banner, invoice template, fonts (you supply these)
```

## 2. Database Schemas (MongoDB / Motor)

### `users`
| Field | Type | Notes |
|---|---|---|
| `_id` | int | Telegram user id |
| `name`, `username` | str | |
| `warn_count` | int | current strike count |
| `warns` | list | `{chat_id, reason, date}` |
| `is_vip` | bool | |
| `vip_plan` | str \| null | `1month` / `3months` / `lifetime` |
| `vip_expiry` | datetime \| null | |
| `messages_count`, `media_count` | int | activity tracker counters |
| `active_hours` | dict | `{"14": 32, ...}` message count per UTC hour |
| `is_banned` | bool | |
| `saved_thumbnail_path` | str | for renamer/thumbnail engine |

### `chats`
| Field | Type | Notes |
|---|---|---|
| `_id` | int | chat id |
| `rules` | str \| null | |
| `fsub_channels` | list\[int\] | overrides global `FSUB_CHANNELS` |
| `auto_duplicate_clean` | bool | |
| `welcome_config` | dict | `{enabled, text, banner}` |
| `warn_limit` | int | default 3 |

### `filestore`
| Field | Type | Notes |
|---|---|---|
| `_id` | str | random token used in deep-link |
| `file_ids` | list\[int\] | message ids inside `DB_CHANNEL_ID` |
| `owner_id` | int | admin who created it |
| `is_public` | bool | |
| `is_revoked` | bool | |
| `access_count` | int | |

### `transactions`
| Field | Type | Notes |
|---|---|---|
| `_id` | str | tx id |
| `user_id`, `chat_id` | int | |
| `amount`, `plan` | str | |
| `status` | str | `pending` / `approved` / `rejected` |
| `invoice_id` | str \| null | |
| `screenshot_file_id` | str \| null | |

### `file_hashes`
Used by the duplicate-content cleaner: `{_id: hash, chat_id, file_id, message_id, media_type, created_at}`.

## 3. Setup

Installs straight into the system Python (no virtualenv).

```bash
pip3 install -r requirements.txt
# On Debian 12+ / Ubuntu 23.04+, the system Python is "externally managed"
# and will refuse the line above unless you add --break-system-packages:
#   pip3 install -r requirements.txt --break-system-packages
cp .env.example .env   # fill in API_ID, API_HASH, BOT_TOKEN, OWNER_ID, MONGO_URI, DB_CHANNEL_ID...
python3 bot.py
```

Requirements on the host: **ffmpeg** must be installed and on `$PATH`
(`apt install ffmpeg` on Debian/Ubuntu) for the screenshot / thumbnail
features to work outside Docker.

## 4. Deployment

### Docker (recommended)
```bash
docker compose up -d --build
docker compose logs -f bot
```
This starts a MongoDB container + the bot container, both restart
automatically and persist data in named volumes.

### Bare VPS via systemd (system Python, no venv)
```bash
sudo mkdir -p /opt/telegram-superbot /var/log/telegram-superbot
sudo cp -r . /opt/telegram-superbot
cd /opt/telegram-superbot

sudo apt update && sudo apt install -y python3 python3-pip ffmpeg
sudo pip3 install -r requirements.txt --break-system-packages

sudo useradd -r -s /usr/sbin/nologin botuser || true
sudo chown -R botuser:botuser /opt/telegram-superbot /var/log/telegram-superbot

sudo cp telegram-superbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-superbot
sudo systemctl status telegram-superbot
```

`ExecStart` in the unit file points at `/usr/bin/python3` (the system
interpreter) rather than a venv, so `pip3 install ... --break-system-packages`
must be run as root/with sudo so the packages land in the system site-packages
that `botuser` can import from.

## 5. Key Commands

| Command | Scope | Description |
|---|---|---|
| `/setwelcome`, `/welcometoggle` | admin | Configure the self-destructing welcomer |
| `/setrules`, `/rules` | admin/all | Group rules engine |
| `/warn`, `/unwarn`, `/warns` | admin | 3-strike warn engine |
| `/ban`, `/unban`, `/mute`, `/unmute` | admin | Manual moderation |
| `/cleandeleted` | admin | Purge deleted accounts |
| `/ss [count]` | all | FFmpeg screenshot generator |
| `/rename <name>` | admin | Renamer + thumbnail embed |
| `/setthumb`, `/delthumb` | admin | Manage saved thumbnail |
| `/dupclean` | admin | Toggle duplicate auto-delete |
| `/batch`, `/done`, `/link`, `/revoke` | admin | FileStore link engine |
| `/vip`, `/paid` | user | VIP purchase workflow |
| `/setprice` | owner | Configure plan pricing |
| `/broadcast`, `/broadcast_at` | owner | Button-grid broadcast (instant/scheduled) |
| `/migrate` | owner | Channel-to-channel migrator |
| `/audit` | admin | Deep analytics report |

## 6. Notes & Production Considerations

- **Rate limits**: broadcast/migrate loops honor `FloodWait` from Telegram
  and back off automatically; for very large user bases consider batching
  with a delay between sends.
- **Security**: never commit `.env`; `SUDO_USERS`/`OWNER_ID` gate all
  destructive/administrative commands via `utils/decorators.py`.
- **Scaling**: Motor's `AsyncIOMotorClient` is created once and pooled
  (`maxPoolSize=50`); do not instantiate additional clients per request.
- **Assets**: supply your own `assets/welcome_default.jpg`,
  `assets/invoice_template.png`, and TTF fonts — the invoice generator
  falls back to a plain canvas + default bitmap font if they're absent,
  so the bot won't crash, but output looks best with real assets.
