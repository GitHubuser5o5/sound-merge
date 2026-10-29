"""
Quick smoke test for core.py's helpers -- run before each rebuild to catch
regressions without a manual click-through of the GUI.

Usage:
    python test_core.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import core


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    return condition


def main():
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="dj_downlist_test_"))

    # -- safe_filename --
    ok &= check(
        "safe_filename strips illegal chars",
        core.safe_filename('Song: "Remix" <2026>?') == "Song Remix 2026",
    )
    ok &= check("safe_filename handles None", core.safe_filename(None) == "")

    # -- strip_free_dl_tag: common real-world "free download" promo styles --
    free_dl_cases = [
        ("Song Name (Free Download)", "Song Name"),
        ("Song Name [FREE DL]", "Song Name"),
        ("Song Name - FREE DOWNLOAD", "Song Name"),
        ("Song Name Free Download!", "Song Name"),
        ("(Free DL) Song Name", "Song Name"),
        ("[FREE DOWNLOAD!!] Song Name", "Song Name"),
        ("Song Name (free-dl)", "Song Name"),
        ("Song Name [Free_Download]", "Song Name"),
        ("Song Name | Free Dl", "Song Name"),
        ("SONG NAME (FREE DOWNLOADS)", "SONG NAME"),
        ("Free Bird", "Free Bird"),  # bare "free" must NOT be stripped
        ("Song Name (Extended Mix)", "Song Name (Extended Mix)"),  # unrelated tag untouched
        (None, None),
        ("", ""),
    ]
    for raw, expected in free_dl_cases:
        ok &= check(f"strip_free_dl_tag({raw!r}) -> {expected!r}", core.strip_free_dl_tag(raw) == expected)

    # -- is_mp3 --
    ok &= check("is_mp3 true for .mp3", core.is_mp3(Path("track.mp3")) is True)
    ok &= check("is_mp3 false for .wav", core.is_mp3(Path("track.wav")) is False)
    ok &= check("is_mp3 case-insensitive", core.is_mp3(Path("track.MP3")) is True)

    # -- unique_dest_path --
    (tmp / "Song.mp3").touch()
    (tmp / "Song (2).mp3").touch()
    ok &= check(
        "unique_dest_path skips taken names",
        core.unique_dest_path(tmp, "Song.mp3").name == "Song (3).mp3",
    )
    ok &= check(
        "unique_dest_path leaves free names alone",
        core.unique_dest_path(tmp, "NewSong.mp3").name == "NewSong.mp3",
    )

    # -- ensure_mp3: mp3 source is copied, never re-encoded --
    src_mp3 = tmp / "already.mp3"
    src_mp3.write_bytes(b"not real mp3 data, just checking it's copied byte-for-byte")
    dst_copy = tmp / "copied.mp3"
    core.ensure_mp3(src_mp3, dst_copy, 320)
    ok &= check(
        "ensure_mp3 copies an existing mp3 unchanged",
        dst_copy.exists() and dst_copy.read_bytes() == src_mp3.read_bytes(),
    )

    # -- ensure_mp3: non-mp3 source gets converted (needs ffmpeg) --
    has_ffmpeg = shutil.which("ffmpeg") is not None
    if has_ffmpeg:
        src_wav = tmp / "tone.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(src_wav)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        dst_conv = tmp / "converted.mp3"
        core.ensure_mp3(src_wav, dst_conv, 128)
        ok &= check(
            "ensure_mp3 converts a non-mp3 source to a real MP3 file",
            dst_conv.exists() and dst_conv.stat().st_size > 1000,
        )

        # -- tag_mp3: write then read back --
        info = {"title": "Test Title", "uploader": "Test Artist", "tags": ["house", "test"], "upload_date": "20260101"}
        core.tag_mp3(dst_conv, info)
        from mutagen.id3 import ID3
        tags = ID3(dst_conv)
        ok &= check("tag_mp3 writes title", str(tags["TIT2"]) == "Test Title")
        ok &= check("tag_mp3 writes artist", str(tags["TPE1"]) == "Test Artist")
        ok &= check("tag_mp3 writes year", str(tags["TDRC"]) == "2026")
    else:
        print("[SKIP] ensure_mp3 conversion + tag_mp3 tests -- ffmpeg not on PATH")

    # -- single-instance lock --
    core.acquire_lock()
    ok &= check("lock file created", core.LOCK_PATH.exists())
    ok &= check("find_running_instance ignores our own pid", core.find_running_instance() is None)
    core.release_lock()
    ok &= check("lock file removed on release", not core.LOCK_PATH.exists())

    # -- settings persistence (back up/restore the real file so this test
    # never clobbers actual saved preferences) --
    settings_backup = core.SETTINGS_PATH.read_text(encoding="utf-8") if core.SETTINGS_PATH.exists() else None
    try:
        core.SETTINGS_PATH.unlink(missing_ok=True)
        ok &= check(
            "load_settings returns {} when no file exists",
            not core.SETTINGS_PATH.exists() and core.load_settings() == {},
        )
        core.save_settings({"delete_after_merge": True})
        ok &= check(
            "save_settings/load_settings round-trip",
            core.load_settings() == {"delete_after_merge": True},
        )
    finally:
        if settings_backup is None:
            core.SETTINGS_PATH.unlink(missing_ok=True)
        else:
            core.SETTINGS_PATH.write_text(settings_backup, encoding="utf-8")

    # -- move_to_trash --
    trash_test_file = tmp / "trash_me.txt"
    trash_test_file.write_text("throwaway")
    core.move_to_trash(trash_test_file)
    ok &= check("move_to_trash removes the file from its original location", not trash_test_file.exists())

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("ALL PASSED" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
