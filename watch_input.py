"""
DJ track pipeline -- local-file merge helper.

Used by gui.py's "Process File + Link" flow: given a track you've already
downloaded yourself (e.g. from a site that requires sign-in) plus its
matching URL, fetch metadata from that URL, get the file into a
true 320kbps CBR MP3 (or copy it as-is if it's already an MP3), tag it, and
return the destination path in mp3_folder.

No audio is ever downloaded -- the MP3's actual audio
quality comes entirely from your local file. A low-bitrate source stays
low-bitrate; this does not fake or upscale quality.

This module has no CLI entry point of its own -- run gui.py.
"""

import urllib.request
from pathlib import Path

import yt_dlp

import core


def fetch_metadata(url: str) -> dict:
    cookie_path = str(core.APP_DIR / "www.beatport.com_cookies.txt")
    ydl_opts = {
        "quiet": True, 
        "no_warnings": True, 
        "skip_download": True,
        "cookiefile": cookie_path
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def fetch_thumbnail(info: dict, dst: Path):
    thumb_url = info.get("thumbnail")
    if not thumb_url:
        return None
    try:
        # Falls back to standard urllib request safely using cookies/headers if needed
        req = urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        dst.write_bytes(data)
        return dst
    except Exception:
        return None


def process_file(audio_path: Path, url: str, cfg: dict) -> Path:
    """Core merge-flow logic: fetch metadata from url, get audio_path into
    a true 320kbps MP3 (copied as-is if already MP3, else converted), tag
    it, and drop it into mp3_folder. Returns the destination path.

    Does not touch audio_path itself -- the caller decides whether/where
    to file the source away afterward.
    """
    dest_dir = Path(cfg["mp3_folder"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(cfg["temp_dir"])
    tmp_dir.mkdir(parents=True, exist_ok=True)

    print("    - Fetching metadata from URL...")
    info = fetch_metadata(url)

    safe_title = core.safe_filename(core.strip_free_dl_tag(info.get("title")) or audio_path.stem)
    safe_artist = core.safe_filename(info.get("uploader") or info.get("artist") or "Unknown Artist")
    dst = core.unique_dest_path(dest_dir, f"{safe_artist} - {safe_title}.mp3")

    if core.is_mp3(audio_path):
        print("    - Source is already MP3 -- copying without re-encoding...")
    else:
        print("    - Converting your file to true 320kbps CBR MP3 (source quality preserved)...")
    core.ensure_320_mp3(audio_path, dst)

    thumb_path = tmp_dir / f"{audio_path.stem}_cover.jpg"
    thumb = fetch_thumbnail(info, thumb_path)

    print("    - Tagging metadata...")
    core.tag_mp3(dst, info, thumb)

    if thumb and thumb.exists():
        thumb.unlink(missing_ok=True)

    return dst