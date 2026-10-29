"""
Shared helpers for the DJ track pipeline scripts (watcher.py, watch_input.py,
gui.py).

- load_config(): read config.json, with all folder/file paths resolved to
  absolute paths relative to the app's own directory
- convert_to_320_mp3(): true 320kbps CBR MP3 via ffmpeg (libmp3lame)
- tag_mp3(): write ID3 tags (title/artist/genre/year/cover art) from a
  yt-dlp-style info dict
- safe_filename(): strip characters that are illegal in Windows filenames
- find_running_instance()/acquire_lock()/release_lock(): a simple
  cross-entry-point single-instance lock shared by gui.py/watcher.py/
  watch_input.py
- load_settings()/save_settings(): small persisted GUI preferences
- move_to_trash(): send a file to the Recycle Bin (recoverable)
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TCON, TDRC, APIC
from send2trash import send2trash


def get_app_dir() -> Path:
    """Directory the running exe (or this script, when run unfrozen) lives
    in. Used to resolve config.json and its relative paths consistently --
    PyInstaller's __file__ points into a temp extraction folder once
    frozen, not the exe's real location, and the process's current working
    directory isn't reliable either (depends on how the exe was launched).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


APP_DIR = get_app_dir()
CONFIG_PATH = APP_DIR / "config.json"
LOCK_PATH = APP_DIR / ".dj_downlist.lock"
SETTINGS_PATH = APP_DIR / "gui_settings.json"

_PATH_KEYS = ("queue_file", "done_log", "temp_dir", "input_folder", "mp3_folder")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key in _PATH_KEYS:
        if key in cfg and not Path(cfg[key]).is_absolute():
            cfg[key] = str(APP_DIR / cfg[key])
    return cfg


def load_settings() -> dict:
    """Small persisted GUI preferences (e.g. the delete-after-merge toggle)
    -- kept separate from config.json so the app never rewrites the file
    the user hand-edits for folder paths.
    """
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_settings(settings: dict):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def move_to_trash(path: Path):
    """Send path to the Recycle Bin (recoverable) rather than deleting it
    outright."""
    send2trash(str(path))


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name or "").strip()


# Matches "free download"/"free dl" in pretty much any style uploaders use:
# (Free Download), [FREE DL], Free Download!, FreeDL, Free-Download, etc.
# -- case-insensitive, with or without brackets/separators/punctuation.
# Deliberately does NOT match a bare "free" on its own, since that could be
# part of a track's real title (e.g. "Free Bird") rather than promo text.
_FREE_DL_RE = re.compile(r'[\[\(\{]?\s*free[\s\-_]*d(?:ownload|l)s?\b\s*!*\s*[\]\)\}]?', re.IGNORECASE)


def strip_free_dl_tag(text: str) -> str:
    """Remove '(Free Download)'/'[FREE DL]'/etc. promo text from a title
    (in any bracket/case/spacing style), then tidy up whatever dangling
    punctuation it leaves behind -- e.g. "Song - Free Download" becomes
    "Song", not "Song -".
    """
    if not text:
        return text
    cleaned = _FREE_DL_RE.sub("", text)
    cleaned = re.sub(r'\(\s*\)|\[\s*\]|\{\s*\}', '', cleaned)  # now-empty brackets
    cleaned = re.sub(r'\s*[-|]\s*$', '', cleaned)  # dangling trailing separator
    cleaned = re.sub(r'^\s*[-|]\s*', '', cleaned)  # dangling leading separator
    cleaned = re.sub(r'\s{2,}', ' ', cleaned)  # collapse doubled-up spaces
    return cleaned.strip()


