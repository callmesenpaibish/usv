"""
Ultimate Media Saver Bot — functions.py  (v3 — definitive)

Key fixes:
  • Catches "No video formats found" / "There is no video in this post" /
    "No video could be found" errors and routes to image fallback
  • Pinterest  → og:image scrape
  • Instagram  → direct image URL from info dict
  • Twitter/X  → direct image URL from info dict
  • TikTok     → browser User-Agent fix
  • Reddit     → gallery-dl first, yt-dlp video fallback
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
from bs4 import BeautifulSoup

log = logging.getLogger("MediaSaverBot.functions")

# ─── Constants ────────────────────────────────────────────────────────────────
TEMP_ROOT   = Path(os.getenv("TEMP_DIR", "/tmp/media_saver"))
MAX_BYTES   = 2 * 1024 ** 3
COOKIE_FILE = os.getenv("YTDLP_COOKIES", "")

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

# These substrings in a yt-dlp error mean "no video, only image"
NO_VIDEO_PHRASES = (
    "no video formats found",
    "there is no video in this post",
    "no video could be found",
    "this tweet does not have a video",
    "no media could be extracted",
)

IS_TIKTOK    = re.compile(r"tiktok\.com", re.I)
IS_PINTEREST = re.compile(r"pinterest\.(com|co\.\w+)", re.I)
IS_INSTAGRAM = re.compile(r"instagram\.com", re.I)
IS_TWITTER   = re.compile(r"(twitter\.com|x\.com)", re.I)
IS_REDDIT    = re.compile(r"(reddit\.com|redd\.it)", re.I)
IS_IMAGE_URL = re.compile(r"\.(jpg|jpeg|png|gif|webp)(\?.*)?$", re.I)


# ─── Data structures ──────────────────────────────────────────────────────────
class MediaType(Enum):
    VIDEO   = "video"
    AUDIO   = "audio"
    IMAGE   = "image"
    GALLERY = "gallery"


@dataclass
class MediaResult:
    media_type:     MediaType
    file_path:      str | None  = None
    thumbnail_path: str | None  = None
    gallery_files:  list[str]   = field(default_factory=list)
    caption:        str | None  = None
    duration:       int | None  = None
    width:          int | None  = None
    height:         int | None  = None
    temp_dir:       str | None  = None


# ─── Exceptions ───────────────────────────────────────────────────────────────
class DownloadError(Exception):
    pass

class UnsupportedURLError(DownloadError):
    pass

class FileTooLargeError(DownloadError):
    def __init__(self, size_bytes: int):
        self.size_mb = size_bytes / 1_048_576
        super().__init__(f"File size {self.size_mb:.0f} MB exceeds 2 GB limit")

class PrivateMediaError(DownloadError):
    pass

class NoVideoError(DownloadError):
    """yt-dlp confirmed there is no video — only images — in this post."""
    pass


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _make_temp_dir() -> Path:
    path = TEMP_ROOT / str(uuid.uuid4())
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_dir(directory: str | Path) -> None:
    try:
        shutil.rmtree(str(directory), ignore_errors=True)
    except Exception as exc:
        log.warning("cleanup_dir failed: %s", exc)


def _find_largest_video(directory: Path) -> Path | None:
    candidates = (
        list(directory.glob("**/*.mp4"))
        + list(directory.glob("**/*.webm"))
        + list(directory.glob("**/*.mkv"))
        + list(directory.glob("**/*.mov"))
    )
    return max(candidates, key=lambda p: p.stat().st_size) if candidates else None


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
    if path.stat().st_size > MAX_BYTES:
        raise FileTooLargeError(path.stat().st_size)


def _build_caption(info: dict) -> str:
    title    = info.get("title") or ""
    uploader = info.get("uploader") or info.get("channel") or ""
    parts    = [p for p in (title, f"— {uploader}" if uploader else "") if p]
    return " ".join(parts)[:1024]


def _is_no_video_error(msg: str) -> bool:
    m = msg.lower()
    return any(phrase in m for phrase in NO_VIDEO_PHRASES)


# ─── yt-dlp core ─────────────────────────────────────────────────────────────
def _base_ydl_opts(extra: dict | None = None) -> dict:
    opts: dict = {
        "quiet":            True,
        "no_warnings":      True,
        "noprogress":       True,
        "socket_timeout":   30,
        "retries":          3,
        "fragment_retries": 3,
        "http_headers": {
            "User-Agent":      BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if COOKIE_FILE and Path(COOKIE_FILE).exists():
        opts["cookiefile"] = COOKIE_FILE
    if extra:
        opts.update(extra)
    return opts


def _ytdlp_extract_info_only(url: str) -> dict:
    opts = _base_ydl_opts({"noplaylist": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            return ydl.extract_info(url, download=False) or {}
        except yt_dlp.utils.DownloadError as exc:
            _raise_typed(exc)


def _ytdlp_download_video(
    url: str, out_dir: Path, progress_hook: Callable | None
) -> dict:
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
    msg = str(exc).lower()
    if any(k in msg for k in ("private", "login", "age", "unavailable")):
        raise PrivateMediaError(exc) from exc
    if "unsupported url" in msg:
        raise UnsupportedURLError(exc) from exc
    if _is_no_video_error(msg):
        raise NoVideoError(exc) from exc
    raise DownloadError(exc) from exc


# ─── HTTP helpers ─────────────────────────────────────────────────────────────
async def _http_download(url: str, dest: Path) -> Path:
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=40
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)
    return dest


async def _scrape_og_image(page_url: str, out_dir: Path) -> Path | None:
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    try:
        async with httpx.AsyncClient(
            headers=headers, follow_redirects=True, timeout=30
        ) as client:
            r = await client.get(page_url)
            r.raise_for_status()
            html = r.text
    except Exception as exc:
        log.warning("og:image page fetch failed: %s", exc)
        return None

    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for prop in ("og:image", "og:image:secure_url", "twitter:image"):
        tag = soup.find("meta", property=prop) or soup.find(
            "meta", attrs={"name": prop}
        )
        if tag and tag.get("content"):
            candidates.append(tag["content"])

    for img_url in candidates:
        if not img_url.startswith("http"):
            continue
        try:
            ext  = img_url.split("?")[0].rsplit(".", 1)[-1].lower() or "jpg"
            dest = out_dir / f"og_image.{ext}"
            await _http_download(img_url, dest)
            if dest.exists() and dest.stat().st_size > 1024:
                return dest
        except Exception as exc:
            log.warning("og:image download failed: %s", exc)

    return None


async def _image_from_info_dict(info: dict, out_dir: Path) -> MediaResult | None:
    """Extract a direct image URL from a yt-dlp info dict and download it."""
    image_url: str | None = None

    for fmt in (info.get("formats") or []):
        ext = (fmt.get("ext") or "").lower()
        if ext in ("jpg", "jpeg", "png", "webp", "gif"):
            image_url = fmt.get("url")
            break

    if not image_url:
        url_field = info.get("url") or ""
        if any(ext in url_field.lower() for ext in (".jpg", ".jpeg", ".png", ".webp")):
            image_url = url_field

    if not image_url:
        image_url = info.get("thumbnail")

    if not image_url:
        return None

    ext  = image_url.split("?")[0].rsplit(".", 1)[-1].lower() or "jpg"
    dest = out_dir / f"image.{ext}"
    try:
        await _http_download(image_url, dest)
        if dest.exists() and dest.stat().st_size > 512:
            _guard_size(dest)
            return MediaResult(
                media_type=MediaType.IMAGE,
                file_path=str(dest),
                caption=_build_caption(info),
                temp_dir=str(out_dir),
            )
    except Exception as exc:
        log.warning("image_from_info_dict failed: %s", exc)

    return None


# ─── gallery-dl ───────────────────────────────────────────────────────────────
def _gallery_dl_download(url: str, out_dir: Path) -> list[Path]:
    cmd = ["gallery-dl", "--dest", str(out_dir), "--no-mtime", url]
    try:
        subprocess.run(
            cmd, timeout=120, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        log.warning("gallery-dl not installed")
        return []
    except subprocess.CalledProcessError as exc:
        raise DownloadError(f"gallery-dl failed: {exc}") from exc

    files = sorted(out_dir.rglob("*"), key=lambda p: p.stat().st_mtime)
    return [f for f in files if f.is_file()]


def _classify_files(files: list[Path], out_dir: Path) -> MediaResult | None:
    VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov"}
    IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

    for f in files:
        _guard_size(f)

    videos = [f for f in files if f.suffix.lower() in VIDEO_EXT]
    images = [f for f in files if f.suffix.lower() in IMAGE_EXT]

    if len(videos) == 1 and not images:
        vp = videos[0]
        w, h = _get_video_dimensions(vp)
        return MediaResult(
            media_type=MediaType.VIDEO,
            file_path=str(vp),
            thumbnail_path=_extract_thumbnail(vp, out_dir),
            width=w, height=h,
            temp_dir=str(out_dir),
        )
    if len(images) == 1 and not videos:
        return MediaResult(
            media_type=MediaType.IMAGE,
            file_path=str(images[0]),
            temp_dir=str(out_dir),
        )
    if images or len(videos) > 1:
        return MediaResult(
            media_type=MediaType.GALLERY,
            gallery_files=[str(f) for f in images + videos],
            temp_dir=str(out_dir),
        )
    return None


# ─── Build video result ───────────────────────────────────────────────────────
def _build_video_result(info: dict, out_dir: Path) -> MediaResult:
    video_file = _find_largest_video(out_dir)

    if video_file is None:
        audio_files = (
            list(out_dir.glob("**/*.mp3")) + list(out_dir.glob("**/*.m4a"))
            + list(out_dir.glob("**/*.ogg")) + list(out_dir.glob("**/*.opus"))
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
            raise DownloadError("yt-dlp finished but no output file found.")
        video_file = max(all_files, key=lambda p: p.stat().st_size)

    _guard_size(video_file)
    w, h = _get_video_dimensions(video_file)
    return MediaResult(
        media_type=MediaType.VIDEO,
        file_path=str(video_file),
        thumbnail_path=_extract_thumbnail(video_file, out_dir),
        caption=_build_caption(info),
        duration=int(info.get("duration") or 0),
        width=w, height=h,
        temp_dir=str(out_dir),
    )


# ─── Platform handlers ────────────────────────────────────────────────────────

async def _handle_pinterest(url: str, out_dir: Path, hook: Callable | None) -> MediaResult:
    try:
        info = await asyncio.to_thread(_ytdlp_download_video, url, out_dir, hook)
        return _build_video_result(info, out_dir)
    except NoVideoError:
        pass
    except DownloadError:
        pass

    try:
        info = await asyncio.to_thread(_ytdlp_extract_info_only, url)
        result = await _image_from_info_dict(info, out_dir)
        if result:
            return result
    except Exception:
        pass

    img_path = await _scrape_og_image(url, out_dir)
    if img_path:
        _guard_size(img_path)
        return MediaResult(
            media_type=MediaType.IMAGE,
            file_path=str(img_path),
            temp_dir=str(out_dir),
        )

    raise DownloadError("Pinterest: could not extract image or video from this pin.")


async def _handle_instagram(url: str, out_dir: Path, hook: Callable | None) -> MediaResult:
    try:
        info = await asyncio.to_thread(_ytdlp_download_video, url, out_dir, hook)
        return _build_video_result(info, out_dir)
    except NoVideoError as exc:
        no_video_exc = exc

    try:
        info = await asyncio.to_thread(_ytdlp_extract_info_only, url)
    except Exception:
        info = {}

    entries = info.get("entries") or []
    if entries:
        downloaded: list[str] = []
        for i, entry in enumerate(entries):
            if not entry:
                continue
            item_dir = out_dir / f"item_{i:03d}"
            item_dir.mkdir(exist_ok=True)
            try:
                result = await _image_from_info_dict(entry, item_dir)
                if result and result.file_path:
                    downloaded.append(result.file_path)
            except Exception as exc:
                log.warning("Instagram gallery entry %d failed: %s", i, exc)
        if downloaded:
            if len(downloaded) == 1:
                return MediaResult(
                    media_type=MediaType.IMAGE,
                    file_path=downloaded[0],
                    temp_dir=str(out_dir),
                )
            return MediaResult(
                media_type=MediaType.GALLERY,
                gallery_files=downloaded,
                temp_dir=str(out_dir),
            )

    if info:
        result = await _image_from_info_dict(info, out_dir)
        if result:
            return result

    img_path = await _scrape_og_image(url, out_dir)
    if img_path:
        _guard_size(img_path)
        return MediaResult(
            media_type=MediaType.IMAGE,
            file_path=str(img_path),
            temp_dir=str(out_dir),
        )

    raise DownloadError("Instagram: could not extract media from this post.") from no_video_exc


async def _handle_twitter(url: str, out_dir: Path, hook: Callable | None) -> MediaResult:
    try:
        info = await asyncio.to_thread(_ytdlp_download_video, url, out_dir, hook)
        return _build_video_result(info, out_dir)
    except NoVideoError as exc:
        no_video_exc = exc

    try:
        info = await asyncio.to_thread(_ytdlp_extract_info_only, url)
    except Exception:
        info = {}

    entries = info.get("entries") or []
    if entries:
        downloaded: list[str] = []
        for i, entry in enumerate(entries):
            if not entry:
                continue
            item_dir = out_dir / f"item_{i:03d}"
            item_dir.mkdir(exist_ok=True)
            result = await _image_from_info_dict(entry, item_dir)
            if result and result.file_path:
                downloaded.append(result.file_path)
        if downloaded:
            if len(downloaded) == 1:
                return MediaResult(
                    media_type=MediaType.IMAGE,
                    file_path=downloaded[0],
                    temp_dir=str(out_dir),
                )
            return MediaResult(
                media_type=MediaType.GALLERY,
                gallery_files=downloaded,
                temp_dir=str(out_dir),
            )

    if info:
        result = await _image_from_info_dict(info, out_dir)
        if result:
            return result

    img_path = await _scrape_og_image(url, out_dir)
    if img_path:
        _guard_size(img_path)
        return MediaResult(
            media_type=MediaType.IMAGE,
            file_path=str(img_path),
            temp_dir=str(out_dir),
        )

    raise DownloadError("Twitter/X: could not extract media from this tweet.") from no_video_exc


async def _handle_reddit(url: str, out_dir: Path, hook: Callable | None) -> MediaResult:
    files = await asyncio.to_thread(_gallery_dl_download, url, out_dir)
    if files:
        result = _classify_files(files, out_dir)
        if result:
            return result

    info = await asyncio.to_thread(_ytdlp_download_video, url, out_dir, hook)
    return _build_video_result(info, out_dir)


async def _handle_tiktok(url: str, out_dir: Path, hook: Callable | None) -> MediaResult:
    info = await asyncio.to_thread(_ytdlp_download_video, url, out_dir, hook)
    return _build_video_result(info, out_dir)


async def _handle_generic(url: str, out_dir: Path, hook: Callable | None) -> MediaResult:
    info = await asyncio.to_thread(_ytdlp_download_video, url, out_dir, hook)
    return _build_video_result(info, out_dir)


# ─── Public entry point ───────────────────────────────────────────────────────
async def download_media(
    url: str,
    progress_hook: Callable | None = None,
) -> MediaResult:
    temp_dir = _make_temp_dir()
    try:
        if IS_IMAGE_URL.search(url.split("?")[0]):
            ext  = url.split("?")[0].rsplit(".", 1)[-1].lower() or "jpg"
            dest = temp_dir / f"image.{ext}"
            await _http_download(url, dest)
            _guard_size(dest)
            return MediaResult(
                media_type=MediaType.IMAGE,
                file_path=str(dest),
                temp_dir=str(temp_dir),
            )

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
        log.exception("Unexpected error: %s", url)
        cleanup_dir(temp_dir)
        raise DownloadError(str(exc)) from exc
