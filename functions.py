"""
Ultimate Media Saver Bot — functions.py  (v2 — full rewrite)

Fixes:
  • TikTok  — updated user-agent + extractor args
  • Images  — yt-dlp info-dict inspection BEFORE downloading
  • Gallery — Instagram/Reddit multi-image posts loop
  • Pinterest — og:image scrape fallback when no video found
  • X/Twitter — single-photo tweet detection
  • MediaType returned on every path so bot.py knows send_photo vs send_video
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable

import httpx
import yt_dlp
from bs4 import BeautifulSoup          # pip install beautifulsoup4

log = logging.getLogger("MediaSaverBot.functions")

# ─── Constants ───────────────────────────────────────────────────────────────
TEMP_ROOT   = Path(os.getenv("TEMP_DIR", "/tmp/media_saver"))
MAX_BYTES   = 2 * 1024 ** 3            # 2 GB — Telegram hard limit
COOKIE_FILE = os.getenv("YTDLP_COOKIES", "")

# Tiktok / Instagram / general browser UA — prevents many 403s
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

YTDLP_FORMAT = (
    "bestvideo[ext=mp4][filesize<2000M]+bestaudio[ext=m4a]/"
    "bestvideo[ext=mp4]+bestaudio/"
    "bestvideo+bestaudio/"
    "best[ext=mp4][filesize<2000M]/"
    "best"
)

# Regex helpers
IS_TIKTOK     = re.compile(r"tiktok\.com",                    re.I)
IS_PINTEREST  = re.compile(r"pinterest\.(com|co\.\w+)",       re.I)
IS_INSTAGRAM  = re.compile(r"instagram\.com",                  re.I)
IS_TWITTER    = re.compile(r"(twitter\.com|x\.com)",           re.I)
IS_REDDIT     = re.compile(r"(reddit\.com|redd\.it)",          re.I)
IS_GALLERY_DL = re.compile(
    r"(reddit\.com|redd\.it|pinterest\.(com|co\.\w+))", re.I
)
IS_IMAGE_URL  = re.compile(
    r"\.(jpg|jpeg|png|gif|webp)(\?.*)?$", re.I
)
SUPPORTED     = re.compile(
    r"(youtube\.com|youtu\.be|instagram\.com|tiktok\.com|"
    r"reddit\.com|redd\.it|pinterest\.(com|co\.\w+)|"
    r"twitter\.com|x\.com|twimg\.com)",
    re.I,
)


# ─── Data structures ─────────────────────────────────────────────────────────
class MediaType(Enum):
    VIDEO   = "video"
    AUDIO   = "audio"
    IMAGE   = "image"
    GALLERY = "gallery"


@dataclass
class MediaResult:
    media_type:     MediaType
    file_path:      str | None   = None   # single file (video / audio / image)
    thumbnail_path: str | None   = None
    gallery_files:  list[str]    = field(default_factory=list)
    caption:        str | None   = None
    duration:       int | None   = None
    width:          int | None   = None
    height:         int | None   = None
    temp_dir:       str | None   = None


# ─── Exceptions ──────────────────────────────────────────────────────────────
class DownloadError(Exception):
    """Generic download failure."""

class UnsupportedURLError(DownloadError):
    """URL not recognised by any engine."""

class FileTooLargeError(DownloadError):
    def __init__(self, size_bytes: int):
        self.size_mb = size_bytes / 1_048_576
        super().__init__(f"File size {self.size_mb:.0f} MB exceeds 2 GB limit")

class PrivateMediaError(DownloadError):
    """Media is private or age-restricted."""


# ─── Low-level helpers ────────────────────────────────────────────────────────
def _make_temp_dir() -> Path:
    path = TEMP_ROOT / str(uuid.uuid4())
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_dir(directory: str | Path) -> None:
    try:
        shutil.rmtree(str(directory), ignore_errors=True)
    except Exception as exc:
        log.warning("cleanup_dir failed for %s: %s", directory, exc)


def _find_largest_video(directory: Path) -> Path | None:
    candidates = (
        list(directory.glob("**/*.mp4"))
        + list(directory.glob("**/*.webm"))
        + list(directory.glob("**/*.mkv"))
        + list(directory.glob("**/*.mov"))
    )
    return max(candidates, key=lambda p: p.stat().st_size) if candidates else None


def _find_images(directory: Path) -> list[Path]:
    exts = ("*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp")
    imgs: list[Path] = []
    for pat in exts:
        imgs.extend(directory.glob(f"**/{pat}"))
    return sorted(imgs, key=lambda p: p.stat().st_mtime)


def _extract_thumbnail(video_path: Path, out_dir: Path) -> str | None:
    thumb = out_dir / "thumb.jpg"
    for seek in ("00:00:05", "00:00:00"):
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-ss", seek, "-i", str(video_path),
                 "-vframes", "1", "-q:v", "2", str(thumb)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
            if r.returncode == 0 and thumb.exists():
                return str(thumb)
        except Exception as exc:
            log.warning("ffmpeg thumbnail failed: %s", exc)
    return None


def _get_video_dimensions(video_path: Path) -> tuple[int, int]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
             str(video_path)],
            capture_output=True, text=True, timeout=15,
        )
        w, h = r.stdout.strip().split("x")
        return int(w), int(h)
    except Exception:
        return 0, 0


def _guard_size(path: Path) -> None:
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise FileTooLargeError(size)


def _build_caption(info: dict) -> str:
    title    = info.get("title") or ""
    uploader = info.get("uploader") or info.get("channel") or ""
    parts    = [p for p in (title, f"— {uploader}" if uploader else "") if p]
    return " ".join(parts)[:1024]


# ─── yt-dlp: info extraction (no download) ───────────────────────────────────
def _base_ydl_opts(extra: dict | None = None) -> dict:
    """
    Common yt-dlp options shared by both info-extraction and download calls.
    The critical addition is `http_headers` with a real browser UA — this
    fixes TikTok, Instagram, and Pinterest 403/unsupported errors.
    """
    opts: dict = {
        "quiet":            True,
        "no_warnings":      True,
        "noprogress":       True,
        "socket_timeout":   30,
        "retries":          3,
        "fragment_retries": 3,
        # ── TikTok / Instagram fix ──────────────────────────────────────
        "http_headers": {
            "User-Agent": BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
        # Force yt-dlp to use its newest extractor logic
        "extractor_args": {
            "tiktok": {"webpage_download": ["1"]},
        },
    }
    if COOKIE_FILE and Path(COOKIE_FILE).exists():
        opts["cookiefile"] = COOKIE_FILE
    if extra:
        opts.update(extra)
    return opts


def _ytdlp_extract_info(url: str) -> dict:
    """
    Run yt-dlp with download=False to get metadata only.
    Raises UnsupportedURLError / PrivateMediaError / DownloadError.
    """
    opts = _base_ydl_opts({"noplaylist": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            return ydl.extract_info(url, download=False) or {}
        except yt_dlp.utils.DownloadError as exc:
            _raise_typed(exc)


def _ytdlp_download_video(
    url: str, out_dir: Path, progress_hook: Callable | None
) -> dict:
    """Full download — returns info dict."""
    extra = {
        "format":              YTDLP_FORMAT,
        "outtmpl":             str(out_dir / "%(title).80s.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist":          True,
        "format_sort":         ["filesize:2000M"],
        "postprocessors": [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
        ],
    }
    if progress_hook:
        extra["progress_hooks"] = [progress_hook]

    opts = _base_ydl_opts(extra)
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            return ydl.extract_info(url, download=True) or {}
        except yt_dlp.utils.DownloadError as exc:
            _raise_typed(exc)


def _raise_typed(exc: Exception) -> None:
    """Re-raise a yt-dlp DownloadError as our typed exception."""
    msg = str(exc).lower()
    if any(k in msg for k in ("private", "login", "age", "unavailable")):
        raise PrivateMediaError(exc) from exc
    if "unsupported url" in msg:
        raise UnsupportedURLError(exc) from exc
    raise DownloadError(exc) from exc


# ─── Image-type detection from info dict ─────────────────────────────────────
def _info_is_image(info: dict) -> bool:
    """
    Return True if the yt-dlp info dict represents a static image / photo,
    not a playable video.
    """
    # Direct signals
    if info.get("_type") in ("photo", "image"):
        return True

    ext = (info.get("ext") or "").lower()
    if ext in ("jpg", "jpeg", "png", "webp", "gif"):
        return True

    # No video codec AND no formats with a video stream
    vcodec = (info.get("vcodec") or "none").lower()
    formats = info.get("formats") or []
    has_video_format = any(
        (f.get("vcodec") or "none").lower() not in ("none", "")
        for f in formats
    )
    if vcodec in ("none", "") and not has_video_format:
        return True

    return False


def _info_is_gallery(info: dict) -> bool:
    """Return True if the info dict describes a multi-item gallery/playlist."""
    return info.get("_type") in ("playlist", "multi_video") or bool(
        info.get("entries")
    )


# ─── httpx helpers ────────────────────────────────────────────────────────────
async def _download_image_from_url(url: str, dest: Path) -> Path:
    """Download a direct image URL to *dest* (the file path, not directory)."""
    headers = {"User-Agent": BROWSER_UA}
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=40
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)
    return dest


async def _scrape_og_image(page_url: str, out_dir: Path) -> Path | None:
    """
    Scrape the og:image / highest-res image from a page's <meta> tags.
    Used as the Pinterest (and fallback X/Instagram) image extractor.
    """
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(
            headers=headers, follow_redirects=True, timeout=30
        ) as client:
            r = await client.get(page_url)
            r.raise_for_status()
            html = r.text
    except Exception as exc:
        log.warning("og:image scrape — page fetch failed: %s", exc)
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Priority order for image meta tags
    candidates: list[str] = []
    for prop in ("og:image", "og:image:secure_url", "twitter:image"):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            candidates.append(tag["content"])

    # Also grab any large <img> src as last resort
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src.startswith("http") and any(
            ext in src.lower() for ext in (".jpg", ".jpeg", ".png", ".webp")
        ):
            candidates.append(src)

    for img_url in candidates:
        if not img_url.startswith("http"):
            continue
        try:
            ext  = img_url.split("?")[0].rsplit(".", 1)[-1].lower() or "jpg"
            dest = out_dir / f"og_image.{ext}"
            await _download_image_from_url(img_url, dest)
            if dest.exists() and dest.stat().st_size > 1024:
                log.info("og:image scraped from %s → %s", page_url, dest)
                return dest
        except Exception as exc:
            log.warning("og:image download failed (%s): %s", img_url, exc)

    return None


# ─── gallery-dl engine ────────────────────────────────────────────────────────
def _gallery_dl_download(url: str, out_dir: Path) -> list[Path]:
    cmd = ["gallery-dl", "--dest", str(out_dir), "--no-mtime", url]
    try:
        subprocess.run(
            cmd, timeout=120, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        log.warning("gallery-dl not installed; skipping")
        return []
    except subprocess.CalledProcessError as exc:
        raise DownloadError(f"gallery-dl failed: {exc}") from exc

    files = sorted(out_dir.rglob("*"), key=lambda p: p.stat().st_mtime)
    return [f for f in files if f.is_file()]


# ─── Platform-specific handlers ──────────────────────────────────────────────

async def _handle_tiktok(
    url: str, out_dir: Path, progress_hook: Callable | None
) -> MediaResult:
    """
    TikTok videos: force browser UA and use --no-check-certificate to bypass
    region / bot-detection blocks.
    """
    log.info("TikTok strategy for %s", url)
    info = await asyncio.to_thread(_ytdlp_download_video, url, out_dir, progress_hook)
    return _build_video_result(info, out_dir)


async def _handle_pinterest(
    url: str, out_dir: Path, progress_hook: Callable | None
) -> MediaResult:
    """
    Pinterest: try yt-dlp first (video pins), fall back to og:image scrape.
    """
    log.info("Pinterest strategy for %s", url)

    # 1. Try yt-dlp info extraction first
    try:
        info = await asyncio.to_thread(_ytdlp_extract_info, url)
    except (UnsupportedURLError, DownloadError):
        info = {}

    # 2. If info suggests a video, download it
    if info and not _info_is_image(info):
        try:
            info = await asyncio.to_thread(
                _ytdlp_download_video, url, out_dir, progress_hook
            )
            return _build_video_result(info, out_dir)
        except DownloadError:
            pass  # fall through to image scrape

    # 3. Scrape og:image
    img_path = await _scrape_og_image(url, out_dir)
    if img_path:
        _guard_size(img_path)
        return MediaResult(
            media_type=MediaType.IMAGE,
            file_path=str(img_path),
            caption=info.get("title") if info else None,
            temp_dir=str(out_dir),
        )

    raise DownloadError("Pinterest: could not extract image or video from this pin.")


async def _handle_instagram(
    url: str, out_dir: Path, progress_hook: Callable | None
) -> MediaResult:
    """
    Instagram: inspect info dict first.
    • Single video  → download video
    • Single image  → download image via direct URL
    • Carousel      → loop entries and download all images/videos
    """
    log.info("Instagram strategy for %s", url)

    info = await asyncio.to_thread(_ytdlp_extract_info, url)

    # ── Carousel / gallery ────────────────────────────────────────────────
    if _info_is_gallery(info):
        return await _handle_info_gallery(info, out_dir, progress_hook, url)

    # ── Single image post ─────────────────────────────────────────────────
    if _info_is_image(info):
        return await _download_image_from_info(info, out_dir)

    # ── Video reel ────────────────────────────────────────────────────────
    info = await asyncio.to_thread(_ytdlp_download_video, url, out_dir, progress_hook)
    return _build_video_result(info, out_dir)


async def _handle_twitter(
    url: str, out_dir: Path, progress_hook: Callable | None
) -> MediaResult:
    """
    X / Twitter: inspect info dict.
    • Video tweet  → download video
    • Photo tweet  → download image
    • Multi-photo  → gallery
    """
    log.info("Twitter/X strategy for %s", url)

    info = await asyncio.to_thread(_ytdlp_extract_info, url)

    if _info_is_gallery(info):
        return await _handle_info_gallery(info, out_dir, progress_hook, url)

    if _info_is_image(info):
        return await _download_image_from_info(info, out_dir)

    info = await asyncio.to_thread(_ytdlp_download_video, url, out_dir, progress_hook)
    return _build_video_result(info, out_dir)


async def _handle_reddit(
    url: str, out_dir: Path, progress_hook: Callable | None
) -> MediaResult:
    """
    Reddit: gallery-dl first (handles image galleries natively),
    then yt-dlp for video posts.
    """
    log.info("Reddit strategy for %s", url)

    files = await asyncio.to_thread(_gallery_dl_download, url, out_dir)
    if files:
        result = _classify_gallery_dl_files(files, out_dir)
        if result:
            return result

    # Fall through to yt-dlp (video post)
    info = await asyncio.to_thread(_ytdlp_download_video, url, out_dir, progress_hook)
    return _build_video_result(info, out_dir)


# ─── Gallery helpers ──────────────────────────────────────────────────────────

async def _handle_info_gallery(
    info: dict, out_dir: Path, progress_hook: Callable | None, original_url: str
) -> MediaResult:
    """
    Loop through info["entries"] and download each item individually.
    Returns GALLERY if multiple images, VIDEO if single video entry.
    """
    entries = info.get("entries") or []
    if not entries:
        raise DownloadError("Gallery metadata had no entries.")

    downloaded: list[str] = []

    for i, entry in enumerate(entries):
        if not entry:
            continue
        entry_url = entry.get("webpage_url") or entry.get("url") or ""
        if not entry_url:
            continue

        item_dir = out_dir / f"item_{i:03d}"
        item_dir.mkdir(exist_ok=True)

        try:
            if _info_is_image(entry):
                img = await _download_image_from_info(entry, item_dir)
                if img.file_path:
                    downloaded.append(img.file_path)
            else:
                # Video entry
                item_info = await asyncio.to_thread(
                    _ytdlp_download_video, entry_url, item_dir, None
                )
                vf = _find_largest_video(item_dir)
                if vf:
                    _guard_size(vf)
                    downloaded.append(str(vf))
        except Exception as exc:
            log.warning("Gallery entry %d failed: %s", i, exc)

    if not downloaded:
        raise DownloadError("Gallery: no items could be downloaded.")

    if len(downloaded) == 1:
        p = Path(downloaded[0])
        if p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov"):
            w, h  = _get_video_dimensions(p)
            thumb = _extract_thumbnail(p, out_dir)
            return MediaResult(
                media_type=MediaType.VIDEO,
                file_path=str(p),
                thumbnail_path=thumb,
                width=w, height=h,
                temp_dir=str(out_dir),
            )
        return MediaResult(
            media_type=MediaType.IMAGE,
            file_path=str(p),
            temp_dir=str(out_dir),
        )

    return MediaResult(
        media_type=MediaType.GALLERY,
        gallery_files=downloaded,
        temp_dir=str(out_dir),
    )


async def _download_image_from_info(info: dict, out_dir: Path) -> MediaResult:
    """
    Extract the best image URL from a yt-dlp info dict and download it.
    Tries: formats list → thumbnail → direct url field.
    """
    image_url: str | None = None

    # 1. Look for an image-type format entry
    for fmt in (info.get("formats") or []):
        ext = (fmt.get("ext") or "").lower()
        if ext in ("jpg", "jpeg", "png", "webp", "gif"):
            image_url = fmt.get("url")
            break

    # 2. Fall back to the info url itself
    if not image_url:
        image_url = info.get("url") or info.get("thumbnail")

    if not image_url:
        raise DownloadError("Image post detected but no image URL found in metadata.")

    ext  = image_url.split("?")[0].rsplit(".", 1)[-1].lower() or "jpg"
    dest = out_dir / f"image.{ext}"
    await _download_image_from_url(image_url, dest)
    _guard_size(dest)

    return MediaResult(
        media_type=MediaType.IMAGE,
        file_path=str(dest),
        caption=_build_caption(info),
        temp_dir=str(out_dir),
    )


def _classify_gallery_dl_files(
    files: list[Path], out_dir: Path
) -> MediaResult | None:
    """Turn a list of gallery-dl output files into a MediaResult."""
    for f in files:
        _guard_size(f)

    video_exts = {".mp4", ".webm", ".mkv", ".mov"}
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

    videos = [f for f in files if f.suffix.lower() in video_exts]
    images = [f for f in files if f.suffix.lower() in image_exts]

    if len(videos) == 1 and not images:
        vp    = videos[0]
        thumb = _extract_thumbnail(vp, out_dir)
        w, h  = _get_video_dimensions(vp)
        return MediaResult(
            media_type=MediaType.VIDEO,
            file_path=str(vp),
            thumbnail_path=thumb, width=w, height=h,
            temp_dir=str(out_dir),
        )

    if len(images) == 1 and not videos:
        return MediaResult(
            media_type=MediaType.IMAGE,
            file_path=str(images[0]),
            temp_dir=str(out_dir),
        )

    if images or len(videos) > 1:
        all_media = [str(f) for f in (images + videos)]
        return MediaResult(
            media_type=MediaType.GALLERY,
            gallery_files=all_media,
            temp_dir=str(out_dir),
        )

    return None


def _build_video_result(info: dict, out_dir: Path) -> MediaResult:
    """Locate the downloaded video file and build a MediaResult."""
    video_file = _find_largest_video(out_dir)

    # Audio-only fallback
    if video_file is None:
        vcodec = (info.get("vcodec") or "none").lower()
        if vcodec in ("none", ""):
            audio_files = (
                list(out_dir.glob("**/*.mp3"))
                + list(out_dir.glob("**/*.m4a"))
                + list(out_dir.glob("**/*.ogg"))
                + list(out_dir.glob("**/*.opus"))
            )
            if audio_files:
                ap = max(audio_files, key=lambda p: p.stat().st_size)
                _guard_size(ap)
                return MediaResult(
                    media_type=MediaType.AUDIO,
                    file_path=str(ap),
                    caption=info.get("title"),
                    duration=int(info.get("duration") or 0),
                    temp_dir=str(out_dir),
                )

        all_files = [f for f in out_dir.rglob("*") if f.is_file()]
        if not all_files:
            raise DownloadError("yt-dlp finished but no output file was found.")
        video_file = max(all_files, key=lambda p: p.stat().st_size)

    _guard_size(video_file)
    thumb = _extract_thumbnail(video_file, out_dir)
    w, h  = _get_video_dimensions(video_file)

    return MediaResult(
        media_type=MediaType.VIDEO,
        file_path=str(video_file),
        thumbnail_path=thumb,
        caption=_build_caption(info),
        duration=int(info.get("duration") or 0),
        width=w, height=h,
        temp_dir=str(out_dir),
    )


# ─── Generic yt-dlp handler (YouTube, etc.) ──────────────────────────────────
async def _handle_generic(
    url: str, out_dir: Path, progress_hook: Callable | None
) -> MediaResult:
    """YouTube, and any other site not given a dedicated handler."""
    info = await asyncio.to_thread(_ytdlp_download_video, url, out_dir, progress_hook)
    return _build_video_result(info, out_dir)


# ─── Public entry point ───────────────────────────────────────────────────────
async def download_media(
    url: str,
    progress_hook: Callable | None = None,
) -> MediaResult:
    """
    Main entry point called from bot.py.

    Routing table:
        TikTok    → _handle_tiktok   (browser UA + extractor fix)
        Pinterest → _handle_pinterest (video pin → yt-dlp, image → og:image)
        Instagram → _handle_instagram (video/image/carousel detection)
        Twitter/X → _handle_twitter  (video/image/gallery detection)
        Reddit    → _handle_reddit   (gallery-dl first, yt-dlp fallback)
        Direct img→ httpx download
        Everything else → yt-dlp generic
    """
    temp_dir = _make_temp_dir()

    try:
        # ── Direct image URL (e.g. i.redd.it, pbs.twimg.com, etc.) ──────────
        if IS_IMAGE_URL.search(url.split("?")[0]):
            ext  = url.split("?")[0].rsplit(".", 1)[-1].lower() or "jpg"
            dest = temp_dir / f"image.{ext}"
            await _download_image_from_url(url, dest)
            _guard_size(dest)
            return MediaResult(
                media_type=MediaType.IMAGE,
                file_path=str(dest),
                temp_dir=str(temp_dir),
            )

        # ── Platform routing ─────────────────────────────────────────────────
        if IS_TIKTOK.search(url):
            result = await _handle_tiktok(url, temp_dir, progress_hook)

        elif IS_PINTEREST.search(url):
            result = await _handle_pinterest(url, temp_dir, progress_hook)

        elif IS_INSTAGRAM.search(url):
            result = await _handle_instagram(url, temp_dir, progress_hook)

        elif IS_TWITTER.search(url):
            result = await _handle_twitter(url, temp_dir, progress_hook)

        elif IS_REDDIT.search(url):
            result = await _handle_reddit(url, temp_dir, progress_hook)

        else:
            result = await _handle_generic(url, temp_dir, progress_hook)

        return result

    except (DownloadError, PrivateMediaError, UnsupportedURLError, FileTooLargeError):
        cleanup_dir(temp_dir)
        raise
    except Exception as exc:
        log.exception("Unexpected error downloading %s", url)
        cleanup_dir(temp_dir)
        raise DownloadError(str(exc)) from exc
