"""
Ultimate Media Saver Bot — functions.py
Download engine: yt-dlp (async), gallery-dl / httpx fallback, ffmpeg thumbnails.
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

log = logging.getLogger("MediaSaverBot.functions")

# ─── Constants ───────────────────────────────────────────────────────────────
TEMP_ROOT    = Path(os.getenv("TEMP_DIR", "/tmp/media_saver"))
MAX_BYTES    = 2 * 1024 ** 3           # 2 GB — Telegram hard limit
COOKIE_FILE  = os.getenv("YTDLP_COOKIES", "")  # optional cookies.txt path

YTDLP_FORMAT = (
    "bestvideo[ext=mp4][filesize<2000M]+bestaudio[ext=m4a]/"
    "bestvideo[ext=mp4]+bestaudio/"
    "bestvideo+bestaudio/"
    "best[ext=mp4][filesize<2000M]/"
    "best"
)

# Platforms that yt-dlp handles natively
SUPPORTED_PATTERNS = re.compile(
    r"(youtube\.com|youtu\.be|instagram\.com|tiktok\.com|"
    r"reddit\.com|redd\.it|pinterest\.(com|co\.\w+)|"
    r"twitter\.com|x\.com|twimg\.com)",
    re.IGNORECASE,
)

# Platforms where gallery-dl is the better engine
GALLERY_DL_PATTERNS = re.compile(
    r"(reddit\.com|redd\.it|pinterest\.(com|co\.\w+))",
    re.IGNORECASE,
)


# ─── Data structures ─────────────────────────────────────────────────────────
class MediaType(Enum):
    VIDEO   = auto()
    AUDIO   = auto()
    IMAGE   = auto()
    GALLERY = auto()


@dataclass
class MediaResult:
    media_type:      MediaType
    file_path:       str | None        = None
    thumbnail_path:  str | None        = None
    gallery_files:   list[str]         = field(default_factory=list)
    caption:         str | None        = None
    duration:        int | None        = None
    width:           int | None        = None
    height:          int | None        = None
    temp_dir:        str | None        = None


# ─── Custom exceptions ────────────────────────────────────────────────────────
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


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _make_temp_dir() -> Path:
    """Create a unique temporary directory for one download session."""
    path = TEMP_ROOT / str(uuid.uuid4())
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_dir(directory: str | Path) -> None:
    """Recursively remove a temp directory."""
    try:
        shutil.rmtree(str(directory), ignore_errors=True)
    except Exception as exc:
        log.warning("cleanup_dir failed for %s: %s", directory, exc)


def _find_largest_video(directory: Path) -> Path | None:
    """Return the largest .mp4 / .webm / .mkv file in *directory*."""
    candidates = list(directory.glob("**/*.mp4")) + \
                 list(directory.glob("**/*.webm")) + \
                 list(directory.glob("**/*.mkv"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _extract_thumbnail(video_path: Path, out_dir: Path) -> str | None:
    """Use ffmpeg to extract a JPEG thumbnail at 5 s (or 0 s if shorter)."""
    thumb = out_dir / "thumb.jpg"
    try:
        for seek in ("00:00:05", "00:00:00"):
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", seek,
                    "-i", str(video_path),
                    "-vframes", "1",
                    "-q:v", "2",
                    str(thumb),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            if result.returncode == 0 and thumb.exists():
                return str(thumb)
    except Exception as exc:
        log.warning("ffmpeg thumbnail extraction failed: %s", exc)
    return None


def _get_video_dimensions(video_path: Path) -> tuple[int, int]:
    """Return (width, height) using ffprobe, or (0, 0) on failure."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        w, h = r.stdout.strip().split("x")
        return int(w), int(h)
    except Exception:
        return 0, 0


