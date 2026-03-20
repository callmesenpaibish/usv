# 🎬 Ultimate Media Saver Bot

A production-ready Telegram bot built with **Pyrogram** + **yt-dlp** that downloads
videos, images, and galleries from YouTube, Instagram, TikTok, Reddit, Pinterest, and X/Twitter.

---

## 📁 Project Structure

```
media_saver_bot/
├── bot.py            ← Pyrogram client, handlers, progress UI
├── functions.py      ← Download engine (yt-dlp, gallery-dl, httpx)
├── requirements.txt  ← Python dependencies
├── .env.example      ← Environment variable template
└── README.md
```

---

## ⚙️ Prerequisites

### 1. System packages

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y ffmpeg

# macOS
brew install ffmpeg

# Arch Linux
sudo pacman -S ffmpeg
```

> **gallery-dl** is installed via pip (see below) but requires Python 3.8+.

### 2. Python 3.10+

```bash
python --version   # must be ≥ 3.10 (uses match statements / union types)
```

---

## 🚀 Setup

### Step 1 — Clone / copy files

```bash
mkdir media_saver_bot && cd media_saver_bot
# copy bot.py, functions.py, requirements.txt here
```

### Step 2 — Create a virtual environment

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
```

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 4 — Configure credentials

```bash
cp .env.example .env
# Edit .env and fill in your values:
#   TELEGRAM_API_ID    — from https://my.telegram.org/apps
#   TELEGRAM_API_HASH  — from https://my.telegram.org/apps
#   TELEGRAM_BOT_TOKEN — from @BotFather
```

Then load the env file before running:

```bash
export $(grep -v '^#' .env | xargs)
```

Or set the variables directly in `bot.py` (lines 19–21).

### Step 5 — Run the bot

```bash
python bot.py
```

---

## 🔑 Getting Credentials

| Credential | Where to get it |
|---|---|
| `API_ID` & `API_HASH` | [https://my.telegram.org/apps](https://my.telegram.org/apps) |
| `BOT_TOKEN` | Message [@BotFather](https://t.me/BotFather) → `/newbot` |

---

## 🍪 Cookies (age-restricted / private content)

Some Instagram Reels, age-gated YouTube videos, or Twitter posts require you to be logged in.
Export cookies from your browser and point the bot to them:

1. Install the **"Get cookies.txt LOCALLY"** browser extension.
2. Visit the platform while logged in and export `cookies.txt`.
3. Set `YTDLP_COOKIES=/path/to/cookies.txt` in `.env`.

---

## 🌐 Supported Platforms

| Platform | Engine | Notes |
|---|---|---|
| YouTube (Videos & Shorts) | yt-dlp | Best quality ≤ 2 GB |
| Instagram (Reels & Posts) | yt-dlp | Cookies recommended |
| TikTok | yt-dlp | Watermark-free |
| Reddit (Videos) | yt-dlp | Audio merged |
| Reddit (Galleries) | gallery-dl | Up to 10 items/batch |
| Pinterest | gallery-dl + yt-dlp | Images & video pins |
| X / Twitter | yt-dlp | Video tweets |

---

## 📐 Architecture Notes

### UUID temp directories
Every download gets its own `uuid4` folder under `TEMP_DIR`.  
Concurrent downloads from different users never collide.

### 2 GB wall
`yt-dlp` is configured with `filesize<2000M` format selectors.  
`_guard_size()` in `functions.py` enforces the limit as a second check.

### Progress feedback
- **Download phase** — yt-dlp progress hook → edits the status message every 3 s.
- **Upload phase** — Pyrogram's `progress` callback → live MB counter.
- `send_chat_action` keeps the "uploading video…" indicator active.

### Cleanup
`cleanup_dir()` is called in the `finally` block of `handle_url` in `bot.py`,
guaranteeing temp files are deleted even if an error occurs mid-way.

---

## 🐛 Error Handling

| Error | User message |
|---|---|
| Unsupported URL | ❌ Platform not supported |
| File > 2 GB | ❌ File size shown in MB |
| Private / age-restricted | 🔒 Private content notice |
| Generic failure | ⚠️ Error type + message |

---

## 🐳 Running with Docker (optional)

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg gallery-dl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

```bash
docker build -t media-saver-bot .
docker run -d --env-file .env media-saver-bot
```

---

## 📝 License

MIT — use freely, modify as needed.
