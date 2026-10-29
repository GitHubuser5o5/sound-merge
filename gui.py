"""
Minimal customtkinter GUI for the DJ track pipeline, branded "Sound Merge".

Two actions:
  - "Process file + link": merge flow (same logic as watch_input.py) --
    drop an audio file onto the drop zone (or click it to browse), paste
    its matching SoundCloud URL, convert to 320kbps MP3 (or copy as-is if
    already MP3), tag, drop into mp3_folder. The "Delete original after
    merge" switch (state persisted across restarts in gui_settings.json)
    controls whether the dragged-in file is sent to the Recycle Bin after
    a successful merge, or left exactly where it is. The form (file +
    URL) only resets after a successful merge -- never just from picking
    a new file.
  - "Download 160kbps rip": direct flow (same logic as watcher.py) --
    paste a SoundCloud URL, download + tag. Without a logged-in SoundCloud
    session this is effectively always a streamed rip (~128-160kbps), kept
    at its native bitrate rather than upscaled to 320kbps.

One file/URL at a time -- buttons are disabled while a job is running.

Two log views:
  - Progress: always visible, a short clean per-job status (name + steps).
  - Detailed log: a separate popup window (toggled via the button at the
    bottom) with the full raw output from watch_input.py/watcher.py/yt-dlp.

Background: a flat solid color (PANEL_BG), freely resizable, no aspect
lock. Window size + screen position are remembered across restarts.

Widget toolkit note: customtkinter is used (instead of plain tk)
specifically because it's the only practical way to get real rounded
corners and a real toggle switch in a Tkinter app without hand-drawing
every widget on a canvas.
"""

import atexit
import contextlib
import ctypes
import os
import shutil
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image
from tkinterdnd2 import DND_FILES, TkinterDnD

import core
import watch_input
import watcher

# Guard against a windowed (--noconsole) PyInstaller build, where
# sys.stdout/sys.stderr are None and any print() would crash the worker
# threads that watch_input/watcher modules log to.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

APP_TITLE = "Sound Merge"
LOGO_PATH = core.APP_DIR / "sound merge logo.png"
ICON_PATH = core.APP_DIR / "sound merge icon.png"
LOGO_DISPLAY_WIDTH = 340

DEFAULT_WH = (720, 780)
MIN_WH = (480, 560)

PANEL_BG = "#141318"
ENTRY_BG = "#1f1d21"
BORDER = "#2a2724"
TEXT_PRIMARY = "#f2f0ec"
TEXT_SECONDARY = "#8f8b83"
ACCENT_RED = "#e0475a"
ACCENT_RED_HOVER = "#c9394b"
ACCENT_TEAL = "#35c9d1"
SWITCH_ON = ACCENT_TEAL
SWITCH_OFF = "#3a3a3c"
SWITCH_KNOB = "#ffffff"

FONT_FAMILY = "Segoe UI"

SC_URL_LIST_HEIGHT = 66  # fixed, compact -- just over one row tall; scrolls internally for more
PROGRESS_HEIGHT = 200  # fixed, compact -- scrolls internally rather than filling the window

DROP_ZONE_PLACEHOLDER = "Drop audio file here\n(or click to browse)"
DETAIL_LOG_GEOMETRY = "680x500"

# customtkinter auto-detects Windows DPI scaling and applies its own scale
# factor inside geometry()/minsize()/maxsize() -- but not inside aspect(),
# which it doesn't override. With both active they fight each other and
# distort the locked aspect ratio. We handle sizing precisely ourselves,
# so this scaling layer is unwanted here.
ctk.deactivate_automatic_dpi_awareness()
ctk.set_appearance_mode("dark")


def _font(size, weight="normal"):
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)


_icon_ico_path = None  # cached generated .ico, computed once per run