# ─── yt-dlp engine ───────────────────────────────────────────────────────────
def _build_ydl_opts(out_dir: Path, progress_hook: Callable | None) -> dict:
    opts: dict = {
        "format":             YTDLP_FORMAT,
        "outtmpl":            str(out_dir / "%(title).80s.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist":         True,
        "quiet":              True,
        "no_warnings":        True,
        "noprogress":         True,
        "socket_timeout":     30,
        "retries":            3,
        "fragment_retries":   3,
        "postprocessors": [
            {
                "key":            "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
        # Respect file-size limit inside yt-dlp as first guard
        "format_sort": ["filesize:2000M"],
    }
    if COOKIE_FILE and Path(COOKIE_FILE).exists():
        opts["cookiefile"] = COOKIE_FILE
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts


def _ytdlp_download(url: str, out_dir: Path, progress_hook: Callable | None) -> dict:
    """Blocking yt-dlp download. Returns the info dict."""
    opts = _build_ydl_opts(out_dir, progress_hook)
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
            return info or {}
        except yt_dlp.utils.DownloadError as exc:
            msg = str(exc).lower()
            if any(k in msg for k in ("private", "login", "age", "unavailable")):
                raise PrivateMediaError(exc) from exc
            if "unsupported url" in msg:
                raise UnsupportedURLError(exc) from exc
            raise DownloadError(exc) from exc


# ─── gallery-dl engine ────────────────────────────────────────────────────────
def _gallery_dl_download(url: str, out_dir: Path) -> list[Path]:
    """
    Use gallery-dl (subprocess) to download image/video galleries.
    Returns list of downloaded file paths.
    """
    cmd = [
        "gallery-dl",
        "--dest", str(out_dir),
        "--no-mtime",
        url,
    ]
    try:
        subprocess.run(cmd, timeout=120, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        log.warning("gallery-dl not installed; falling back to httpx scrape")
        return []
    except subprocess.CalledProcessError as exc:
        raise DownloadError(f"gallery-dl failed: {exc}") from exc

    files = sorted(out_dir.rglob("*"), key=lambda p: p.stat().st_mtime)
    return [f for f in files if f.is_file()]


# ─── httpx image fallback ─────────────────────────────────────────────────────
async def _httpx_download_image(url: str, out_dir: Path) -> Path:
    """
    Simple direct image download via httpx for plain image URLs.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; MediaSaverBot/1.0)"
        )
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True,
                                  timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        ct   = r.headers.get("content-type", "image/jpeg")
        ext  = ct.split("/")[-1].split(";")[0].strip() or "jpg"
        path = out_dir / f"image.{ext}"
        path.write_bytes(r.content)
        return path


# ─── Public async API ─────────────────────────────────────────────────────────
async def download_media(
    url: str,
    progress_hook: Callable | None = None,
) -> MediaResult:
    """
    Entry point called from bot.py.
    Decides which engine to use, downloads the content, and returns MediaResult.
    """
    # Basic URL validation
    if not SUPPORTED_PATTERNS.search(url):
        # Still attempt yt-dlp — it supports 1000+ sites
        pass

    temp_dir = _make_temp_dir()

    try:
        # ── Strategy: gallery-dl first for Reddit / Pinterest ───────────────
        if GALLERY_DL_PATTERNS.search(url):
            result = await _handle_gallery(url, temp_dir, progress_hook)
            if result:
                return result
            # If gallery-dl returned nothing, fall through to yt-dlp

        # ── Strategy: direct image URL ───────────────────────────────────────
        if re.search(r"\.(jpg|jpeg|png|gif|webp)(\?.*)?$", url, re.IGNORECASE):
            img_path = await _httpx_download_image(url, temp_dir)
            _guard_size(img_path)
            return MediaResult(
                media_type=MediaType.IMAGE,
                file_path=str(img_path),
                temp_dir=str(temp_dir),
            )

        # ── Strategy: yt-dlp ─────────────────────────────────────────────────
        info = await asyncio.to_thread(
            _ytdlp_download, url, temp_dir, progress_hook
        )
        return _build_result_from_ytdlp(info, temp_dir)

    except (DownloadError, PrivateMediaError, UnsupportedURLError, FileTooLargeError):
        cleanup_dir(temp_dir)
        raise
    except Exception as exc:
        cleanup_dir(temp_dir)
        raise DownloadError(str(exc)) from exc


# ─── Internal result builders ─────────────────────────────────────────────────
async def _handle_gallery(
    url: str,
    temp_dir: Path,
    progress_hook: Callable | None,
) -> MediaResult | None:
    """Try gallery-dl; if it returns videos too, process the first as video."""
    files = await asyncio.to_thread(_gallery_dl_download, url, temp_dir)

    if not files:
        return None

    for f in files:
        _guard_size(f)

    video_exts = {".mp4", ".webm", ".mkv", ".mov"}
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

    videos = [f for f in files if f.suffix.lower() in video_exts]
    images = [f for f in files if f.suffix.lower() in image_exts]

    # Single video
    if len(videos) == 1 and not images:
        vp     = videos[0]
        thumb  = _extract_thumbnail(vp, temp_dir)
        w, h   = _get_video_dimensions(vp)
        return MediaResult(
            media_type=MediaType.VIDEO,
            file_path=str(vp),
            thumbnail_path=thumb,
            width=w,
            height=h,
            temp_dir=str(temp_dir),
        )

    # Gallery of images / mixed
    if images or (len(videos) > 1):
        all_media = [str(f) for f in (images + videos)]
        return MediaResult(
            media_type=MediaType.GALLERY,
            gallery_files=all_media,
            temp_dir=str(temp_dir),
        )

    return None


def _build_result_from_ytdlp(info: dict, temp_dir: Path) -> MediaResult:
    """Parse yt-dlp info dict and locate the downloaded file."""
    video_file = _find_largest_video(temp_dir)

    # Determine if the result is audio-only
    vcodec = (info.get("vcodec") or "").lower()
    is_audio_only = vcodec in ("none", "") and video_file is None

    # Audio-only: find any audio file
    if is_audio_only:
        audio_candidates = (
            list(temp_dir.glob("**/*.mp3")) +
            list(temp_dir.glob("**/*.m4a")) +
            list(temp_dir.glob("**/*.ogg")) +
            list(temp_dir.glob("**/*.opus"))
        )
        if audio_candidates:
            ap = max(audio_candidates, key=lambda p: p.stat().st_size)
            _guard_size(ap)
            return MediaResult(
                media_type=MediaType.AUDIO,
                file_path=str(ap),
                caption=info.get("title"),
                duration=int(info.get("duration") or 0),
                temp_dir=str(temp_dir),
            )
        raise DownloadError("No usable file found after yt-dlp download.")

    if video_file is None:
        # Try to find any file as a last resort
        all_files = [f for f in temp_dir.rglob("*") if f.is_file()]
        if not all_files:
            raise DownloadError("yt-dlp finished but no output file was found.")
        video_file = max(all_files, key=lambda p: p.stat().st_size)

    _guard_size(video_file)

    thumb  = _extract_thumbnail(video_file, temp_dir)
    w, h   = _get_video_dimensions(video_file)

    return MediaResult(
        media_type=MediaType.VIDEO,
        file_path=str(video_file),
        thumbnail_path=thumb,
        caption=_build_caption(info),
        duration=int(info.get("duration") or 0),
        width=w,
        height=h,
        temp_dir=str(temp_dir),
    )


def _build_caption(info: dict) -> str:
    title    = info.get("title", "")
    uploader = info.get("uploader") or info.get("channel") or ""
    parts    = [p for p in (title, f"— {uploader}" if uploader else "") if p]
    return " ".join(parts)[:1024]  # Telegram caption limit


def _guard_size(path: Path) -> None:
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise FileTooLargeError(size)
