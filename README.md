<p align="center">
  <img src="sound%20merge%20logo.png" alt="Sound Merge" width="360">
</p>

# Sound Merge

Sound Merge is a Windows desktop app that turns SoundCloud tracks into tagged MP3 files and puts them in your Rekordbox import folder. It either merges a file you already have with its SoundCloud link, or downloads a track from a URL. Playlists, analysis and cue points are still done in Rekordbox.

## Contents

- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Building the exe](#building-the-exe)
- [Queue watcher](#queue-watcher)
- [Tests](#tests)
- [Project layout](#project-layout)
- [Disclaimer](#disclaimer)

## Installation

Requirements:

- Windows
- Python 3.10 or newer
- [ffmpeg](https://ffmpeg.org/download.html) on your `PATH` (`ffmpeg -version` should work in a terminal)

```bash
git clone https://github.com/GitHubuser5o5/sound-merge.git
cd sound-merge
pip install -r requirements.txt
```

Set `mp3_folder` in `config.json` to the folder Rekordbox imports from (see [Configuration](#configuration)), then start the app:

```bash
python gui.py
```

## Usage

The window has two flows.

### Process file + link

For a track you already downloaded yourself, for example from a site that needs a sign-in.

1. Drag the audio file onto the drop zone, or click the zone to browse. It opens in your Downloads folder.
2. Paste the track's SoundCloud URL below it.
3. Click **Process file + link**.

Title, artist, genre, year and cover art are fetched from the URL. No audio is downloaded from SoundCloud in this flow. An MP3 is copied as is. Any other format is converted to 320 kbps CBR MP3. The audio quality is whatever your file already has; nothing is upscaled.

The **Delete original after merge** switch sends the source file to the Recycle Bin after a successful merge. It is off by default and the setting is remembered. The input fields are only cleared after a successful merge, so a failed attempt can be retried as is.

### Download 160kbps rip

1. Paste a SoundCloud URL.
2. Click **Download 160kbps rip**.

The track is downloaded, converted and tagged. Without a signed-in SoundCloud session, most tracks are only available as a stream of about 128-160 kbps, so the output keeps that bitrate instead of being upscaled to 320 kbps. If a free-download original is available, it is encoded as 320 kbps CBR.

### Other behaviour

- Promo text such as `(Free Download)`, `[FREE DL]` or `Free-Download!` is removed from titles.
- One job runs at a time. The buttons are disabled while a job is running.
- The main log shows a short status per job. **Show detailed log** shows the full yt-dlp and ffmpeg output.
- Opening a second copy asks whether to run alongside the first, skip, or close the other one.
- Window size and position are remembered.
- If `wallpaper.jpg` is next to the app, it is used as the background and the window keeps the image's aspect ratio. Without it the window is plain dark. The image is not included in this repository; use any image you like.

## Configuration

`config.json` is read from the folder of `gui.py` or the exe. Relative paths are resolved from that folder.

| Key | Default | Description |
| --- | --- | --- |
| `mp3_folder` | `output_mp3` | Where finished MP3 files are saved. Set this to your Rekordbox import folder, for example `D:/Music/Unsorted`. |
| `queue_file` | `queue.txt` | URL list used by the queue watcher. |
| `done_log` | `done_log.txt` | URLs the watcher has already processed. |
| `temp_dir` | `temp_downloads` | Scratch space for downloads and cover art. |
| `input_folder` | `input_downloads` | Working folder for local file merges. |
| `poll_seconds` | `5` | How often the watcher checks `queue_file`. |

If a site needs a signed-in session, export your cookies in Netscape format and save them as `www.beatport.com_cookies.txt` next to the app. The file is passed to yt-dlp. It is listed in `.gitignore`; do not commit it.

## Building the exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --collect-data tkinterdnd2 --collect-data customtkinter --icon "sound merge icon.ico" --name "Sound Merge" gui.py
```

Copy `config.json`, `sound merge logo.png` and `sound merge icon.png` (and `wallpaper.jpg`, if you use one) next to the exe in `dist/`. The app looks for its config, images and working folders next to the exe, not in the current directory.

## Queue watcher

`watcher.py` does the same job as **Download 160kbps rip** from the terminal. Start it, then add SoundCloud URLs to `queue.txt`, one per line, and save the file. Each new URL is downloaded, converted, tagged and moved to `mp3_folder`.

```bash
python watcher.py
```

## Tests

```bash
python test_core.py
```

A smoke test of the shared helpers: filename sanitising, MP3 pass-through versus conversion, tagging, duplicate file naming, the single-instance lock and settings storage. Run it before building the exe.

## Project layout

```
gui.py           customtkinter app (entry point)
core.py          config, ffmpeg conversion, tagging, instance lock
watch_input.py   "Process file + link" pipeline
watcher.py       URL to MP3 pipeline and queue watcher
test_core.py     smoke tests for core.py
config.json      folder settings
Sound Merge.spec PyInstaller spec
```

## Disclaimer

Not affiliated with SoundCloud, Beatport, Pioneer DJ / AlphaTheta or Rekordbox. Only download and convert music you have the right to use, and follow the terms of the services you use.