def convert_to_mp3(src_path: Path, dst_path: Path, bitrate_k: int = 320):
    """Convert any audio file to an MP3 via ffmpeg at a fixed CBR bitrate.

    Quality is only ever as good as src_path — this does not (and cannot)
    manufacture quality that isn't in the source file. Pass the source's
    own real bitrate here (rather than always 320) when the source is a
    lossy stream, so the output doesn't claim quality it doesn't have.
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(src_path),
        "-vn",
        "-codec:a", "libmp3lame",
        "-b:a", f"{bitrate_k}k",
        "-ar", "44100",
        str(dst_path),
    ]
    # Without this, Windows briefly flashes a console window for the child
    # ffmpeg.exe process even though our own app is windowed/console-less.
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.run(
        cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def convert_to_320_mp3(src_path: Path, dst_path: Path):
    """Convert any audio file to a true 320kbps CBR MP3 via ffmpeg."""
    convert_to_mp3(src_path, dst_path, 320)


def is_mp3(path: Path) -> bool:
    return path.suffix.lower() == ".mp3"


def ensure_mp3(src_path: Path, dst_path: Path, bitrate_k: int = 320):
    """Get src_path into dst_path as an MP3 at bitrate_k CBR.

    If src is already an MP3, it's copied as-is -- never re-encoded, since
    re-encoding an existing MP3 (at any bitrate) would only degrade it and
    gains nothing. Any other format is run through convert_to_mp3() at
    bitrate_k -- pass the source's own real bitrate to avoid upscaling a
    low-quality stream into a fake high-bitrate file.
    """
    if is_mp3(src_path):
        shutil.copy2(src_path, dst_path)
    else:
        convert_to_mp3(src_path, dst_path, bitrate_k)


def ensure_320_mp3(src_path: Path, dst_path: Path):
    """Get src_path into dst_path as a true 320kbps MP3 (see ensure_mp3)."""
    ensure_mp3(src_path, dst_path, 320)


def unique_dest_path(dest_dir: Path, filename: str) -> Path:
    """Return dest_dir/filename, or an incrementing 'name (2).ext' variant
    if that name is already taken -- mirrors Windows' own duplicate-file
    naming instead of an opaque ID suffix.
    """
    dst = dest_dir / filename
    if not dst.exists():
        return dst
    stem = Path(filename).stem
    ext = Path(filename).suffix
    n = 2
    while True:
        candidate = dest_dir / f"{stem} ({n}){ext}"
        if not candidate.exists():
            return candidate
        n += 1


def tag_mp3(mp3_path: Path, info: dict, thumbnail_path: Path | None = None):
    """Write ID3 tags to mp3_path from a yt-dlp-style info dict.

    info is expected to look like what yt_dlp.YoutubeDL.extract_info()
    returns (works whether or not audio was actually downloaded from it).
    """
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    title = strip_free_dl_tag(info.get("title")) or "Unknown Title"
    artist = info.get("uploader") or info.get("artist") or "Unknown Artist"
    genre = ", ".join(info.get("tags") or []) if info.get("tags") else None
    year = str(info.get("release_year") or (info.get("upload_date") or "")[:4] or "")

    tags.setall("TIT2", [TIT2(encoding=3, text=title)])
    tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
    if genre:
        tags.setall("TCON", [TCON(encoding=3, text=genre)])
    if year:
        tags.setall("TDRC", [TDRC(encoding=3, text=year)])

    if thumbnail_path and thumbnail_path.exists():
        with open(thumbnail_path, "rb") as img:
            tags.setall("APIC", [APIC(
                encoding=3, mime="image/jpeg", type=3, desc="cover", data=img.read()
            )])

    tags.save(mp3_path, v2_version=3)


# -- single-instance lock (shared by gui.py, watcher.py, watch_input.py) --
#
# A lightweight PID-in-a-file lock, checked via Win32 OpenProcess -- not
# bulletproof (a PID could in principle get reused by an unrelated process
# between crash and recheck), but good enough for a local single-user tool,
# and it directly prevents the kind of accidental double-processing that
# happens when e.g. the GUI and a leftover watch_input.py console are both
# watching the same folder at once.

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001


def _pid_is_running(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def find_running_instance() -> int | None:
    """Return the PID of an already-running instance of this app, or None.

    Cleans up the lock file if it's stale (points at a PID that's no
    longer alive, or is unreadable).
    """
    if not LOCK_PATH.exists():
        return None
    try:
        pid = int(LOCK_PATH.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        LOCK_PATH.unlink(missing_ok=True)
        return None
    if pid == os.getpid():
        return None
    if _pid_is_running(pid):
        return pid
    LOCK_PATH.unlink(missing_ok=True)
    return None


def acquire_lock():
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def release_lock():
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except OSError:
        pass


def terminate_process(pid: int):
    handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if handle:
        ctypes.windll.kernel32.TerminateProcess(handle, 0)
        ctypes.windll.kernel32.CloseHandle(handle)