def _apply_icon(window):
    """Set this window's (and its taskbar entry's) icon from
    sound merge icon.png. The exe file's own icon (shown in Explorer
    before it's even running) is set separately via PyInstaller's --icon
    flag at build time -- this covers the icon while the app is running.

    Uses iconbitmap() with a real .ico (converted from the PNG once via
    Pillow), not iconphoto() -- customtkinter's CTk schedules its own
    call 200ms after creation that resets the icon to its own default
    UNLESS iconbitmap() was the method used to set it (that's the one
    thing it checks for). iconphoto() doesn't register, so it silently
    gets overwritten a fraction of a second after being set.
    """
    global _icon_ico_path
    if not ICON_PATH.exists():
        return
    try:
        if _icon_ico_path is None:
            candidate = core.APP_DIR / "sound merge icon.ico"
            img = Image.open(ICON_PATH).convert("RGBA")
            img.save(candidate, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
            _icon_ico_path = candidate
        window.iconbitmap(default=str(_icon_ico_path))
    except Exception:
        pass


def _apply_dark_titlebar(root):
    """Ask Windows to draw this window's native title bar in dark mode, so
    it matches the app's own dark theme instead of the default white bar.
    Silently does nothing on unsupported Windows versions.
    """
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE: 20 (new), 19 (older builds)
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
            if result == 0:
                break
    except Exception:
        pass


def _make_scrollbar_autohide(textbox, hidden_color, visible_color):
    """Blend the textbox's scrollbar thumb into the background until the
    mouse hovers over the textbox, then reveal it -- avoids reaching into
    customtkinter's private scrollbar internals by just recoloring the
    thumb via its public scrollbar_button_color option.
    """
    textbox.configure(scrollbar_button_color=hidden_color)
    textbox.bind("<Enter>", lambda e: textbox.configure(scrollbar_button_color=visible_color))
    textbox.bind("<Leave>", lambda e: textbox.configure(scrollbar_button_color=hidden_color))


class _LogStream:
    """File-like object that forwards each printed line to a log callback.

    watch_input.process_file() and watcher.process_url() report detailed
    progress via print(). Redirecting stdout through this during a job
    routes those raw lines into the detailed log instead of the console.
    """

    def __init__(self, log_fn):
        self.log_fn = log_fn
        self._buf = ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self.log_fn(line)

    def flush(self):
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.audio_path = None
        self.busy = False

        settings = core.load_settings()
        self.delete_var = ctk.BooleanVar(value=settings.get("delete_after_merge", False))

        # The stacked sections need more height than comfortably fits in a
        # default-sized window (or many screens at all) without either
        # compressing the fixed-height panels or filling the window with
        # empty space. Making the whole thing scrollable lets every panel
        # stay compact/fixed-size regardless of window size -- you scroll
        # the app, not stretch its widgets.
        self.content = ctk.CTkScrollableFrame(
            root, fg_color=PANEL_BG, corner_radius=0,
            scrollbar_fg_color=PANEL_BG, scrollbar_button_color=ENTRY_BG, scrollbar_button_hover_color=ACCENT_TEAL,
        )
        self.content.pack(fill="both", expand=True)

        c = self.content

        # -- logo, always centered --
        if LOGO_PATH.exists():
            try:
                logo_img = Image.open(LOGO_PATH).convert("RGBA")
                logo_h = round(LOGO_DISPLAY_WIDTH * logo_img.height / logo_img.width)
                ctk_logo = ctk.CTkImage(light_image=logo_img, dark_image=logo_img,
                                         size=(LOGO_DISPLAY_WIDTH, logo_h))
                ctk.CTkLabel(c, image=ctk_logo, text="", fg_color=PANEL_BG).pack(pady=(26, 14))
            except Exception:
                ctk.CTkLabel(c, text=APP_TITLE, font=_font(26), text_color=TEXT_PRIMARY, fg_color=PANEL_BG).pack(pady=(26, 14))
        else:
            ctk.CTkLabel(c, text=APP_TITLE, font=_font(26), text_color=TEXT_PRIMARY, fg_color=PANEL_BG).pack(pady=(26, 14))

        # -- file + link flow --
        self._section_label(c, "File + link").pack(anchor="w", padx=24)

        file_row = ctk.CTkFrame(c, fg_color=PANEL_BG)
        file_row.pack(fill="x", padx=24, pady=(8, 0))

        # switch packed first, hugging the right edge -- drop zone (packed
        # after, fill+expand) then claims all remaining space
        delete_container = ctk.CTkFrame(file_row, fg_color=PANEL_BG)
        delete_container.pack(side="right", padx=(10, 0))

        self.delete_switch = ctk.CTkSwitch(
            delete_container, text="", variable=self.delete_var,
            command=self._on_toggle_delete,
            fg_color=SWITCH_OFF, progress_color=SWITCH_ON,
            button_color=SWITCH_KNOB, button_hover_color=SWITCH_KNOB,
            switch_width=58, switch_height=30, corner_radius=15, border_width=0,
        )
        self.delete_switch.pack()
        ctk.CTkLabel(
            delete_container, text="Delete original\nafter merge", font=_font(11),
            text_color=TEXT_SECONDARY, fg_color=PANEL_BG, justify="center",
        ).pack(pady=(6, 0))

        self.drop_zone = ctk.CTkLabel(
            file_row, text=DROP_ZONE_PLACEHOLDER, font=_font(16),
            fg_color=ENTRY_BG, text_color=ACCENT_RED, corner_radius=16,
            cursor="hand2", justify="center", height=150,
        )
        self.drop_zone.pack(side="left", fill="both", expand=True)
        self.drop_zone.bind("<Button-1>", lambda e: self.browse_file())
        self.drop_zone.drop_target_register(DND_FILES)
        self.drop_zone.dnd_bind("<<Drop>>", self.handle_drop)

        self._section_label(c, "SoundCloud / Beatport URL for that file").pack(anchor="w", padx=24, pady=(18, 0))
        self.file_url_entry = self._entry(c)
        self.file_url_entry.pack(fill="x", padx=24, pady=(8, 0))

        self.process_btn = self._button(c, "Process file + link", self.run_process_file, primary=True)
        self.process_btn.pack(fill="x", padx=24, pady=(14, 8))

        self.multi_merge_btn = self._button(c, "Multi merge", self.open_multi_merge, primary=False)
        self.multi_merge_btn.pack(fill="x", padx=24, pady=(0, 26))

        # -- direct download flow: collapsed by default (rarely used) --
        # A slim clickable bar; clicking reveals the url-box list + download
        # button. Kept out of the way until you actually need it.
        self.sc_section_expanded = False
        self.sc_toggle_btn = ctk.CTkButton(
            c, text="▸  DIRECT DOWNLOAD", command=self._toggle_sc_section,
            font=_font(12), anchor="w",
            fg_color="transparent", hover_color=ENTRY_BG, text_color=TEXT_SECONDARY,
            corner_radius=8, height=32,
        )
        self.sc_toggle_btn.pack(fill="x", padx=24, pady=(0, 0))

        self.sc_section_frame = ctk.CTkFrame(c, fg_color=PANEL_BG)
        # not packed yet -- hidden until _toggle_sc_section() reveals it

        self.sc_url_list_frame = ctk.CTkScrollableFrame(self.sc_section_frame, fg_color=ENTRY_BG, corner_radius=14, height=SC_URL_LIST_HEIGHT)
        self.sc_url_list_frame.pack(fill="x")
        self.sc_url_rows = []
        self.sc_url_entries = []
        self._add_sc_url_box()

        dl_row = ctk.CTkFrame(self.sc_section_frame, fg_color=PANEL_BG)
        dl_row.pack(fill="x", pady=(14, 0))
        self.download_btn = self._button(dl_row, "Download 160kbps rip", self.run_download, primary=False)
        self.download_btn.pack(side="left", fill="x", expand=True)
        self.add_url_btn = ctk.CTkButton(
            dl_row, text="+", command=self._add_sc_url_box, font=_font(20, "bold"),
            fg_color=PANEL_BG, hover_color=ENTRY_BG, text_color=ACCENT_TEAL,
            border_width=1, border_color=ACCENT_TEAL, corner_radius=12, width=46, height=46,
        )
        self.add_url_btn.pack(side="left", padx=(10, 0))

        # -- progress panel: always visible, clean summary --
        self.progress_section_label = self._section_label(c, "Progress")
        self.progress_section_label.pack(anchor="w", padx=24, pady=(14, 0))
        self.progress_widget = ctk.CTkTextbox(
            c, font=_font(14), fg_color=ENTRY_BG, text_color=TEXT_PRIMARY,
            corner_radius=14, border_width=1, border_color=BORDER, state="disabled",
            height=PROGRESS_HEIGHT,
        )
        self.progress_widget.pack(fill="x", padx=24, pady=(8, 14))
        _make_scrollbar_autohide(self.progress_widget, hidden_color=ENTRY_BG, visible_color=ACCENT_TEAL)

        # -- detailed log: separate popup window, not embedded --
        self.toggle_btn = self._button(c, "Show detailed log", self.toggle_detail, primary=False)
        self.toggle_btn.pack(fill="x", padx=24, pady=(0, 24))

        self.detail_win = ctk.CTkToplevel(root)
        self.detail_win.title(f"{APP_TITLE} -- detailed log")
        self.detail_win.geometry(DETAIL_LOG_GEOMETRY)
        self.detail_win.configure(fg_color=PANEL_BG)
        self.detail_win.protocol("WM_DELETE_WINDOW", self._hide_detail)
        _apply_icon(self.detail_win)
        self.detail_widget = ctk.CTkTextbox(
            self.detail_win, font=_font(14), fg_color=ENTRY_BG, text_color=TEXT_PRIMARY,
            corner_radius=14, border_width=1, border_color=BORDER, state="disabled",
        )
        self.detail_widget.pack(fill="both", expand=True, padx=14, pady=14)
        _make_scrollbar_autohide(self.detail_widget, hidden_color=ENTRY_BG, visible_color=ACCENT_TEAL)
        self.root.after(50, self.detail_win.withdraw)
        self.root.after(60, lambda: _apply_dark_titlebar(self.detail_win))

    # -- theming helpers --

    def _section_label(self, parent, text):
        return ctk.CTkLabel(parent, text=text.upper(), font=_font(13), text_color=TEXT_SECONDARY, fg_color=PANEL_BG)

    def _entry(self, parent):
        return ctk.CTkEntry(
            parent, font=_font(15), fg_color=ENTRY_BG, text_color=TEXT_PRIMARY,
            border_color=BORDER, corner_radius=12, height=46,
        )

    def _button(self, parent, text, command, primary):
        if primary:
            return ctk.CTkButton(
                parent, text=text, command=command, font=_font(17, "bold"),
                fg_color=ACCENT_RED, hover_color=ACCENT_RED_HOVER, text_color=PANEL_BG,
                corner_radius=12, height=52,
            )
        return ctk.CTkButton(
            parent, text=text, command=command, font=_font(15),
            fg_color=PANEL_BG, hover_color=ENTRY_BG, text_color=ACCENT_TEAL,
            border_width=1, border_color=ACCENT_TEAL, corner_radius=12, height=46,
        )

    # -- detail log popup window --

    def toggle_detail(self):
        if self.detail_win.state() == "withdrawn":
            self.detail_win.deiconify()
            self.detail_win.lift()
            self.toggle_btn.configure(text="Hide detailed log")
        else:
            self._hide_detail()

    def _hide_detail(self):
        self.detail_win.withdraw()
        self.toggle_btn.configure(text="Show detailed log")

    # -- logging (thread-safe: always marshaled onto the Tk main loop) --

    def log_progress(self, msg):
        self.root.after(0, self._append, self.progress_widget, msg)

    def log_detail(self, msg):
        self.root.after(0, self._append, self.detail_widget, msg)

    def _append(self, widget, msg):
        widget.configure(state="normal")
        widget.insert(tk.END, msg + "\n")
        widget.see(tk.END)
        widget.configure(state="disabled")

    # -- busy state: one job at a time --

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.process_btn.configure(state=state)
        self.download_btn.configure(state=state)
        self.multi_merge_btn.configure(state=state)

    # -- file + link flow --

    def _on_toggle_delete(self):
        settings = core.load_settings()
        settings["delete_after_merge"] = bool(self.delete_var.get())
        core.save_settings(settings)

    def browse_file(self):
        if self.busy:
            return
        downloads = Path.home() / "Downloads"
        path = filedialog.askopenfilename(
            title="Select audio file",
            initialdir=str(downloads) if downloads.is_dir() else None,
            filetypes=[("Audio files", "*.mp3 *.wav *.flac *.aiff *.aif *.m4a"), ("All files", "*.*")],
        )
        if not path:
            return
        self._set_audio_file(Path(path))

    def handle_drop(self, event):
        if self.busy:
            return
        paths = self.root.tk.splitlist(event.data)
        if not paths:
            return
        if len(paths) > 1:
            messagebox.showerror("Multiple files", "Drop one audio file at a time.")
            return
        path = Path(paths[0])
        if not path.is_file():
            messagebox.showerror("Not a file", f"That's not a file:\n{path}")
            return
        self._set_audio_file(path)

    def _set_audio_file(self, path: Path):
        self.audio_path = path
        self.drop_zone.configure(text=path.name)

    def _reset_file_form(self):
        self.audio_path = None
        self.drop_zone.configure(text=DROP_ZONE_PLACEHOLDER)
        self.file_url_entry.delete(0, "end")

    def run_process_file(self):
        if self.busy:
            return
        if not self.audio_path:
            messagebox.showerror("No file", "Choose or drop an audio file first.")
            return
        url = self.file_url_entry.get().strip()
        if not url:
            messagebox.showerror("No URL", "Paste the matching URL first.")
            return
        delete_after = bool(self.delete_var.get())
        self.set_busy(True)
        threading.Thread(
            target=self._process_file_worker,
            args=(self.audio_path, url, delete_after),
            daemon=True,
        ).start()

    def _process_file_worker(self, audio_path, url, delete_after):
        self.log_progress(f"\n{audio_path.name}")
        self.log_progress("* merging file with metadata...")
        try:
            cfg = core.load_config()
            with contextlib.redirect_stdout(_LogStream(self.log_detail)):
                dst = watch_input.process_file(audio_path, url, cfg)

            if delete_after:
                core.move_to_trash(audio_path)
                self.log_detail(f"Sent original to Recycle Bin: {audio_path}")

            self.log_progress(f"* finished -> {dst.name}")
            self.root.after(0, self._reset_file_form)
        except Exception as e:
            self.log_progress(f"* failed: {e}")
        finally:
            self.root.after(0, self.set_busy, False)

    # -- direct SoundCloud download flow (collapsed by default, a growable list of URL boxes) --
    #
    # The list area itself is a fixed, compact height (SC_URL_LIST_HEIGHT)
    # -- it never grows the window; it just scrolls internally as more
    # boxes are added via "+". Every row has its own remove button, in
    # case "+" gets clicked by accident -- removing the last remaining
    # row just clears it instead, so there's always at least one box.

    def _toggle_sc_section(self):
        self.sc_section_expanded = not self.sc_section_expanded
        if self.sc_section_expanded:
            self.sc_toggle_btn.configure(text="▾  DIRECT DOWNLOAD")
            # pack() re-appends to the END of the packing order by default,
            # which would drop this below "Progress" -- anchor it back into
            # its proper spot, right before the Progress section label.
            self.sc_section_frame.pack(fill="x", padx=24, pady=(8, 0), before=self.progress_section_label)
        else:
            self.sc_toggle_btn.configure(text="▸  DIRECT DOWNLOAD")
            self.sc_section_frame.pack_forget()

    def _add_sc_url_box(self):
        row = ctk.CTkFrame(self.sc_url_list_frame, fg_color=ENTRY_BG)
        row.pack(fill="x", pady=6)

        entry = self._entry(row)
        entry.pack(side="left", fill="x", expand=True)

        remove_btn = ctk.CTkButton(
            row, text="x", width=32, height=32, font=_font(13, "bold"),
            fg_color=ENTRY_BG, hover_color=PANEL_BG, text_color=TEXT_SECONDARY,
            corner_radius=8, command=lambda: self._remove_sc_url_box(row),
        )
        remove_btn.pack(side="left", padx=(8, 0))

        self.sc_url_rows.append((row, entry))
        self.sc_url_entries.append(entry)

    def _remove_sc_url_box(self, row):
        if len(self.sc_url_rows) <= 1:
            self.sc_url_rows[0][1].delete(0, "end")
            return
        self.sc_url_rows = [(r, e) for (r, e) in self.sc_url_rows if r is not row]
        self.sc_url_entries = [e for (_, e) in self.sc_url_rows]
        row.destroy()

    def _reset_sc_url_boxes(self):
        for row, _entry in self.sc_url_rows:
            row.destroy()
        self.sc_url_rows = []
        self.sc_url_entries = []
        self._add_sc_url_box()

    def run_download(self):
        if self.busy:
            return
        urls = [e.get().strip() for e in self.sc_url_entries if e.get().strip()]
        if not urls:
            messagebox.showerror("No URL", "Paste at least one URL first.")
            return
        self.set_busy(True)
        threading.Thread(target=self._download_worker_batch, args=(urls,), daemon=True).start()

    def _download_worker_batch(self, urls):
        for url in urls:
            self.log_progress(f"\n{url}")
            self.log_progress("* downloading...")
            try:
                cfg = core.load_config()
                with contextlib.redirect_stdout(_LogStream(self.log_detail)):
                    dst = watcher.process_url(url, cfg)
                if dst:
                    self.log_progress(f"* finished -> {dst.name}")
                else:
                    self.log_progress("* failed -- see detailed log")
            except Exception as e:
                self.log_progress(f"* failed: {e}")
        self.root.after(0, self._reset_sc_url_boxes)
        self.root.after(0, self.set_busy, False)

    # -- multi merge: collect several links, then pair each with a file in turn --

    def open_multi_merge(self):
        if self.busy:
            messagebox.showerror("Busy", "Wait for the current job to finish first.")
            return
        MultiMergeWindow(self)


class MultiMergeWindow:
    """Popup flow for merging several file+link pairs in one guided pass.

    Page 1: paste links one at a time -- each box that becomes non-empty
    grows a fresh empty box underneath it, so you never have to click
    anything to keep adding more.
    Page 2: once you click "That's all the links", steps through each
    link in order -- fetches its title/artist, shows a drop zone, and as
    soon as you provide the matching file it merges automatically and
    advances to the next link. A failed merge stays on that same link
    (with the error shown) so you can retry with a different file rather
    than silently skipping it.

    Reuses the main App's log_progress/log_detail so all activity shows
    up in the same Progress panel and Detailed log as the other flows,
    and reuses App.busy/set_busy so it can't overlap with a job started
    from the main window.
    """

    def __init__(self, app: App):
        self.app = app
        self.url_entries = []
        self.links = []
        self.current_index = 0

        self.win = ctk.CTkToplevel(app.root)
        self.win.title(f"{APP_TITLE} -- multi merge")
        self.win.geometry("640x600")
        self.win.minsize(480, 420)
        self.win.configure(fg_color=PANEL_BG)
        _apply_icon(self.win)
        self.win.after(60, lambda: _apply_dark_titlebar(self.win))
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        self._build_collect_page()

    def _close(self):
        self.win.destroy()

    # -- page 1: collect links --

    def _build_collect_page(self):
        self.collect_frame = ctk.CTkFrame(self.win, fg_color=PANEL_BG)
        self.collect_frame.pack(fill="both", expand=True, padx=22, pady=22)

        self.app._section_label(self.collect_frame, "Paste links, one at a time").pack(anchor="w")

        self.url_list_frame = ctk.CTkScrollableFrame(self.collect_frame, fg_color=ENTRY_BG, corner_radius=14)
        self.url_list_frame.pack(fill="both", expand=True, pady=(10, 16))

        self._add_url_box()

        self.done_btn = self.app._button(self.collect_frame, "That's all the links", self._finish_collecting, primary=True)
        self.done_btn.pack(fill="x")

    def _add_url_box(self):
        entry = self.app._entry(self.url_list_frame)
        entry.pack(fill="x", pady=6)
        entry.bind("<KeyRelease>", lambda e, ent=entry: self._on_url_box_changed(ent))
        self.url_entries.append(entry)

    def _on_url_box_changed(self, entry):
        if entry is not self.url_entries[-1]:
            return
        if entry.get().strip():
            self._add_url_box()

    def _finish_collecting(self):
        self.links = [e.get().strip() for e in self.url_entries if e.get().strip()]
        if not self.links:
            messagebox.showerror("No links", "Paste at least one SoundCloud URL first.")
            return
        self.current_index = 0
        self.collect_frame.pack_forget()
        self._build_pair_page()
        self._load_current_pair()

    # -- page 2: step through each link, pairing it with a file --

    def _build_pair_page(self):
        self.pair_frame = ctk.CTkFrame(self.win, fg_color=PANEL_BG)
        self.pair_frame.pack(fill="both", expand=True, padx=22, pady=22)

        self.pair_counter_label = self.app._section_label(self.pair_frame, "")
        self.pair_counter_label.pack(anchor="w")

        self.pair_title_label = ctk.CTkLabel(
            self.pair_frame, text="Fetching metadata...", font=_font(18, "bold"),
            text_color=TEXT_PRIMARY, fg_color=PANEL_BG, wraplength=560, justify="left",
        )
        self.pair_title_label.pack(anchor="w", pady=(8, 18))

        self.pair_drop_zone = ctk.CTkLabel(
            self.pair_frame, text=DROP_ZONE_PLACEHOLDER, font=_font(16),
            fg_color=ENTRY_BG, text_color=ACCENT_RED, corner_radius=16,
            cursor="hand2", justify="center", height=140,
        )
        self.pair_drop_zone.pack(fill="both", expand=True)
        self.pair_drop_zone.bind("<Button-1>", lambda e: self._browse_pair_file())
        self.pair_drop_zone.drop_target_register(DND_FILES)
        self.pair_drop_zone.dnd_bind("<<Drop>>", self._handle_pair_drop)

        self.skip_btn = self.app._button(self.pair_frame, "Skip this link", self._skip_current, primary=False)
        self.skip_btn.pack(fill="x", pady=(12, 0))

        self.pair_status_label = ctk.CTkLabel(
            self.pair_frame, text="", font=_font(12),
            text_color=TEXT_SECONDARY, fg_color=PANEL_BG, wraplength=560, justify="left",
        )
        self.pair_status_label.pack(anchor="w", pady=(10, 0))

    def _load_current_pair(self):
        url = self.links[self.current_index]
        self.pair_counter_label.configure(text=f"Link {self.current_index + 1} of {len(self.links)}")
        self.pair_title_label.configure(text="Fetching metadata...")
        self.pair_status_label.configure(text="")
        self.pair_drop_zone.configure(text=DROP_ZONE_PLACEHOLDER)
        threading.Thread(target=self._fetch_pair_metadata, args=(url,), daemon=True).start()

    def _fetch_pair_metadata(self, url):
        try:
            info = watch_input.fetch_metadata(url)
            title = core.strip_free_dl_tag(info.get("title")) or "Unknown title"
            artist = info.get("uploader") or "Unknown artist"
            self.win.after(0, lambda: self.pair_title_label.configure(text=f"{title} -- {artist}"))
        except Exception as e:
            self.win.after(0, lambda: self.pair_title_label.configure(text=f"Could not fetch metadata: {e}"))

    def _browse_pair_file(self):
        if self.app.busy:
            return
        downloads = Path.home() / "Downloads"
        path = filedialog.askopenfilename(
            title="Select audio file",
            initialdir=str(downloads) if downloads.is_dir() else None,
            filetypes=[("Audio files", "*.mp3 *.wav *.flac *.aiff *.aif *.m4a"), ("All files", "*.*")],
        )
        if path:
            self._process_pair_file(Path(path))

    def _handle_pair_drop(self, event):
        if self.app.busy:
            return
        paths = self.win.tk.splitlist(event.data)
        if not paths:
            return
        if len(paths) > 1:
            messagebox.showerror("Multiple files", "Drop one audio file at a time.")
            return
        path = Path(paths[0])
        if not path.is_file():
            messagebox.showerror("Not a file", f"That's not a file:\n{path}")
            return
        self._process_pair_file(path)

    def _process_pair_file(self, audio_path):
        self.pair_drop_zone.configure(text=audio_path.name)
        self.pair_status_label.configure(text="Merging...", text_color=TEXT_SECONDARY)
        url = self.links[self.current_index]
        delete_after = bool(self.app.delete_var.get())
        self.app.set_busy(True)
        threading.Thread(target=self._process_pair_worker, args=(audio_path, url, delete_after), daemon=True).start()

    def _process_pair_worker(self, audio_path, url, delete_after):
        n, total = self.current_index + 1, len(self.links)
        self.app.log_progress(f"\n[multi merge {n}/{total}] {audio_path.name}")
        try:
            cfg = core.load_config()
            with contextlib.redirect_stdout(_LogStream(self.app.log_detail)):
                dst = watch_input.process_file(audio_path, url, cfg)

            if delete_after:
                core.move_to_trash(audio_path)
                self.app.log_detail(f"Sent original to Recycle Bin: {audio_path}")

            self.app.log_progress(f"* finished -> {dst.name}")
            self.win.after(0, self._advance_to_next)
        except Exception as e:
            self.app.log_progress(f"* failed: {e}")
            self.win.after(0, lambda: self.pair_status_label.configure(
                text=f"Failed: {e}\nDrop a different file to retry this link, or click Skip.", text_color=ACCENT_RED,
            ))
        finally:
            self.win.after(0, self.app.set_busy, False)

    def _skip_current(self):
        if self.app.busy:
            return
        url = self.links[self.current_index]
        n, total = self.current_index + 1, len(self.links)
        self.app.log_progress(f"\n[multi merge {n}/{total}] skipped")
        self.app.log_detail(f"Skipped link: {url}")
        self._advance_to_next()

    def _advance_to_next(self):
        self.current_index += 1
        if self.current_index >= len(self.links):
            self._show_done()
        else:
            self._load_current_pair()

    def _show_done(self):
        self.pair_frame.pack_forget()
        done_frame = ctk.CTkFrame(self.win, fg_color=PANEL_BG)
        done_frame.pack(fill="both", expand=True, padx=22, pady=22)
        ctk.CTkLabel(
            done_frame, text=f"All {len(self.links)} links merged.", font=_font(18, "bold"),
            text_color=TEXT_PRIMARY, fg_color=PANEL_BG,
        ).pack(pady=(60, 24))
        self.app._button(done_frame, "Close", self._close, primary=True).pack(padx=80)


def run_startup_checks():
    """Sanity-check the environment before showing the window. Returns
    (cfg, problems) -- problems is a list of human-readable strings, empty
    when everything looks fine (in which case nothing is shown to the
    user at all; a popup only appears when something is actually wrong).
    """
    try:
        cfg = core.load_config()
    except Exception as e:
        return None, [f"Could not read config.json: {e}"]

    problems = []
    if shutil.which("ffmpeg") is None:
        problems.append("ffmpeg was not found on PATH -- conversions will fail until it's installed.")

    mp3_folder = Path(cfg["mp3_folder"])
    if not mp3_folder.parent.exists():
        problems.append(
            f"mp3_folder's parent folder doesn't exist:\n{mp3_folder.parent}\n(check config.json)"
        )

    return cfg, problems


def _show_instance_conflict_dialog(other_pid):
    """Modal Yes/No/Close-others dialog shown when another instance of
    this app is already running. Returns "yes", "no", or "close_others".

    Uses a throwaway plain tk.Tk() root (not customtkinter) -- creating
    the real customtkinter root and withdrawing it before this decision
    is made breaks its first deiconify() in this customtkinter version,
    so the real themed root is only ever created once we're committed to
    actually launching.
    """
    probe_root = tk.Tk()
    probe_root.withdraw()
    _apply_icon(probe_root)

    result = {"choice": "no"}
    dlg = tk.Toplevel(probe_root)
    dlg.title("Already running")
    dlg.resizable(False, False)
    dlg.configure(background=PANEL_BG)

    tk.Label(
        dlg,
        text=(
            f"{APP_TITLE} appears to already be running (PID {other_pid}).\n"
            "Running two copies at once risks the same file being processed twice.\n\n"
            "Start another copy anyway?"
        ),
        font=(FONT_FAMILY, 13), fg=TEXT_PRIMARY, bg=PANEL_BG, justify="left",
    ).pack(padx=20, pady=20)

    btn_frame = tk.Frame(dlg, background=PANEL_BG)
    btn_frame.pack(pady=(0, 20), padx=20)

    def pick(choice):
        result["choice"] = choice
        dlg.destroy()

    tk.Button(btn_frame, text="Yes", width=10, bg=ACCENT_RED, fg=PANEL_BG, relief="flat",
              command=lambda: pick("yes")).pack(side="left", padx=4)
    tk.Button(btn_frame, text="No", width=10, bg=PANEL_BG, fg=TEXT_PRIMARY, relief="flat",
              command=lambda: pick("no")).pack(side="left", padx=4)
    tk.Button(btn_frame, text="Close others", width=12, bg=PANEL_BG, fg=ACCENT_TEAL, relief="flat",
              command=lambda: pick("close_others")).pack(side="left", padx=4)

    dlg.protocol("WM_DELETE_WINDOW", lambda: pick("no"))
    dlg.after(60, lambda: _apply_dark_titlebar(dlg))
    dlg.grab_set()
    probe_root.wait_window(dlg)
    probe_root.destroy()
    return result["choice"]


def main():
    other_pid = core.find_running_instance()
    if other_pid is not None:
        choice = _show_instance_conflict_dialog(other_pid)
        if choice == "no":
            return
        if choice == "close_others":
            core.terminate_process(other_pid)
            time.sleep(0.5)
        # "yes" and "close_others" both fall through to launching normally

    core.acquire_lock()
    atexit.register(core.release_lock)

    _cfg, problems = run_startup_checks()

    root = ctk.CTk()
    TkinterDnD._require(root)
    root.title(APP_TITLE)
    _apply_icon(root)

    settings = core.load_settings()
    saved_geometry = settings.get("window_geometry")
    root.geometry(saved_geometry if saved_geometry else f"{DEFAULT_WH[0]}x{DEFAULT_WH[1]}")
    root.minsize(*MIN_WH)

    if problems:
        messagebox.showerror("Startup check", "\n\n".join(problems))

    App(root)

    def on_close():
        try:
            s = core.load_settings()
            s["window_geometry"] = root.geometry()
            core.save_settings(s)
        except Exception:
            pass
        core.release_lock()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(60, lambda: _apply_dark_titlebar(root))
    root.mainloop()


if __name__ == "__main__":
    main()
