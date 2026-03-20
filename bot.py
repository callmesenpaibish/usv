"""
Ultimate Media Saver Bot — bot.py
Main Pyrogram client, handlers, and progress reporting.
"""

import asyncio
import logging
import os
import time

from pyrogram import Client, filters
from pyrogram.enums import ChatAction, ParseMode
from pyrogram.types import Message

from functions import (
    download_media,
    cleanup_dir,
    MediaResult,
    MediaType,
    DownloadError,
    FileTooLargeError,
    PrivateMediaError,
    UnsupportedURLError,
)

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("MediaSaverBot")

# ─── Config (use env vars or fill directly) ──────────────────────────────────
API_ID   = int(os.getenv("TELEGRAM_API_ID",   "YOUR_API_ID"))
API_HASH =     os.getenv("TELEGRAM_API_HASH",  "YOUR_API_HASH")
BOT_TOKEN =    os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")

# ─── Client ──────────────────────────────────────────────────────────────────
app = Client(
    "ultimate_media_saver",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# ─── Helpers ─────────────────────────────────────────────────────────────────
PROGRESS_THROTTLE = 3          # seconds between progress edits (avoid flood-wait)
PROGRESS_BAR_LEN  = 14         # character width of the bar


def _build_bar(current: int, total: int) -> str:
    """Return a Unicode progress bar string."""
    if total <= 0:
        return "⬛" * PROGRESS_BAR_LEN
    filled = int(PROGRESS_BAR_LEN * current / total)
    bar    = "🟦" * filled + "⬛" * (PROGRESS_BAR_LEN - filled)
    pct    = current * 100 // total
    speed  = ""
    return f"{bar} {pct}%"


def make_progress_callback(status_msg: Message, label: str):
    """
    Returns an async callable compatible with Pyrogram's `progress` parameter.
    Edits `status_msg` at most once every PROGRESS_THROTTLE seconds.
    """
    last_edit: list[float] = [0.0]

    async def callback(current: int, total: int) -> None:
        now = time.monotonic()
        if now - last_edit[0] < PROGRESS_THROTTLE and current < total:
            return
        last_edit[0] = now
        bar  = _build_bar(current, total)
        size = f"{current / 1_048_576:.1f} / {total / 1_048_576:.1f} MB"
        try:
            await status_msg.edit_text(
                f"**{label}**\n{bar}\n`{size}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass  # Flood-wait or message-not-modified — silently skip

    return callback


def make_download_progress_hook(status_msg: Message):
    """
    Returns a yt-dlp progress_hooks dict that updates the status message.
    Runs in a thread, so we use asyncio.run_coroutine_threadsafe.
    """
    loop = asyncio.get_event_loop()
    last_edit: list[float] = [0.0]

    def hook(d: dict) -> None:
        if d["status"] not in ("downloading", "finished"):
            return
        now = time.monotonic()
        if now - last_edit[0] < PROGRESS_THROTTLE and d["status"] != "finished":
            return
        last_edit[0] = now

        if d["status"] == "finished":
            text = "⚙️ **Processing / merging…**"
        else:
            downloaded = d.get("downloaded_bytes", 0) or 0
            total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            bar        = _build_bar(downloaded, total)
            eta        = d.get("eta") or 0
            speed_raw  = d.get("speed") or 0
            speed_mb   = f"{speed_raw / 1_048_576:.1f} MB/s" if speed_raw else "—"
            text = (
                f"📥 **Downloading…**\n"
                f"{bar}\n"
                f"`ETA {eta}s  •  {speed_mb}`"
            )

        asyncio.run_coroutine_threadsafe(
            _safe_edit(status_msg, text),
            loop,
        )

    return hook


async def _safe_edit(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass


# ─── Handlers ────────────────────────────────────────────────────────────────

@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message) -> None:
    await message.reply_text(
        "👋 **Ultimate Media Saver Bot**\n\n"
        "Send me any link from:\n"
        "• YouTube (Videos & Shorts)\n"
        "• Instagram (Reels & Posts)\n"
        "• TikTok\n"
        "• Reddit (Videos & Galleries)\n"
        "• Pinterest\n"
        "• X / Twitter\n\n"
        "I'll download and send it right here! 🚀",
        parse_mode=ParseMode.MARKDOWN,
    )


@app.on_message(filters.command("help"))
async def cmd_help(client: Client, message: Message) -> None:
    await message.reply_text(
        "**How to use:**\n"
        "1. Simply paste any supported URL.\n"
        "2. Wait while I download it.\n"
        "3. Receive your file!\n\n"
        "**Limits:**\n"
        "• Max file size: **2 GB**\n"
        "• Private / age-gated content may not work.\n\n"
        "**Commands:**\n"
        "`/start` — Welcome message\n"
        "`/help`  — This message",
        parse_mode=ParseMode.MARKDOWN,
    )


@app.on_message(filters.text & filters.private & ~filters.command(["start", "help"]))
async def handle_url(client: Client, message: Message) -> None:
    url = message.text.strip()

    # Basic URL sanity check
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.reply_text(
            "⚠️ Please send a valid URL starting with `http://` or `https://`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status_msg = await message.reply_text(
        "🔍 **Analysing URL…**",
        parse_mode=ParseMode.MARKDOWN,
    )

    result: MediaResult | None = None

    try:
        # Show download progress via yt-dlp hook
        progress_hook = make_download_progress_hook(status_msg)

        await _safe_edit(status_msg, "📥 **Starting download…**")
        result = await download_media(url, progress_hook=progress_hook)

        # ── Video / GIF ────────────────────────────────────────────────────
        if result.media_type == MediaType.VIDEO:
            await _safe_edit(status_msg, "📤 **Uploading video…**")
            await client.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)

            upload_progress = make_progress_callback(status_msg, "📤 Uploading…")

            await client.send_video(
                chat_id=message.chat.id,
                video=result.file_path,
                thumb=result.thumbnail_path,
                caption=result.caption or "",
                duration=result.duration or 0,
                width=result.width or 0,
                height=result.height or 0,
                supports_streaming=True,
                progress=upload_progress,
            )

        # ── Audio ─────────────────────────────────────────────────────────
        elif result.media_type == MediaType.AUDIO:
            await _safe_edit(status_msg, "📤 **Uploading audio…**")
            await client.send_chat_action(message.chat.id, ChatAction.UPLOAD_AUDIO)

            upload_progress = make_progress_callback(status_msg, "📤 Uploading audio…")

            await client.send_audio(
                chat_id=message.chat.id,
                audio=result.file_path,
                thumb=result.thumbnail_path,
                caption=result.caption or "",
                progress=upload_progress,
            )

        # ── Single Image ──────────────────────────────────────────────────
        elif result.media_type == MediaType.IMAGE:
            await _safe_edit(status_msg, "📤 **Uploading image…**")
            await client.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)

            await client.send_photo(
                chat_id=message.chat.id,
                photo=result.file_path,
                caption=result.caption or "",
            )

        # ── Image Gallery ─────────────────────────────────────────────────
        elif result.media_type == MediaType.GALLERY:
            await _safe_edit(status_msg, "📤 **Uploading gallery…**")
            await client.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)

            # Telegram media groups support up to 10 items; chunk if needed
            files = result.gallery_files or []
            for i in range(0, len(files), 10):
                chunk = files[i : i + 10]
                media_group = []
                from pyrogram.types import InputMediaPhoto, InputMediaVideo
                for fp in chunk:
                    ext = fp.lower().rsplit(".", 1)[-1]
                    if ext in ("mp4", "mov", "webm"):
                        media_group.append(InputMediaVideo(fp))
                    else:
                        media_group.append(InputMediaPhoto(fp))
                await client.send_media_group(
                    chat_id=message.chat.id,
                    media=media_group,
                )

        await status_msg.delete()

    # ── Graceful error handling ────────────────────────────────────────────
    except UnsupportedURLError:
        await _safe_edit(
            status_msg,
            "❌ **Unsupported URL**\n"
            "This platform or link type isn't supported yet.",
        )

    except FileTooLargeError as e:
        await _safe_edit(
            status_msg,
            f"❌ **File Too Large**\n"
            f"The media is `{e.size_mb:.0f} MB`, which exceeds Telegram's **2 GB** limit.",
        )

    except PrivateMediaError:
        await _safe_edit(
            status_msg,
            "🔒 **Private / Age-Restricted Content**\n"
            "This media is not publicly accessible.",
        )

    except DownloadError as e:
        log.error("DownloadError for %s: %s", url, e)
        await _safe_edit(
            status_msg,
            f"⚠️ **Download Failed**\n`{e}`",
        )

    except Exception as e:
        log.exception("Unexpected error for URL %s", url)
        await _safe_edit(
            status_msg,
            f"💥 **Unexpected Error**\n`{type(e).__name__}: {e}`",
        )

    finally:
        if result and result.temp_dir:
            await asyncio.to_thread(cleanup_dir, result.temp_dir)


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting Ultimate Media Saver Bot…")
    app.run()
