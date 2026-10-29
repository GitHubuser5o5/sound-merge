"""
URL -> Rekordbox-ready MP3 pipeline.

Watches a text file (queue.txt) for URLs. For each new URL it:
  1. Downloads the best available audio. 
  2. Converts it to MP3 at the source's own native bitrate (or 320kbps CBR).
  3. Tags it (title, artist, genre, year, cover art) from metadata.
  4. Moves the finished file into your Rekordbox import/watch folder.

Usage:
    python watcher.py
Then just paste URLs into queue.txt (one per line) and save.
"""

import time
from pathlib import Path

import yt_dlp

import core

FORMAT_SELECTOR = "download/best[acodec=mp3][protocol^=http]/bestaudio/best"


class _YdlLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        print(f"    ! yt-dlp: {msg}")

    def error(self, msg):
        print(f"    ! yt-dlp: {msg}")


def load_queue(queue_path: Path):
    if not queue_path.exists():
        queue_path.touch()
        return []
    lines = [l.strip() for l in queue_path.read_text(encoding="utf-8").splitlines()]
    return [l for l in lines if l and not l.startswith("#")]


def mark_done(queue_path: Path, done_path: Path, url: str):
    lines = queue_path.read_text(encoding="utf-8").splitlines()
    new_lines = []
    removed = False
    for l in lines:
        if not removed and l.strip() == url:
            removed = True
            continue
        new_lines.append(l)
    queue_path.write_text(
        "\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8"
    )
    with open(done_path, "a", encoding="utf-8") as f:
        f.write(url + "\n")


def download(url: str, tmp_dir: Path):
    outtmpl = str(tmp_dir / "%(id)s.%(ext)s")
    cookie_path = str(core.APP_DIR / "www.beatport.com_cookies.txt")
    ydl_opts = {
        "format": FORMAT_SELECTOR,
        "outtmpl": outtmpl,
        "writeinfojson": True,
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ],
        "quiet": True,
        "logger": _YdlLogger(),
        "cookiefile": cookie_path
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return info


def find_downloaded_audio(tmp_dir: Path, track_id: str):
    skip_ext = {"json", "jpg", "jpeg", "png", "webp"}
    for f in tmp_dir.glob(f"{track_id}.*"):
        if f.suffix.lstrip(".").lower() in skip_ext:
            continue
        if ".info" in f.suffixes:
            continue
        return f
    return None


def process_url(url: str, cfg: dict) -> Path | None:
    tmp_dir = Path(cfg["temp_dir"])
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest_dir = Path(cfg["mp3_folder"])
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        info = download(url, tmp_dir)
        track_id = str(info.get("id"))
        safe_title = core.safe_filename(core.strip_free_dl_tag(info.get("title")) or track_id)
        safe_artist = core.safe_filename(info.get("uploader") or info.get("artist") or "Unknown Artist")
        print(f"\n[+] Processing: {safe_title} - {safe_artist}")

        src = find_downloaded_audio(tmp_dir, track_id)
        if src is None:
            print("    ! Could not find downloaded audio file, skipping")
            return None

        format_id = str(info.get("format_id") or "")
        source_abr = info.get("abr")
        is_original = "download" in format_id.lower()
        target_bitrate = 320 if is_original else (int(source_abr) if source_abr else 128)

        dst = core.unique_dest_path(dest_dir, f"{safe_artist} - {safe_title}.mp3")

        if core.is_mp3(src):
            print("    - Source is already MP3 -- copying without re-encoding...")
        elif is_original:
            print("    - Source: free-download original -- converting to true 320kbps CBR MP3...")
        else:
            print(f"    - Source: streamed rip (~{target_bitrate}kbps) -- converting, not upscaled...")
        core.ensure_mp3(src, dst, target_bitrate)

        thumb = tmp_dir / f"{track_id}.jpg"
        thumb = thumb if thumb.exists() else None

        print("    - Tagging metadata...")
        core.tag_mp3(dst, info, thumb)

        for f in tmp_dir.glob(f"{track_id}.*"):
            f.unlink(missing_ok=True)

        print(f"    ✓ Done -> {dst}")
        return dst
    except Exception as e:
        print(f"    ! Failed: {e}")
        return None


def main():
    other_pid = core.find_running_instance()
    if other_pid is not None:
        print(f"! Another instance is already running (PID {other_pid}).")
        print("  Close it first -- running two at once risks the same file being")
        print("  processed twice.")
        return
    core.acquire_lock()

    try:
        cfg = core.load_config()
        queue_path = Path(cfg["queue_file"])
        done_path = Path(cfg["done_log"])
        poll_seconds = cfg.get("poll_seconds", 5)

        print(f"Watching {queue_path.resolve()}")
        print("Paste URLs into it (one per line) and save. Ctrl+C to stop.\n")

        while True:
            for url in load_queue(queue_path):
                process_url(url, cfg)
                mark_done(queue_path, done_path, url)
            time.sleep(poll_seconds)
    finally:
        core.release_lock()


if __name__ == "__main__":
    main()