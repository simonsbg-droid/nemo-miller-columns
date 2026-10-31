#!/usr/bin/env python3
"""
Nemo Miller Columns - Spaltenansicht wie im macOS Finder
Ursprung: github.com/davemin/linux-nemo-miller-columns (MIT-Lizenz)
Erweiterte Fassung: Tastatursteuerung, Dateioperationen, Drag & Drop, Galerie
Entwickelt durch Claude (KI-Modell von Anthropic) nach Vorgaben und Tests von simonsbg-droid
https://github.com/simonsbg-droid/nemo-miller-columns
"""

import sys
import os
import gi
import shutil
import subprocess
import mimetypes
import urllib.parse
import threading
import queue
import time
import zipfile
import tarfile
import uuid
import json
import re
import unicodedata
import hashlib
import shlex
import tempfile
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Generator

gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('Pango', '1.0')

from gi.repository import Gtk, Gdk, GdkPixbuf, Gio, GLib, Pango

URI_TARGET = "text/uri-list"
GNOME_COPIED = "x-special/gnome-copied-files"
CONFIG_DIR = Path.home() / '.config' / 'nemo-miller-columns'
APP_ICON = "nemo-miller-columns"

TAR_SUFFIXES = ('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz', '.tbz2', '.tar.xz', '.txz')
OTHER_ARCHIVE_SUFFIXES = ('.7z', '.rar', '.tar.zst', '.zst', '.gz', '.bz2', '.xz',
                          '.iso', '.cab', '.arj', '.lzh', '.ace')


class Settings:
    """Gespeichert in ~/.config/nemo-miller-columns/einstellungen.json"""
    FILE = CONFIG_DIR / 'einstellungen.json'
    DEFAULTS = {"font_size": 0,          # 0 = wie System
                "row_density": 38,        # 0 = sehr eng ... 100 = sehr locker
                "emphasis": 50,           # 0 = gleich breit ... 100 = maximal betont
                "show_hidden": False,
                "gallery_auto": True,     # Bildordner automatisch als Galerie
                "thumb_size": 160,        # Kachelgroesse der Galerie in Pixel
                "sort_key": "name",       # Standard-Sortierung: name/mtime/size/type
                "sort_desc": False,
                "dirs_first": True}
    DENSITY_NORMAL = 38
    OLD_SPACING = {"kompakt": 19, "normal": 38, "locker": 69}

    def __init__(self):
        self.data = dict(self.DEFAULTS)
        try:
            loaded = json.loads(self.FILE.read_text(encoding='utf-8'))
            for k in self.DEFAULTS:
                if k in loaded and type(loaded[k]) is type(self.DEFAULTS[k]):
                    self.data[k] = loaded[k]
            # alte Einstellung (drei Knoepfe) uebernehmen
            if "row_density" not in loaded and loaded.get("row_spacing") in self.OLD_SPACING:
                self.data["row_density"] = self.OLD_SPACING[loaded["row_spacing"]]
        except Exception:
            pass
        self.data["row_density"] = max(0, min(100, int(self.data["row_density"])))
        if self.data["sort_key"] not in ("name", "mtime", "size", "type"):
            self.data["sort_key"] = "name"

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self.FILE.write_text(json.dumps(self.data, indent=2), encoding='utf-8')
        except OSError:
            pass

    @staticmethod
    def system_font_size():
        try:
            name = Gtk.Settings.get_default().props.gtk_font_name or ""
            return max(6, int(round(float(name.split()[-1]))))
        except Exception:
            return 10

    def effective_font_size(self):
        return self.data["font_size"] or self.system_font_size()

    def icon_size(self):
        # ganz eng: Symbole schrumpfen bis auf Texthoehe
        v = self.data["row_density"]
        factor = 1.6 + 0.8 * min(1.0, v / 25.0)
        return max(14, min(48, int(round(self.effective_font_size() * factor))))

    def row_margin(self):
        """Abstand ueber und unter jeder Zeile in Pixel: 0 (sehr eng) bis 16 (sehr locker)"""
        return int(round(self.data["row_density"] * 0.16))


def format_size(size):
    """1536 -> '1,5 KB' (deutsche Schreibweise)"""
    if size < 1024:
        return f"{size} Bytes" if size != 1 else "1 Byte"
    for unit in ['KB', 'MB', 'GB', 'TB', 'PB']:
        size /= 1024
        if size < 1024 or unit == 'PB':
            return f"{size:.1f} {unit}".replace('.', ',')


def format_int(n):
    return f"{n:,}".replace(',', '.')


def format_date(dt):
    """GLib.DateTime oder Zeitstempel -> '26.10.2026 17:29'"""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return datetime.fromtimestamp(dt).strftime("%d.%m.%Y %H:%M")
    return dt.to_local().format("%d.%m.%Y %H:%M")


class FolderSizer:
    """Berechnet Ordnergroessen im Hintergrund. Immer nur eine Berechnung,
    eine neue Anfrage bricht die alte ab. Ergebnisse werden zwischengespeichert."""

    def __init__(self):
        self.cache = {}
        self.waiters = {}
        self.current = None
        self.cancel = threading.Event()
        self.jobs = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def key_for(paths):
        return tuple(sorted(str(p) for p in paths))

    def request(self, paths, callback):
        key = self.key_for(paths)
        if key in self.cache:
            callback(key, self.cache[key], True)
            return key
        self.waiters.setdefault(key, []).append(callback)
        if self.current != key:
            self.cancel.set()
            self.jobs.put(key)
        return key

    def invalidate(self, path=None):
        """path=None: alles vergessen; sonst alles, was diesen Ordner enthaelt"""
        if path is None:
            self.cache.clear()
            return
        p = str(path)
        for key in list(self.cache):
            if any(p == k or p.startswith(k.rstrip('/') + '/') for k in key):
                del self.cache[key]

    def _deliver(self, key, result, done):
        cbs = self.waiters.get(key, [])
        if done:
            self.waiters.pop(key, None)
        for cb in list(cbs):
            try:
                cb(key, result, done)
            except Exception:
                pass
        return False

    def _run(self):
        while True:
            key = self.jobs.get()
            while not self.jobs.empty():
                key = self.jobs.get_nowait()
            if key in self.cache:
                GLib.idle_add(self._deliver, key, self.cache[key], True)
                continue
            self.cancel.clear()
            self.current = key
            size = files = dirs = 0
            last = time.monotonic()
            cancelled = False
            stack = []
            for root in key:
                try:
                    st = os.lstat(root)
                except OSError:
                    continue
                if os.path.isdir(root) and not os.path.islink(root):
                    stack.append((root, st.st_dev))
                else:
                    files += 1
                    size += st.st_size
            while stack:
                if self.cancel.is_set():
                    cancelled = True
                    break
                d, dev = stack.pop()
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            try:
                                if e.is_dir(follow_symlinks=False):
                                    est = e.stat(follow_symlinks=False)
                                    if est.st_dev == dev:   # keine anderen Laufwerke mitzaehlen
                                        dirs += 1
                                        stack.append((e.path, dev))
                                else:
                                    files += 1
                                    size += e.stat(follow_symlinks=False).st_size
                            except OSError:
                                pass
                except OSError:
                    pass
                now = time.monotonic()
                if now - last > 0.35:
                    last = now
                    GLib.idle_add(self._deliver, key, (size, files, dirs), False)
            self.current = None
            if not cancelled:
                self.cache[key] = (size, files, dirs)
                GLib.idle_add(self._deliver, key, (size, files, dirs), True)


def describe_size(result, done):
    size, files, dirs = result
    text = format_size(size)
    detail = f"{format_int(files)} Datei{'' if files == 1 else 'en'}"
    if dirs:
        detail += f" in {format_int(dirs)} Ordner{'' if dirs == 1 else 'n'}"
    if not done:
        return f"{text} … (wird noch berechnet)", detail
    return text, detail


def read_start_folder():
    try:
        t = (CONFIG_DIR / 'startordner').read_text(encoding='utf-8').strip()
        if t and Path(os.path.expanduser(t)).is_dir():
            return Path(os.path.expanduser(t))
    except Exception:
        pass
    return Path.home()


def write_start_folder(path):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / 'startordner').write_text(str(path) + "\n", encoding='utf-8')


def archive_kind(path):
    """'zip', 'tar', 'other' (ueber Archivverwaltung) oder None"""
    name = Path(path).name.lower()
    if Path(path).is_dir():
        return None
    if name.endswith('.zip'):
        return 'zip'
    if name.endswith(TAR_SUFFIXES):
        return 'tar'
    if name.endswith(OTHER_ARCHIVE_SUFFIXES) and shutil.which('file-roller'):
        return 'other'
    return None


def archive_stem(path):
    name = Path(path).name
    low = name.lower()
    for suf in sorted(('.zip',) + TAR_SUFFIXES + OTHER_ARCHIVE_SUFFIXES, key=len, reverse=True):
        if low.endswith(suf) and len(name) > len(suf):
            return name[:-len(suf)]
    return Path(name).stem or name


def zip_member_name(info):
    """Dateinamen aus Windows-ZIPs (Umlaute) richtig lesen"""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode('cp437')
    except UnicodeEncodeError:
        return name
    for enc in ('utf-8', 'cp850'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def safe_member_parts(name):
    """Pfad im Archiv in sichere Bestandteile zerlegen (kein ../, keine absoluten Pfade)"""
    parts = []
    for part in name.replace('\\', '/').split('/'):
        if part in ('', '.', '..'):
            continue
        if len(part) == 2 and part[1] == ':':
            continue
        parts.append(part)
    return parts


def path_to_uri(p):
    return Gio.File.new_for_path(str(p)).get_uri()


def uri_to_path(uri):
    uri = uri.strip()
    if not uri or uri.startswith('#'):
        return None
    f = Gio.File.new_for_uri(uri)
    p = f.get_path()
    return Path(p) if p else None


def unique_name(folder, name, copy=True):
    """Liefert einen freien Namen, z. B. 'Bericht (Kopie).pdf'"""
    folder = Path(folder)
    p = Path(name)
    if name.startswith('.') and p.suffix == name:
        stem, ext = name, ''
    else:
        stem, ext = p.stem, p.suffix
        if (folder / name).is_dir():
            stem, ext = name, ''
    n = 1
    while True:
        if copy:
            label = "Kopie" if n == 1 else f"Kopie {n}"
        else:
            label = str(n + 1)
        candidate = folder / f"{stem} ({label}){ext}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        n += 1


SORT_KEYS = (("name", "Name"), ("mtime", "Geändert"), ("size", "Größe"), ("type", "Art"))
SORT_LABELS = dict(SORT_KEYS)
_DIGITS = re.compile(r"(\d+)")


def natural_key(name):
    """Natuerliche Sortierung: 'Foto 2' vor 'Foto 10', Umlaute wie Grundbuchstaben
    (Ä bei A), Gross-/Kleinschreibung egal. Unabhaengig von der Spracheinstellung."""
    base = unicodedata.normalize("NFKD", name)
    base = "".join(c for c in base if not unicodedata.combining(c)).casefold()
    parts = []
    for i, part in enumerate(_DIGITS.split(base)):
        if i % 2:
            parts.append((0, int(part), len(part)))
        elif part:
            parts.append((1, part, 0))
    return (tuple(parts), name)


class FolderSorting:
    """Standard-Sortierung (in den Einstellungen) plus abweichende Sortierung
    einzelner Ordner (~/.config/nemo-miller-columns/sortierung.json)"""

    FILE = CONFIG_DIR / "sortierung.json"

    def __init__(self, settings):
        self.settings = settings
        self.folders = {}
        try:
            data = json.loads(self.FILE.read_text(encoding="utf-8"))
            for k, v in data.items():
                if isinstance(v, dict) and v.get("key") in SORT_LABELS:
                    self.folders[k] = {"key": v["key"], "desc": bool(v.get("desc")),
                                       "dirs_first": bool(v.get("dirs_first", True))}
        except Exception:
            pass

    def _save(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self.FILE.write_text(json.dumps(self.folders, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
        except OSError:
            pass

    def default(self):
        s = self.settings
        return {"key": s["sort_key"], "desc": s["sort_desc"], "dirs_first": s["dirs_first"]}

    def has_own(self, folder):
        return str(folder) in self.folders

    def get(self, folder):
        return dict(self.folders.get(str(folder)) or self.default())

    def set(self, folder, cfg):
        """Aendert die Sortierung dort, wo sie gilt: eigener Ordner oder Standard"""
        if self.has_own(folder):
            self.folders[str(folder)] = dict(cfg)
            self._save()
        else:
            self.settings["sort_key"] = cfg["key"]
            self.settings["sort_desc"] = cfg["desc"]
            self.settings["dirs_first"] = cfg["dirs_first"]

    def set_own(self, folder, own):
        if own:
            self.folders[str(folder)] = self.get(folder)
        else:
            self.folders.pop(str(folder), None)
        self._save()

    def clear_all(self):
        self.folders.clear()
        self._save()

    def describe(self, folder):
        c = self.get(folder)
        text = f"{SORT_LABELS[c['key']]}, {'absteigend' if c['desc'] else 'aufsteigend'}"
        return text + (" (nur dieser Ordner)" if self.has_own(folder) else "")

    def sort(self, items, folder):
        c = self.get(folder)
        items.sort(key=lambda it: natural_key(it.name))
        k = c["key"]
        if k == "name":
            items.reverse() if c["desc"] else None
        elif k == "mtime":
            items.sort(key=lambda it: it.mtime(), reverse=c["desc"])
        elif k == "size":
            items.sort(key=lambda it: it.size_or_count(), reverse=c["desc"])
        elif k == "type":
            items.sort(key=lambda it: it.type_key(), reverse=c["desc"])
        if c["dirs_first"]:
            items.sort(key=lambda it: not it.is_dir)   # stabil: Reihenfolge bleibt


class FileItem:
    """Eine Datei oder ein Ordner"""

    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name or str(self.path)
        self.is_dir = self.path.is_dir()
        self.is_symlink = self.path.is_symlink()
        self._stat = None
        self._count = None

    def _st(self):
        if self._stat is None:
            try:
                self._stat = self.path.stat()
            except OSError:
                try:
                    self._stat = self.path.lstat()
                except OSError:
                    self._stat = False
        return self._stat

    def mtime(self):
        st = self._st()
        return st.st_mtime if st else 0

    def size_or_count(self):
        """Dateien: Groesse in Bytes; Ordner: Anzahl der Elemente (schnell)"""
        if self.is_dir:
            if self._count is None:
                try:
                    self._count = sum(1 for e in os.scandir(self.path)
                                      if ColumnView.show_hidden or not e.name.startswith('.'))
                except OSError:
                    self._count = 0
            return self._count
        st = self._st()
        return st.st_size if st else 0

    def type_key(self):
        if self.is_dir:
            return ""
        suffix = self.path.suffix.lower()
        return suffix[1:] if suffix else "~"

    def icon_name(self, icon_theme):
        if self.is_dir:
            return "folder"
        mime_type, _ = mimetypes.guess_type(str(self.path))
        if mime_type:
            name = mime_type.replace('/', '-')
            if icon_theme.has_icon(name):
                return name
            name = mime_type.split('/')[0] + "-x-generic"
            if icon_theme.has_icon(name):
                return name
        return "text-x-generic"

    def get_icon(self, icon_theme, size=24):
        try:
            return icon_theme.load_icon(self.icon_name(icon_theme), size,
                                        Gtk.IconLookupFlags.FORCE_SIZE)
        except Exception:
            try:
                return icon_theme.load_icon("text-x-generic", size,
                                            Gtk.IconLookupFlags.FORCE_SIZE)
            except Exception:
                return None


# ---------------------------------------------------------------------------
# Zwischenablage (kompatibel mit Nemo: x-special/gnome-copied-files)
# ---------------------------------------------------------------------------

class ClipboardHelper:
    def __init__(self):
        self.widget = Gtk.Invisible()
        self.widget.connect("selection-get", self._on_get)
        self.widget.connect("selection-clear-event", self._on_clear)
        self.mode = None
        self.paths = []
        self.owned = False

    def set(self, mode, paths):
        self.mode = mode
        self.paths = [Path(p) for p in paths]
        clip = Gdk.SELECTION_CLIPBOARD
        Gtk.selection_clear_targets(self.widget, clip)
        for i, t in enumerate([GNOME_COPIED, URI_TARGET, "UTF8_STRING", "text/plain"]):
            Gtk.selection_add_target(self.widget, clip, Gdk.Atom.intern(t, False), i)
        self.owned = Gtk.selection_owner_set(self.widget, clip, Gtk.get_current_event_time())

    def clear(self):
        self.mode = None
        self.paths = []

    def _on_get(self, widget, data, info, time_):
        uris = [path_to_uri(p) for p in self.paths]
        if info == 0:
            text = (self.mode or "copy") + "\n" + "\n".join(uris)
            data.set(Gdk.Atom.intern(GNOME_COPIED, False), 8, text.encode("utf-8"))
        elif info == 1:
            data.set_uris(uris)
        else:
            data.set_text("\n".join(str(p) for p in self.paths), -1)

    def _on_clear(self, widget, event):
        self.owned = False
        return False

    def get(self):
        """Liefert (modus, [pfade]) oder (None, [])"""
        if self.owned and self.paths:
            return self.mode, list(self.paths)
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        try:
            sel = clipboard.wait_for_contents(Gdk.Atom.intern(GNOME_COPIED, False))
            if sel is not None and sel.get_length() > 0:
                lines = bytes(sel.get_data()).decode("utf-8", "ignore").splitlines()
                if lines:
                    mode = "cut" if lines[0].strip() == "cut" else "copy"
                    paths = [p for p in (uri_to_path(u) for u in lines[1:]) if p]
                    if paths:
                        return mode, paths
        except Exception:
            pass
        try:
            uris = clipboard.wait_for_uris()
            if uris:
                paths = [p for p in (uri_to_path(u) for u in uris) if p]
                if paths:
                    return "copy", paths
        except Exception:
            pass
        return None, []


# ---------------------------------------------------------------------------
# Dateioperationen im Hintergrund
# ---------------------------------------------------------------------------

class FileOperations:
    VERBS = {"copy": "Kopiere", "move": "Verschiebe",
             "trash": "Papierkorb", "delete": "Lösche",
             "extract_here": "Entpacke", "extract_folder": "Entpacke"}

    def __init__(self, app):
        self.app = app
        self.jobs = queue.Queue()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def start(self, kind, sources, dest=None, win=None):
        sources = [Path(s) for s in sources]
        if not sources:
            return
        self.jobs.put((kind, sources, Path(dest) if dest else None, win))

    def _idle(self, fn, *args):
        def call():
            fn(*args)
            return False
        GLib.idle_add(call)

    def _status(self, text):
        self._idle(self.app.set_status, text)

    def _ask_conflict(self, win, target):
        ev = threading.Event()
        res = {"choice": "skip", "all": False}

        def show():
            dlg = Gtk.MessageDialog(transient_for=win, modal=True,
                                    message_type=Gtk.MessageType.QUESTION,
                                    buttons=Gtk.ButtonsType.NONE,
                                    text=f"„{target.name}“ ist bereits vorhanden")
            dlg.format_secondary_text(
                f"im Ordner {target.parent}\n\n"
                "„Ersetzen“ verschiebt die vorhandene Version in den Papierkorb.")
            check = Gtk.CheckButton(label="Für alle weiteren Konflikte übernehmen")
            dlg.get_message_area().pack_start(check, False, False, 0)
            check.show()
            dlg.add_button("Überspringen", 1)
            dlg.add_button("Beide behalten", 2)
            dlg.add_button("Ersetzen", 3)
            dlg.set_default_response(2)
            r = dlg.run()
            res["choice"] = {1: "skip", 2: "keep", 3: "replace"}.get(r, "skip")
            res["all"] = check.get_active()
            dlg.destroy()
            ev.set()
            return False

        GLib.idle_add(show)
        ev.wait()
        return res["choice"], res["all"]

    def _run(self):
        while True:
            kind, sources, dest, win = self.jobs.get()
            errors, created = [], []
            policy = None
            total = len(sources)
            for i, src in enumerate(sources, 1):
                verb = self.VERBS.get(kind, kind)
                self._status(f"{verb} {i} von {total}: {src.name}")
                try:
                    if kind in ("copy", "move"):
                        if not (src.exists() or src.is_symlink()):
                            raise FileNotFoundError("nicht mehr vorhanden")
                        if kind == "move" and src.parent == dest:
                            continue
                        if src.is_dir() and not src.is_symlink() and (
                                dest == src or dest.is_relative_to(src)):
                            raise OSError("Ein Ordner kann nicht in sich selbst "
                                          "kopiert oder verschoben werden")
                        target = dest / src.name
                        if kind == "copy" and src.parent == dest:
                            target = unique_name(dest, src.name)
                        elif target.exists() or target.is_symlink():
                            if policy:
                                choice = policy
                            else:
                                choice, apply_all = self._ask_conflict(win, target)
                                if apply_all:
                                    policy = choice
                            if choice == "skip":
                                continue
                            if choice == "keep":
                                target = unique_name(dest, src.name, copy=False)
                            else:
                                Gio.File.new_for_path(str(target)).trash(None)
                        if kind == "copy":
                            if src.is_dir() and not src.is_symlink():
                                shutil.copytree(src, target, symlinks=True)
                            else:
                                shutil.copy2(src, target, follow_symlinks=False)
                        else:
                            shutil.move(str(src), str(target))
                        created.append(target)
                    elif kind in ("extract_here", "extract_folder"):
                        policy = self._extract(kind, src, dest or src.parent, win,
                                               created, policy, i, total)
                    elif kind == "trash":
                        Gio.File.new_for_path(str(src)).trash(None)
                    elif kind == "delete":
                        if src.is_dir() and not src.is_symlink():
                            shutil.rmtree(src)
                        else:
                            src.unlink()
                except GLib.Error as e:
                    errors.append(f"{src.name}: {e.message}")
                except Exception as e:
                    errors.append(f"{src.name}: {e}")
            self._idle(self.app.operation_finished, kind, errors, created, dest, win)

    # --- Entpacken ---------------------------------------------------------

    def _extract(self, kind, src, dest, win, created, policy, i, total):
        akind = archive_kind(src)
        if akind is None:
            raise OSError("kein unterstütztes Archiv")
        stem = archive_stem(src)
        if kind == "extract_folder":
            target = dest / stem
            if target.exists() or target.is_symlink():
                target = unique_name(dest, stem, copy=False)
            tmp = dest / f".entpacken-{uuid.uuid4().hex[:8]}"
            tmp.mkdir()
            try:
                self._unpack(akind, src, tmp, i, total)
                entries = list(tmp.iterdir())
                if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
                    # Archiv hat genau einen Hauptordner: keinen doppelten Ordner anlegen
                    shutil.move(str(entries[0]), str(target))
                else:
                    target.mkdir()
                    for e in entries:
                        shutil.move(str(e), str(target / e.name))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            created.append(target)
            return policy

        # Hier entpacken: erst in einen versteckten Hilfsordner, dann einsortieren
        tmp = dest / f".entpacken-{uuid.uuid4().hex[:8]}"
        tmp.mkdir()
        try:
            self._unpack(akind, src, tmp, i, total)
            for entry in sorted(tmp.iterdir()):
                target = dest / entry.name
                if target.exists() or target.is_symlink():
                    if policy:
                        choice = policy
                    else:
                        choice, apply_all = self._ask_conflict(win, target)
                        if apply_all:
                            policy = choice
                    if choice == "skip":
                        continue
                    if choice == "keep":
                        target = unique_name(dest, entry.name, copy=False)
                    else:
                        Gio.File.new_for_path(str(target)).trash(None)
                shutil.move(str(entry), str(target))
                created.append(target)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return policy

    def _unpack(self, akind, src, target, i, total):
        label = f"Entpacke {i} von {total}: {src.name}" if total > 1 else f"Entpacke {src.name}"
        if akind == 'zip':
            self._unpack_zip(src, target, label)
        elif akind == 'tar':
            self._status(label)
            with tarfile.open(src) as tf:
                try:
                    tf.extractall(target, filter='data')
                except TypeError:
                    for m in tf.getmembers():
                        if m.issym() or m.islnk() or m.isdev():
                            continue
                        parts = safe_member_parts(m.name)
                        if not parts:
                            continue
                        m.name = "/".join(parts)
                        tf.extract(m, target)
        else:
            self._status(label)
            self._unpack_external(src, target)

    def _unpack_zip(self, src, target, label):
        base = target.resolve()
        try:
            zf = zipfile.ZipFile(src)
        except zipfile.BadZipFile:
            raise OSError("Die ZIP-Datei ist beschädigt oder keine ZIP-Datei")
        with zf:
            infos = zf.infolist()
            if any(info.flag_bits & 0x1 for info in infos):
                if shutil.which('file-roller'):
                    self._status(label + " (passwortgeschützt)")
                    self._unpack_external(src, target)
                    return
                raise OSError("Das Archiv ist passwortgeschützt")
            n = len(infos) or 1
            last = 0
            for k, info in enumerate(infos, 1):
                pct = int(k * 100 / n)
                if pct >= last + 5 or k == n:
                    self._status(f"{label} – {pct} %")
                    last = pct
                parts = safe_member_parts(zip_member_name(info))
                if not parts:
                    continue
                out = base.joinpath(*parts)
                if not str(out.resolve()).startswith(str(base) + os.sep):
                    continue
                if info.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fsrc, open(out, 'wb') as fdst:
                    shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
                try:
                    ts = time.mktime(info.date_time + (0, 0, -1))
                    os.utime(out, (ts, ts))
                except (OverflowError, ValueError, OSError):
                    pass

    def _unpack_external(self, src, target):
        """Fuer 7z, rar, passwortgeschuetzte ZIPs usw.: Archivverwaltung (file-roller)"""
        r = subprocess.run(['file-roller', f'--extract-to={target}', '--force', str(src)],
                           capture_output=True, text=True)
        if r.returncode != 0 and not any(target.iterdir()):
            raise OSError("Entpacken abgebrochen oder fehlgeschlagen")


# ---------------------------------------------------------------------------
# Eine Spalte
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Vorschaubilder (gemeinsamer Linux-Speicher ~/.cache/thumbnails, wie Nemo)
# ---------------------------------------------------------------------------

def is_media_name(name):
    mime, _ = mimetypes.guess_type(name)
    return bool(mime) and (mime.startswith(('image/', 'video/')) or mime == 'application/pdf')


def looks_like_picture_folder(path):
    """Bildordner: mindestens 3 Dateien und mindestens 60 % davon Bilder oder Videos"""
    try:
        files = [e.name for e in os.scandir(path)
                 if not e.name.startswith('.') and e.is_file()]
    except OSError:
        return False
    pics = 0
    for n in files:
        mime, _ = mimetypes.guess_type(n)
        if mime and mime.startswith(('image/', 'video/')):
            pics += 1
    return pics >= 3 and pics >= 0.6 * len(files)


class ThumbnailLoader:
    """Liest Vorschaubilder aus dem Speicher und erzeugt fehlende im Hintergrund:
    Bilder selbst, alles andere (Videos, PDFs ...) mit den Vorschau-Werkzeugen des
    Systems (/usr/share/thumbnailers), die auch Nemo verwendet."""

    SIZE = 256

    def __init__(self, workers=2):
        cache = Path(GLib.get_user_cache_dir()) / "thumbnails"
        self.large = cache / "large"
        self.xlarge = cache / "x-large"
        self.failed = set()
        self.jobs = queue.Queue()
        self.thumbnailers = self._read_thumbnailers()
        for _ in range(workers):
            threading.Thread(target=self._run, daemon=True).start()

    # --- System-Werkzeuge ---------------------------------------------------

    @staticmethod
    def _read_thumbnailers():
        found = {}
        dirs = [GLib.get_user_data_dir()] + list(GLib.get_system_data_dirs())
        for d in dirs:
            folder = Path(d) / "thumbnailers"
            if not folder.is_dir():
                continue
            for f in sorted(folder.glob("*.thumbnailer")):
                kf = GLib.KeyFile()
                try:
                    kf.load_from_file(str(f), GLib.KeyFileFlags.NONE)
                    exec_line = kf.get_string("Thumbnailer Entry", "Exec")
                    mimes = kf.get_string_list("Thumbnailer Entry", "MimeType")
                except GLib.Error:
                    continue
                try:
                    try_exec = kf.get_string("Thumbnailer Entry", "TryExec")
                    if try_exec and not shutil.which(try_exec):
                        continue
                except GLib.Error:
                    pass
                try:
                    argv = shlex.split(exec_line)
                except ValueError:
                    continue
                if not argv or not shutil.which(argv[0]):
                    continue
                for m in mimes:
                    if m and m not in found:     # Benutzerordner hat Vorrang
                        found[m] = argv
        return found

    def _run_thumbnailer(self, argv, path, uri):
        tmpdir = tempfile.mkdtemp(prefix="mc-thumb-")
        out = os.path.join(tmpdir, "out.png")
        args = []
        for a in argv:
            a = (a.replace("%%", "\0").replace("%i", path).replace("%u", uri)
                 .replace("%o", out).replace("%s", str(self.SIZE)).replace("\0", "%"))
            args.append(a)
        try:
            subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return GdkPixbuf.Pixbuf.new_from_file_at_scale(out, self.SIZE, self.SIZE, True)
        except Exception:
            pass
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return None

    # --- Laden ----------------------------------------------------------------

    def request(self, path, callback, token):
        """callback(pixbuf oder None) im Hauptprogramm; token.cancelled bricht ab"""
        self.jobs.put((token, str(path), callback))

    def _cached(self, uri, mtime):
        name = hashlib.md5(uri.encode()).hexdigest() + ".png"
        for folder in (self.xlarge, self.large):
            p = folder / name
            if p.exists():
                try:
                    pb = GdkPixbuf.Pixbuf.new_from_file(str(p))
                    if pb.get_option("tEXt::Thumb::MTime") == str(mtime):
                        return pb
                except Exception:
                    pass
        return None

    def _save(self, pb, uri, mtime):
        try:
            self.large.mkdir(parents=True, exist_ok=True, mode=0o700)
            final = self.large / (hashlib.md5(uri.encode()).hexdigest() + ".png")
            tmp = final.with_name(final.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
            pb.savev(str(tmp), "png", ["tEXt::Thumb::URI", "tEXt::Thumb::MTime",
                                       "tEXt::Software"],
                     [uri, str(mtime), "nemo-miller-columns"])
            os.chmod(tmp, 0o600)
            os.replace(tmp, final)
        except Exception:
            pass

    def load(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        mtime = int(st.st_mtime)
        uri = Gio.File.new_for_path(path).get_uri()
        pb = self._cached(uri, mtime)
        if pb is not None:
            return pb
        if (path, mtime) in self.failed:
            return None
        try:
            info = Gio.File.new_for_path(path).query_info(
                "standard::content-type", Gio.FileQueryInfoFlags.NONE, None)
            mime = Gio.content_type_get_mime_type(info.get_content_type()) or ""
        except Exception:
            mime = mimetypes.guess_type(path)[0] or ""
        pb = None
        if mime.startswith("image/"):
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, self.SIZE, self.SIZE, True)
                pb = pb.apply_embedded_orientation() or pb
            except Exception:
                pb = None
        if pb is None and mime in self.thumbnailers:
            pb = self._run_thumbnailer(self.thumbnailers[mime], path, uri)
        if pb is None:
            self.failed.add((path, mtime))
            return None
        self._save(pb, uri, mtime)
        return pb

    def _run(self):
        while True:
            token, path, callback = self.jobs.get()
            if getattr(token, "cancelled", False):
                continue
            pb = self.load(path)
            if not getattr(token, "cancelled", False):
                GLib.idle_add(lambda cb=callback, p=pb: (cb(p), False)[1])


class _Token:
    cancelled = False


class TileBox(Gtk.FlowBox):
    """FlowBox mit denselben Methodennamen wie die Liste einer Spalte"""

    _drag_row = None

    def select_row(self, child):
        self.select_child(child)

    def unselect_row(self, child):
        self.unselect_child(child)

    def get_selected_rows(self):
        return self.get_selected_children()

    def drag_highlight_row(self, child):
        self.drag_unhighlight_row()
        child.drag_highlight()
        self._drag_row = child

    def drag_unhighlight_row(self):
        if self._drag_row is not None:
            try:
                self._drag_row.drag_unhighlight()
            except Exception:
                pass
            self._drag_row = None



class ColumnView(Gtk.Box):
    """Eine Spalte der Ansicht"""

    MIN_WIDTH = 100
    show_hidden = False

    def __init__(self, path, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.path = Path(path)
        self.win = win
        self.icon_theme = Gtk.IconTheme.get_default()
        self._refresh_id = None
        self._drop_dir = None
        self._drop_row = None

        self.set_size_request(self.MIN_WIDTH, -1)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        # Breite bestimmt allein die Spaltenverteilung, nicht GTK
        scroll.set_hexpand(False)
        self.set_hexpand(False)
        self.scroll = scroll

        self.listbox = self._make_view()
        scroll.add(self.listbox)
        self.pack_start(scroll, True, True, 0)

        # Fusszeile mit Ordnerinfos (nur sichtbar, wenn die Spalte als Vorschau rechts steht)
        self.info_label = Gtk.Label()
        self.info_label.set_xalign(0)
        self.info_label.set_line_wrap(True)
        self.info_label.set_max_width_chars(1)
        self.info_label.set_margin_start(10)
        self.info_label.set_margin_end(10)
        self.info_label.set_margin_top(6)
        self.info_label.set_margin_bottom(6)
        self.info_label.get_style_context().add_class("dim-label")
        self.info_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self.pack_end(self.info_label, False, False, 0)
        self.pack_end(self.info_sep, False, False, 0)
        self.info_label.set_no_show_all(True)
        self.info_sep.set_no_show_all(True)
        self._info_key = None
        self._natural = None

        # Klicks auf leere Flaeche
        self.connect("button-press-event", self._on_background_press)

        # Ziel fuer Drag & Drop
        self.drag_dest_set(Gtk.DestDefaults(0),
                           [Gtk.TargetEntry.new(URI_TARGET, 0, 0)],
                           Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        self.connect("drag-motion", self._on_drag_motion)
        self.connect("drag-leave", self._on_drag_leave)
        self.connect("drag-drop", self._on_drag_drop)
        self.connect("drag-data-received", self._on_drag_data_received)

        # Ordner beobachten (z. B. ownCloud-Sync, andere Programme)
        self.monitor = None
        try:
            self.monitor = Gio.File.new_for_path(str(self.path)).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            self.monitor.connect("changed", self._on_dir_changed)
        except Exception:
            self.monitor = None
        self.connect("destroy", self._on_destroy)

        self.populate()

    def _make_view(self):
        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        lb.set_activate_on_single_click(False)
        lb.connect("selected-rows-changed", self._on_selection_changed)
        lb.connect("row-activated", self._on_row_activated)
        lb.connect("keynav-failed", lambda *a: True)
        lb.get_style_context().add_class("miller-column")
        return lb

    # --- Inhalt -----------------------------------------------------------

    def populate(self):
        self._natural = None
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        try:
            items = []
            for entry in self.path.iterdir():
                if entry.name.startswith('.') and not ColumnView.show_hidden:
                    continue
                try:
                    items.append(FileItem(entry))
                except OSError:
                    continue
            self.win.app.sorting.sort(items, self.path)
            for item in items:
                self.listbox.add(self._create_row(item))
        except PermissionError:
            self._add_message("Keine Berechtigung")
        except Exception as e:
            self._add_message(f"Fehler: {e}")
        self.listbox.show_all()

    def _add_message(self, text):
        label = Gtk.Label(label=text)
        label.set_margin_top(20)
        label.set_margin_bottom(20)
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        row.add(label)
        self.listbox.add(row)

    def _create_row(self, item):
        row = Gtk.ListBoxRow()
        row.item = item

        ebox = Gtk.EventBox()
        ebox.set_visible_window(False)
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox.set_margin_start(8)
        hbox.set_margin_end(8)
        settings = self.win.app.settings
        hbox.set_margin_top(settings.row_margin())
        hbox.set_margin_bottom(settings.row_margin())

        icon = item.get_icon(self.icon_theme, settings.icon_size())
        if icon:
            image = Gtk.Image.new_from_pixbuf(icon)
        else:
            image = Gtk.Image.new_from_icon_name("text-x-generic", Gtk.IconSize.LARGE_TOOLBAR)
        hbox.pack_start(image, False, False, 0)

        label = Gtk.Label(label=item.name)
        label.set_xalign(0)
        # in der Mitte kuerzen: Anfang und Endung bleiben sichtbar
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.set_max_width_chars(1)
        label.set_has_tooltip(True)
        label.connect("query-tooltip", self._on_label_tooltip)
        label.set_hexpand(True)
        if item.name.startswith('.'):
            label.set_opacity(0.6)
        hbox.pack_start(label, True, True, 0)

        if item.is_dir:
            arrow = Gtk.Image.new_from_icon_name("go-next-symbolic", Gtk.IconSize.MENU)
            arrow.set_opacity(0.5)
            hbox.pack_end(arrow, False, False, 0)

        ebox.add(hbox)
        row.add(ebox)

        # Drag & Drop Quelle
        ebox.drag_source_set(Gdk.ModifierType.BUTTON1_MASK,
                             [Gtk.TargetEntry.new(URI_TARGET, 0, 0)],
                             Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        ebox.connect("drag-begin", self._on_drag_begin, row)
        ebox.connect("drag-data-get", self._on_drag_data_get, row)
        ebox.connect("button-press-event", self._on_row_press, row)
        return row

    @staticmethod
    def _on_label_tooltip(label, x, y, keyboard, tooltip):
        """Vollen Namen nur zeigen, wenn er gekuerzt ist"""
        layout = label.get_layout()
        if layout is not None and layout.is_ellipsized():
            tooltip.set_text(label.get_text())
            return True
        return False

    def demand(self):
        """Zwischengespeicherte Breite, die der laengste Name braucht"""
        if self._natural is None:
            self._natural = self.natural_width()
        return self._natural

    def set_info_visible(self, visible):
        self.info_label.set_visible(visible)
        self.info_sep.set_visible(visible)
        if not visible:
            self._info_key = None

    def show_info(self):
        """Fusszeile: Anzahl, Groesse (im Hintergrund berechnet), Aenderungsdatum"""
        n = len(self.rows())
        count = f"{format_int(n)} Element{'' if n == 1 else 'e'}"
        try:
            changed = format_date(self.path.stat().st_mtime)
        except OSError:
            changed = None

        def update(key, result, done):
            if key != self._info_key:
                return
            size, detail = describe_size(result, done)
            lines = [f"{count} · {size}", detail]
            if changed:
                lines.append(f"Geändert: {changed}")
            self.info_label.set_text("\n".join(lines))

        self.set_info_visible(True)
        self.info_label.set_text(f"{count} · Größe wird berechnet …")
        self._info_key = FolderSizer.key_for([self.path])
        self.win.app.sizer.request([self.path], update)

    def natural_width(self):
        """Breite, die der laengste Name braucht"""
        settings = self.win.app.settings
        widest = 0
        for row in self.rows():
            layout = self.listbox.create_pango_layout(row.item.name)
            layout.set_font_description(self._font_description())
            widest = max(widest, layout.get_pixel_size()[0])
        extra = 8 + settings.icon_size() + 8 + 8 + 16 + 8 + 16
        return widest + extra

    def _font_description(self):
        ctx = self.listbox.get_style_context()
        return ctx.get_property("font", Gtk.StateFlags.NORMAL)

    # --- Hilfen -----------------------------------------------------------

    def rows(self):
        return [r for r in self.listbox.get_children() if hasattr(r, 'item')]

    def selected_rows(self):
        return [r for r in self.listbox.get_selected_rows() if hasattr(r, 'item')]

    def selected_paths(self):
        return [r.item.path for r in self.selected_rows()]

    def row_for_path(self, path):
        path = Path(path)
        for row in self.rows():
            if row.item.path == path:
                return row
        return None

    def select_path(self, path):
        """Waehlt genau diesen Eintrag aus"""
        row = self.row_for_path(path)
        if row is None:
            return False
        self.listbox.unselect_all()
        self.listbox.select_row(row)
        return True

    def select_paths(self, paths):
        self.listbox.unselect_all()
        found = False
        for p in paths:
            row = self.row_for_path(p)
            if row is not None:
                self.listbox.select_row(row)
                found = True
        return found

    def focused_row(self):
        focus = self.win.get_focus()
        for row in self.rows():
            if focus is row or (focus is not None and focus.is_ancestor(row)):
                return row
        return None

    def contains_focus(self):
        focus = self.win.get_focus()
        return focus is not None and (focus is self.listbox or focus.is_ancestor(self))

    def focus_row(self, row):
        if row is None:
            return
        row.grab_focus()
        alloc = row.get_allocation()
        adj = self.scroll.get_vadjustment()
        if alloc.y < adj.get_value():
            adj.set_value(alloc.y)
        elif alloc.y + alloc.height > adj.get_value() + adj.get_page_size():
            adj.set_value(alloc.y + alloc.height - adj.get_page_size())

    def row_at(self, x, y):
        coords = self.translate_coordinates(self.listbox, int(x), int(y))
        if not coords:
            return None
        if isinstance(coords, tuple) and len(coords) == 3:
            ok, lx, ly = coords
        else:
            lx, ly = coords
        if ly < 0:
            return None
        row = self.listbox.get_row_at_y(int(ly))
        return row if row is not None and hasattr(row, 'item') else None

    # --- Signale ----------------------------------------------------------

    def _on_selection_changed(self, listbox):
        self.win.on_selection_changed(self)

    def _on_row_activated(self, listbox, row):
        if row and hasattr(row, 'item'):
            self.win.on_item_activated(self, row)

    def _on_row_press(self, widget, event, row):
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 3:
            if not row.is_selected():
                self.listbox.unselect_all()
                self.listbox.select_row(row)
            row.grab_focus()
            self.win.show_context_menu(self, event, row)
            return True
        return False

    def _on_background_press(self, widget, event):
        w = Gtk.get_event_widget(event)
        while w is not None and w is not self:
            if isinstance(w, (Gtk.ListBoxRow, Gtk.FlowBoxChild)) and hasattr(w, 'item'):
                return False
            w = w.get_parent()
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if event.button == 1:
            self.listbox.unselect_all()
            self.listbox.grab_focus()
            return True
        if event.button == 3:
            self.listbox.unselect_all()
            self.win.show_context_menu(self, event, None)
            return True
        return False

    def _on_dir_changed(self, monitor, f, other, event_type):
        if event_type in (Gio.FileMonitorEvent.CHANGES_DONE_HINT,
                          Gio.FileMonitorEvent.ATTRIBUTE_CHANGED):
            return
        self.win.app.sizer.invalidate(self.path)
        self.schedule_refresh()

    def schedule_refresh(self, delay=300):
        if self._refresh_id:
            GLib.source_remove(self._refresh_id)

        def do():
            self._refresh_id = None
            self.win.refresh_column(self)
            return False
        self._refresh_id = GLib.timeout_add(delay, do)

    def _on_destroy(self, *args):
        if self._refresh_id:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = None
        if self.monitor:
            self.monitor.cancel()
            self.monitor = None

    # --- Drag & Drop ------------------------------------------------------

    def _drag_paths(self, row):
        if row.is_selected():
            return self.selected_paths()
        return [row.item.path]

    def _on_drag_begin(self, widget, context, row):
        paths = self._drag_paths(row)
        if len(paths) > 1:
            Gtk.drag_set_icon_name(context, "edit-copy", 0, 0)
        else:
            Gtk.drag_set_icon_name(context, row.item.icon_name(self.icon_theme), 0, 0)

    def _on_drag_data_get(self, widget, context, data, info, time_, row):
        data.set_uris([path_to_uri(p) for p in self._drag_paths(row)])

    def _drop_target(self, x, y):
        row = self.row_at(x, y)
        if row is not None and row.item.is_dir:
            return row, row.item.path
        return None, self.path

    def _on_drag_motion(self, widget, context, x, y, time_):
        row, target = self._drop_target(x, y)
        if row is not self._drop_row:
            self.listbox.drag_unhighlight_row()
            self.drag_unhighlight()
            if row is not None:
                self.listbox.drag_highlight_row(row)
            else:
                self.drag_highlight()
            self._drop_row = row
        elif row is None and self._drop_row is None:
            self.drag_highlight()
        Gdk.drag_status(context, self.win.drop_action(context, target), time_)
        return True

    def _on_drag_leave(self, widget, context, time_):
        self.listbox.drag_unhighlight_row()
        self.drag_unhighlight()
        self._drop_row = None

    def _on_drag_drop(self, widget, context, x, y, time_):
        row, target = self._drop_target(x, y)
        self._drop_dir = target
        self._drop_action = self.win.drop_action(context, target)
        self.drag_get_data(context, Gdk.Atom.intern(URI_TARGET, False), time_)
        return True

    def _on_drag_data_received(self, widget, context, x, y, data, info, time_):
        self._on_drag_leave(widget, context, time_)
        uris = data.get_uris() or []
        paths = [p for p in (uri_to_path(u) for u in uris) if p]
        target = self._drop_dir or self.path
        if paths:
            kind = self.win.decide_drop_kind(self._drop_action, paths, target)
            self.win.app.file_ops.start(kind, paths, target, self.win)
        Gtk.drag_finish(context, bool(paths), False, time_)


class GalleryColumn(ColumnView):
    """Galerie: Inhalt eines Ordners als Raster aus Vorschaubildern.
    Verhaelt sich nach aussen wie eine Spalte (Auswahl, Tastatur, Menue, Drag & Drop)."""

    def _make_view(self):
        box = TileBox()
        box.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        box.set_activate_on_single_click(False)
        box.set_homogeneous(True)
        box.set_valign(Gtk.Align.START)
        box.set_min_children_per_line(1)
        box.set_max_children_per_line(200)
        box.set_row_spacing(6)
        box.set_column_spacing(6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.connect("selected-children-changed", self._on_selection_changed)
        box.connect("child-activated", self._on_row_activated)
        box.get_style_context().add_class("gallery")
        return box

    def populate(self):
        tok = getattr(self, "_token", None)
        if tok is not None:
            tok.cancelled = True
        self._token = _Token()
        super().populate()
        self.info_label.set_visible(True)
        self.info_sep.set_visible(True)

    def _on_destroy(self, *args):
        tok = getattr(self, "_token", None)
        if tok is not None:
            tok.cancelled = True
        super()._on_destroy(*args)

    def _add_message(self, text):
        label = Gtk.Label(label=text)
        label.set_margin_top(20)
        child = Gtk.FlowBoxChild()
        child.set_can_focus(False)
        child.add(label)
        self.listbox.add(child)

    def _create_row(self, item):
        size = self.win.app.settings["thumb_size"]
        tile = Gtk.FlowBoxChild()
        tile.item = item
        tile.set_size_request(size + 16, -1)

        ebox = Gtk.EventBox()
        ebox.set_visible_window(False)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vbox.set_margin_top(6)
        vbox.set_margin_bottom(6)
        vbox.set_margin_start(6)
        vbox.set_margin_end(6)

        image = Gtk.Image()
        image.set_size_request(size, size)
        icon = item.get_icon(self.icon_theme, max(32, int(size * 0.5)))
        if icon:
            image.set_from_pixbuf(icon)
        tile.image = image
        vbox.pack_start(image, False, False, 0)

        label = Gtk.Label(label=item.name)
        label.set_justify(Gtk.Justification.CENTER)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_lines(2)
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.set_max_width_chars(1)
        label.set_has_tooltip(True)
        label.connect("query-tooltip", self._on_label_tooltip)
        if item.name.startswith('.'):
            label.set_opacity(0.6)
        vbox.pack_start(label, False, False, 0)

        ebox.add(vbox)
        tile.add(ebox)
        ebox.drag_source_set(Gdk.ModifierType.BUTTON1_MASK,
                             [Gtk.TargetEntry.new(URI_TARGET, 0, 0)],
                             Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        ebox.connect("drag-begin", self._on_drag_begin, tile)
        ebox.connect("drag-data-get", self._on_drag_data_get, tile)
        ebox.connect("button-press-event", self._on_row_press, tile)

        if not item.is_dir:
            token = self._token

            def done(pb, tile=tile, size=size):
                if pb is None or token.cancelled or tile.get_parent() is None:
                    return
                w, h = pb.get_width(), pb.get_height()
                scale = min(size / w, size / h, 1.0)
                pb2 = pb.scale_simple(max(1, int(w * scale)), max(1, int(h * scale)),
                                      GdkPixbuf.InterpType.BILINEAR)
                tile.image.set_from_pixbuf(pb2)
            self.win.app.thumbs.request(item.path, done, token)
        return tile

    def natural_width(self):
        return 0

    def _on_selection_changed(self, box):
        sel = self.selected_rows()
        if len(sel) == 1:
            self._anchor = sel[0]          # Ausgangspunkt fuer Shift+Pfeil
        super()._on_selection_changed(box)

    def per_line(self):
        rows = self.rows()
        if not rows:
            return 1
        y0 = rows[0].get_allocation().y
        return max(1, sum(1 for r in rows if r.get_allocation().y == y0))

    def extend_selection(self, key):
        """Shift+Pfeil im Raster: Auswahl vom Ausgangspunkt bis zur neuen Kachel"""
        rows = self.rows()
        if not rows:
            return
        cur = self.focused_row() or (self.selected_rows()[-1] if self.selected_rows() else rows[0])
        i = rows.index(cur)
        per = self.per_line()
        if key in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            j = i - 1
        elif key in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            j = i + 1
        elif key in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            j = i - per
        elif key in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            j = i + per
        elif key == Gdk.KEY_Home:
            j = 0
        else:
            j = len(rows) - 1
        j = max(0, min(len(rows) - 1, j))
        anchor = getattr(self, "_anchor", None)
        a = rows.index(anchor) if anchor in rows else i
        lo, hi = sorted((a, j))
        self.win._navigating = True
        try:
            self.listbox.unselect_all()
            for r in rows[lo:hi + 1]:
                self.listbox.select_child(r)
        finally:
            self.win._navigating = False
        self._anchor = rows[a]
        self.focus_row(rows[j])
        self.win.on_selection_changed(self)

    def rows(self):
        return [r for r in self.listbox.get_children() if hasattr(r, 'item')]

    def row_at(self, x, y):
        coords = self.translate_coordinates(self.listbox, int(x), int(y))
        if not coords:
            return None
        if isinstance(coords, tuple) and len(coords) == 3:
            ok, lx, ly = coords
        else:
            lx, ly = coords
        child = self.listbox.get_child_at_pos(int(lx), int(ly))
        return child if child is not None and hasattr(child, 'item') else None

    def set_info_visible(self, visible):
        # Die Galerie zeigt ihre Infozeile immer
        self.info_label.set_visible(True)
        self.info_sep.set_visible(True)

    def update_info(self):
        """Infozeile unten: markierte Datei, Mehrfachauswahl oder der Ordner"""
        rows = self.selected_rows()
        self._info_key = None
        if len(rows) == 1 and not rows[0].item.is_dir:
            item = rows[0].item
            parts = [item.name]
            try:
                st = item.path.stat()
                parts.append(format_size(st.st_size))
            except OSError:
                st = None
            mime, _ = mimetypes.guess_type(str(item.path))
            if mime and mime.startswith('image/'):
                try:
                    fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(str(item.path))
                    if fmt:
                        parts.append(f"{w} × {h} Pixel")
                except Exception:
                    pass
            if st:
                parts.append(f"Geändert {format_date(st.st_mtime)}")
            self.info_label.set_text("  ·  ".join(parts))
            return
        if rows:
            paths = [r.item.path for r in rows]
            head = (f"„{rows[0].item.name}“" if len(rows) == 1
                    else f"{len(rows)} Elemente ausgewählt")
        else:
            paths = [self.path]
            n = len(self.rows())
            head = f"{format_int(n)} Element{'' if n == 1 else 'e'}"
        self.info_label.set_text(f"{head}  ·  Größe wird berechnet …")
        key = FolderSizer.key_for(paths)
        self._info_key = key

        def update(k, result, done):
            if k != self._info_key:
                return
            size, detail = describe_size(result, done)
            self.info_label.set_text(f"{head}  ·  {size}  ·  {detail}")
        self.win.app.sizer.request(paths, update)


class ResizeHandle(Gtk.EventBox):
    """Griff zum Verbreitern von Spalten"""

    def __init__(self, on_drag, on_fit=None):
        super().__init__()
        self.on_drag = on_drag
        self.on_fit = on_fit
        self.dragging = False
        self.start_x = 0
        self.add(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.get_style_context().add_class("resize-handle")
        self.set_size_request(6, -1)
        self.set_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                        Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("button-press-event", self._on_button_press)
        self.connect("button-release-event", self._on_button_release)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("enter-notify-event", self._on_enter)
        self.connect("leave-notify-event", self._on_leave)

    def _on_button_press(self, widget, event):
        if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS:
            self.dragging = False
            if self.on_fit:
                self.on_fit(self)
            return True
        if event.button == 1:
            self.dragging = True
            self.start_x = event.x_root
            return True
        return False

    def _on_button_release(self, widget, event):
        self.dragging = False
        return True

    def _on_motion(self, widget, event):
        if self.dragging:
            delta = event.x_root - self.start_x
            self.start_x = event.x_root
            self.on_drag(self, delta)
            return True
        return False

    def _on_enter(self, widget, event):
        window = self.get_window()
        if window:
            window.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "col-resize"))

    def _on_leave(self, widget, event):
        if not self.dragging:
            window = self.get_window()
            if window:
                window.set_cursor(None)


class PreviewColumn(Gtk.ScrolledWindow):
    """Vorschau im Platz rechts neben der Arbeitsspalte"""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_hexpand(False)
        self.get_style_context().add_class("preview-column")
        self.icon_theme = Gtk.IconTheme.get_default()
        self._size_key = None
        self._detail_label = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        self.add(box)

        self.image = Gtk.Image()
        box.pack_start(self.image, False, False, 0)

        self.name_label = Gtk.Label()
        self.name_label.set_line_wrap(True)
        self.name_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.name_label.set_max_width_chars(1)
        self.name_label.set_justify(Gtk.Justification.CENTER)
        self.name_label.set_selectable(True)
        self.name_label.set_can_focus(False)
        self.name_label.get_style_context().add_class("preview-title")
        box.pack_start(self.name_label, False, False, 0)

        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

        self.grid = Gtk.Grid()
        self.grid.set_column_spacing(12)
        self.grid.set_row_spacing(6)
        box.pack_start(self.grid, False, False, 0)
        self._rows = 0
        box.show_all()

    # --- Bausteine ----------------------------------------------------------

    def _clear(self):
        if getattr(self, "_thumb_token", None) is not None:
            self._thumb_token.cancelled = True
        for c in self.grid.get_children():
            self.grid.remove(c)
        self._rows = 0
        self._size_key = None
        self._detail_label = None

    def _row(self, label, value):
        if value in (None, ""):
            return None
        l = Gtk.Label(label=label)
        l.set_xalign(1)
        l.set_yalign(0)
        l.get_style_context().add_class("dim-label")
        v = Gtk.Label(label=value)
        v.set_xalign(0)
        v.set_line_wrap(True)
        v.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        v.set_max_width_chars(1)
        v.set_hexpand(True)
        v.set_selectable(True)
        v.set_can_focus(False)
        self.grid.attach(l, 0, self._rows, 1, 1)
        self.grid.attach(v, 1, self._rows, 1, 1)
        self._rows += 1
        return v

    @staticmethod
    def _thumbnail_path(info):
        try:
            if info and info.has_attribute("thumbnail::path") and \
                    not info.get_attribute_boolean("thumbnail::failed"):
                p = info.get_attribute_byte_string("thumbnail::path")
                if p and os.path.exists(p):
                    return p
        except Exception:
            pass
        return None

    def _set_picture(self, item, info, max_px):
        """Bildvorschau, vorhandenes Vorschaubild (z. B. von Nemo fuer Videos/PDFs) oder Symbol"""
        mime, _ = mimetypes.guess_type(str(item.path))
        sources = []
        if mime and mime.startswith('image/') and not item.is_dir:
            sources.append(str(item.path))
        thumb = self._thumbnail_path(info)
        if thumb:
            sources.append(thumb)
        for src in sources:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(src, max_px, max_px, True)
                self.image.set_from_pixbuf(pb)
                return
            except Exception:
                continue
        icon = item.get_icon(self.icon_theme, 96)
        if icon:
            self.image.set_from_pixbuf(icon)
        else:
            self.image.clear()
        # noch kein Vorschaubild (z. B. PDF, Video): im Hintergrund erzeugen
        if not item.is_dir and is_media_name(item.name):
            token = _Token()
            if getattr(self, "_thumb_token", None) is not None:
                self._thumb_token.cancelled = True
            self._thumb_token = token
            path = item.path

            def done(pb):
                if pb is None or token.cancelled:
                    return
                w, h = pb.get_width(), pb.get_height()
                scale = min(max_px / w, max_px / h, 1.0)
                self.image.set_from_pixbuf(pb.scale_simple(
                    max(1, int(w * scale)), max(1, int(h * scale)), GdkPixbuf.InterpType.BILINEAR))
            self.win.app.thumbs.request(path, done, token)

    def _max_picture(self):
        w = self.get_size_request()[0]
        return max(96, min(320, (w if w > 0 else 260) - 40))

    def _size_rows(self, paths, size_label):
        self._detail_label = self._row("Inhalt:", "…")
        detail = self._detail_label

        def update(key, result, done):
            if key != self._size_key:
                return
            text, more = describe_size(result, done)
            size_label.set_text(text)
            detail.set_text(more)
        self._size_key = FolderSizer.key_for(paths)
        self.win.app.sizer.request(paths, update)

    # --- Anzeigen -------------------------------------------------------------

    def show_file(self, item):
        self._clear()
        try:
            info = Gio.File.new_for_path(str(item.path)).query_info(
                "standard::*,time::*,thumbnail::*,access::*",
                Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
        except Exception:
            info = None
        self._set_picture(item, info, self._max_picture())
        self.name_label.set_markup(f"<b>{GLib.markup_escape_text(item.name)}</b>")

        ctype = info.get_content_type() if info else None
        self._row("Art:", Gio.content_type_get_description(ctype) if ctype else "Datei")
        st = None
        try:
            st = item.path.lstat()
            size = st.st_size
            self._row("Größe:", format_size(size) +
                      (f" ({format_int(size)} Bytes)" if size >= 1024 else ""))
        except OSError:
            pass
        mime, _ = mimetypes.guess_type(str(item.path))
        if mime and mime.startswith('image/'):
            try:
                fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(str(item.path))
                if fmt:
                    self._row("Abmessungen:", f"{w} × {h} Pixel")
            except Exception:
                pass
        if info:
            for label, getter in (("Erstellt:", "get_creation_date_time"),
                                  ("Geändert:", "get_modification_date_time"),
                                  ("Geöffnet:", "get_access_date_time")):
                try:
                    self._row(label, format_date(getattr(info, getter)()))
                except Exception:
                    pass
            if info.has_attribute("access::can-write") and \
                    not info.get_attribute_boolean("access::can-write"):
                self._row("Rechte:", "Nur lesen")
        elif st:
            self._row("Geändert:", format_date(st.st_mtime))
        if item.is_symlink:
            try:
                self._row("Verweist auf:", os.readlink(item.path))
            except OSError:
                pass
        self._row("Ort:", str(item.path.parent))
        self.grid.show_all()

    def show_folder(self, path):
        """Kein Eintrag markiert: Infos zum Ordner selbst"""
        self._clear()
        item = FileItem(path)
        self._set_picture(item, None, 96)
        self.name_label.set_markup(f"<b>{GLib.markup_escape_text(item.name)}</b>")
        self._row("Art:", "Ordner")
        try:
            visible = [e for e in os.scandir(path)
                       if ColumnView.show_hidden or not e.name.startswith('.')]
            nd = sum(1 for e in visible if e.is_dir())
            self._row("Elemente:", f"{format_int(len(visible))} ({format_int(nd)} Ordner, "
                                   f"{format_int(len(visible) - nd)} Dateien)")
        except OSError:
            pass
        size = self._row("Größe:", "wird berechnet …")
        self._size_rows([path], size)
        try:
            self._row("Geändert:", format_date(path.stat().st_mtime))
        except OSError:
            pass
        self._row("Ort:", str(path.parent))
        self.grid.show_all()

    def show_multi(self, paths):
        self._clear()
        try:
            self.image.set_from_pixbuf(self.icon_theme.load_icon(
                "edit-copy", 96, Gtk.IconLookupFlags.FORCE_SIZE))
        except Exception:
            self.image.clear()
        self.name_label.set_markup(f"<b>{len(paths)} Elemente ausgewählt</b>")
        nd = sum(1 for p in paths if p.is_dir())
        self._row("Ordner:", format_int(nd))
        self._row("Dateien:", format_int(len(paths) - nd))
        size = self._row("Gesamtgröße:", "wird berechnet …")
        self._size_rows(paths, size)
        self._row("Ort:", str(paths[0].parent))
        self.grid.show_all()


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    path: Path
    name: str
    is_dir: bool
    match_type: str  # "name" oder "content"


class SearchEngine:
    MAX_FILE_SIZE = 10 * 1024 * 1024

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def search(self, root_path: Path, query: str) -> Generator[SearchResult, None, None]:
        self.cancelled = False
        query_lower = query.lower()
        for dirpath, dirnames, filenames in os.walk(root_path):
            if self.cancelled:
                return
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            current_dir = Path(dirpath)
            for dirname in dirnames:
                if self.cancelled:
                    return
                if query_lower in dirname.lower():
                    yield SearchResult(current_dir / dirname, dirname, True, "name")
            for filename in filenames:
                if self.cancelled:
                    return
                if filename.startswith('.'):
                    continue
                file_path = current_dir / filename
                if query_lower in filename.lower():
                    yield SearchResult(file_path, filename, False, "name")
                    continue
                if self._search_in_content(file_path, query_lower):
                    yield SearchResult(file_path, filename, False, "content")

    def _search_in_content(self, file_path: Path, query: str) -> bool:
        try:
            if file_path.stat().st_size > self.MAX_FILE_SIZE:
                return False
            mime_type, _ = mimetypes.guess_type(str(file_path))
            if not mime_type:
                return False
            text_types = ('text/', 'application/json', 'application/xml',
                          'application/javascript', 'application/x-python',
                          'application/x-sh', 'application/x-perl')
            if not any(mime_type.startswith(t) for t in text_types):
                return False
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return query in f.read().lower()
        except (PermissionError, OSError, UnicodeDecodeError):
            return False


class SearchResultsView(Gtk.Box):
    def __init__(self, on_result_activated):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_result_activated = on_result_activated
        self.icon_theme = Gtk.IconTheme.get_default()

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(12)
        header.set_margin_end(12)
        header.set_margin_top(8)
        header.set_margin_bottom(8)
        self.status_label = Gtk.Label(label="Suche läuft …")
        self.status_label.set_xalign(0)
        header.pack_start(self.status_label, True, True, 0)
        self.spinner = Gtk.Spinner()
        header.pack_start(self.spinner, False, False, 0)
        self.pack_start(header, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-activated", self._on_row_activated)
        self.listbox.get_style_context().add_class("search-results")
        scroll.add(self.listbox)
        self.pack_start(scroll, True, True, 0)
        self.result_count = 0

    def clear(self):
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        self.result_count = 0
        self.status_label.set_text("Suche läuft …")

    def start_search(self):
        self.clear()
        self.spinner.start()

    def stop_search(self):
        self.spinner.stop()
        if self.result_count == 0:
            self.status_label.set_text("Keine Treffer")
        else:
            self.status_label.set_text(f"{self.result_count} Treffer")

    def add_result(self, result: SearchResult):
        self.result_count += 1
        row = Gtk.ListBoxRow()
        row.result = result
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hbox.set_margin_start(12)
        hbox.set_margin_end(12)
        hbox.set_margin_top(6)
        hbox.set_margin_bottom(6)
        item = FileItem(result.path)
        icon = item.get_icon(self.icon_theme, 24)
        image = Gtk.Image.new_from_pixbuf(icon) if icon else \
            Gtk.Image.new_from_icon_name("text-x-generic", Gtk.IconSize.LARGE_TOOLBAR)
        hbox.pack_start(image, False, False, 0)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_label = Gtk.Label(label=result.name)
        name_label.set_xalign(0)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_box.pack_start(name_label, False, False, 0)
        if result.match_type == "content":
            badge = Gtk.Label()
            badge.get_style_context().add_class("dim-label")
            badge.set_markup("<small><i>im Inhalt</i></small>")
            name_box.pack_start(badge, False, False, 0)
        text_box.pack_start(name_box, False, False, 0)
        path_label = Gtk.Label(label=str(result.path.parent))
        path_label.set_xalign(0)
        path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        path_label.get_style_context().add_class("dim-label")
        text_box.pack_start(path_label, False, False, 0)
        hbox.pack_start(text_box, True, True, 0)

        row.add(hbox)
        self.listbox.add(row)
        row.show_all()
        self.status_label.set_text(f"{self.result_count} Treffer …")

    def _on_row_activated(self, listbox, row):
        if row and hasattr(row, 'result'):
            self.on_result_activated(row.result)


# ---------------------------------------------------------------------------
# Spaltenbereich
# ---------------------------------------------------------------------------

class MillerColumnsContainer(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.win = win
        self.columns = []
        self.handles = []
        self.column_widths = []
        self.working = None
        self.get_style_context().add_class("miller-columns-container")
        # Vorschau steht im Platz rechts der Arbeitsspalte, wenn dort kein Ordnerinhalt steht
        self.preview_sep = Gtk.EventBox()
        self.preview_sep.add(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.preview_sep.get_style_context().add_class("resize-handle")
        self.preview_sep.set_size_request(6, -1)
        self.preview = PreviewColumn(win)
        self.preview.set_size_request(240, -1)
        self.pack_end(self.preview, False, True, 0)
        self.pack_end(self.preview_sep, False, True, 0)
        self.preview.set_no_show_all(True)
        self.preview_sep.set_no_show_all(True)
        self.preview_sep.get_child().show()

    def show_preview(self, visible):
        self.preview.set_visible(visible)
        self.preview_sep.set_visible(visible)

    def is_gallery(self):
        return bool(self.columns) and isinstance(self.columns[-1], GalleryColumn)

    def add_column(self, path, gallery=False):
        column = (GalleryColumn if gallery else ColumnView)(path, self.win)
        if self.columns:
            handle = ResizeHandle(self._on_handle_drag, self._on_handle_fit)
            handle.column_index = len(self.columns) - 1
            self.handles.append(handle)
            self.pack_start(handle, False, False, 0)
            handle.show_all()
        self.columns.append(column)
        self.column_widths.append(-1)
        self.pack_start(column, False, True, 0)
        self._distribute_widths()
        column.show_all()
        self.queue_distribute()
        return column

    def remove_columns_after(self, column):
        if column not in self.columns:
            return
        idx = self.columns.index(column)
        while len(self.columns) > idx + 1:
            col = self.columns.pop()
            self.remove(col)
            col.destroy()
            self.column_widths.pop()
            if self.handles:
                handle = self.handles.pop()
                self.remove(handle)
                handle.destroy()
        self.queue_distribute()

    def clear(self):
        for col in self.columns:
            self.remove(col)
            col.destroy()
        for handle in self.handles:
            self.remove(handle)
            handle.destroy()
        self.columns.clear()
        self.handles.clear()
        self.column_widths.clear()
        self.working = None

    AUTO_MIN = 130   # schmalste automatische Breite

    def queue_distribute(self):
        """Breiten neu berechnen, bevor GTK das naechste Bild zeichnet (kein Flackern)"""
        if getattr(self, '_dist_id', None):
            return
        def run():
            self._dist_id = None
            self._distribute_widths()
            return False
        self._dist_id = GLib.idle_add(run, priority=GLib.PRIORITY_HIGH_IDLE)

    def working_index(self):
        n = len(self.columns)
        if self.is_gallery():
            return n - 1
        if self.working in self.columns:
            return self.columns.index(self.working)
        # ohne Auswahl: die letzte Spalte mit Markierung, sonst die letzte
        for i in range(n - 1, -1, -1):
            if self.columns[i].selected_rows():
                return i
        return n - 1

    def slot_count(self):
        """Spalten links + Arbeitsspalte + immer ein Platz rechts davon"""
        if self.is_gallery():
            return len(self.columns)      # die Galerie nutzt den rechten Platz mit
        return max(len(self.columns), self.working_index() + 2)

    def right_slot_width(self, avail):
        """Fester Platz rechts (Ordnerinhalt oder Vorschau): bleibt beim Scrollen gleich"""
        if self.is_gallery():
            return int(max(420, avail * 0.5))
        e = self.win.app.settings["emphasis"] / 100.0
        share = 0.26 - 0.06 * e
        return int(max(self.AUTO_MIN + 110, min(480, avail * share)))

    def weights(self):
        """Arbeitsspalte breit, rechts davon normal, links davon schrittweise schmaler"""
        n = self.slot_count()
        if not len(self.columns):
            return []
        e = self.win.app.settings["emphasis"] / 100.0
        f = self.working_index()
        result = []
        for i in range(n):
            if i == f:
                w = 1.0 + 1.0 * e
            elif i > f:
                w = 1.0 - 0.25 * e   # Vorschau-Platz rechts etwas schmaler
            else:
                w = 1.0 - e * min(0.6, 0.25 * (f - i))
            result.append(w)
        return result

    def _distribute_widths(self):
        """Breite nach Bedarf: jede Spalte bekommt hoechstens, was ihr laengster Name
        braucht; was frei bleibt, geht an Spalten, die sonst kuerzen muessten. Reicht es
        nicht fuer alle, entscheidet der Regler (Arbeitsspalte zuerst)."""
        if not self.columns:
            return False
        scroll = self.win.columns_scroll
        slots = self.slot_count()
        avail = scroll.get_allocated_width() - 6 * (slots - 1) - 2
        if avail <= 50:
            for i, col in enumerate(self.columns):
                fixed = self.column_widths[i] if i < len(self.column_widths) else -1
                col.set_size_request(fixed if fixed != -1 else ColumnView.MIN_WIDTH, -1)
            return False
        f = self.working_index()
        weights = self.weights()
        right = slots - 1                       # Index des Platzes rechts
        right_w = self.right_slot_width(avail)
        left_slots = list(range(right))         # Spalten bis einschliesslich Arbeitsspalte
        fixed = list(self.column_widths) + [-1] * (slots - len(self.column_widths))
        budget = avail - right_w - sum(fixed[i] for i in left_slots if fixed[i] != -1)
        free = [i for i in left_slots if fixed[i] == -1]
        widths = {}
        demand = {i: max(self.AUTO_MIN, self.columns[i].demand()) for i in free}
        while free:
            tw = sum(weights[i] for i in free) or 1
            share = {i: budget * weights[i] / tw for i in free}
            happy = [i for i in free if demand[i] <= share[i]]
            if happy:
                for i in happy:
                    widths[i] = demand[i]
                    budget -= demand[i]
                    free.remove(i)
                continue
            # nicht genug Platz: nach Gewicht teilen, aber nie unter die Mindestbreite
            small = [i for i in free if share[i] < self.AUTO_MIN]
            if small and len(small) < len(free):
                for i in small:
                    widths[i] = self.AUTO_MIN
                    budget -= self.AUTO_MIN
                    free.remove(i)
                continue
            for i in free:
                widths[i] = max(self.AUTO_MIN, int(share[i]))
            budget = 0
            free = []
        final = [fixed[i] if fixed[i] != -1 else int(widths.get(i, self.AUTO_MIN)) for i in left_slots]
        final.append(right_w if (right >= len(self.columns) or fixed[right] == -1) else fixed[right])
        # uebriger Platz: halb an die Arbeitsspalte, halb an den rechten Platz
        used = sum(final)
        if used < avail:
            extra = avail - used
            if f < right and fixed[f] == -1:
                final[f] += extra // 2
                final[right] += extra - extra // 2
            else:
                final[right] += extra
        for i, col in enumerate(self.columns):
            if col.get_size_request()[0] != final[i]:
                col.set_size_request(final[i], -1)
        if self.preview.get_size_request()[0] != final[right]:
            self.preview.set_size_request(final[right], -1)
        return False

    def set_working(self, column):
        if column is not self.working:
            self.working = column
            self.queue_distribute()

    def _on_handle_fit(self, handle):
        """Doppelklick auf Trennlinie: linke Spalte so breit wie der laengste Name"""
        idx = handle.column_index
        if idx < len(self.columns):
            w = self.columns[idx].demand()
            self.column_widths[idx] = max(ColumnView.MIN_WIDTH, min(900, w))
            self._distribute_widths()

    def _on_handle_drag(self, handle, delta):
        idx = handle.column_index
        if idx >= len(self.columns) - 1:
            return
        left_col = self.columns[idx]
        right_col = self.columns[idx + 1]
        left_width = left_col.get_allocation().width
        right_width = right_col.get_allocation().width
        new_left = left_width + delta
        new_right = right_width - delta
        if new_left < ColumnView.MIN_WIDTH:
            delta = ColumnView.MIN_WIDTH - left_width
            new_left = ColumnView.MIN_WIDTH
            new_right = right_width - delta
        if new_right < ColumnView.MIN_WIDTH:
            delta = right_width - ColumnView.MIN_WIDTH
            new_right = ColumnView.MIN_WIDTH
            new_left = left_width + delta
        self.column_widths[idx] = max(ColumnView.MIN_WIDTH, int(new_left))
        self.column_widths[idx + 1] = max(ColumnView.MIN_WIDTH, int(new_right))
        left_col.set_size_request(self.column_widths[idx], -1)
        right_col.set_size_request(self.column_widths[idx + 1], -1)


# ---------------------------------------------------------------------------
# Hauptfenster
# ---------------------------------------------------------------------------

class MillerColumnsWindow(Gtk.ApplicationWindow):

    def __init__(self, app, start_path=None):
        super().__init__(application=app, title="Spaltenansicht")
        self.app = app
        self.set_default_size(1200, 700)
        self._navigating = False
        self.root_path = None
        self.current_path = Path(start_path or self._read_default_start_path())
        self._typeahead = ""
        self._typeahead_time = 0

        self.search_mode = False
        self.search_engine = SearchEngine()
        self.search_thread = None
        self.search_timeout_id = None

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(main_box)
        self._create_toolbar(main_box)


        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.content_stack.set_transition_duration(150)

        self.columns_container = MillerColumnsContainer(self)
        columns_frame = Gtk.Frame()
        self.columns_scroll = Gtk.ScrolledWindow()
        self.columns_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.columns_scroll.add(self.columns_container)
        hadj = self.columns_scroll.get_hadjustment()
        hadj.connect("changed", lambda adj: adj.set_value(adj.get_upper() - adj.get_page_size()))
        self._last_scroll_width = 0
        self.columns_scroll.connect("size-allocate", self._on_columns_resized)
        columns_frame.add(self.columns_scroll)
        self.content_stack.add_named(columns_frame, "columns")

        self.search_results_view = SearchResultsView(self._on_search_result_activated)
        search_frame = Gtk.Frame()
        search_frame.add(self.search_results_view)
        self.content_stack.add_named(search_frame, "search")
        main_box.pack_start(self.content_stack, True, True, 0)


        # Statuszeile fuer laufende Dateioperationen
        self.status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status_box.set_margin_start(8)
        self.status_box.set_margin_end(8)
        self.status_box.set_margin_top(4)
        self.status_box.set_margin_bottom(4)
        self.status_spinner = Gtk.Spinner()
        self.status_label = Gtk.Label()
        self.status_label.set_xalign(0)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.status_box.pack_start(self.status_spinner, False, False, 0)
        self.status_box.pack_start(self.status_label, True, True, 0)
        main_box.pack_end(self.status_box, False, False, 0)

        self._navigate_to(self.current_path)
        self.connect("key-press-event", self._on_key_press)
        self.show_all()
        self.status_box.hide()
        if app.status_text:
            self.set_status(app.status_text)

    @staticmethod
    def _read_default_start_path():
        return read_start_folder()

    # --- Werkzeugleiste ---------------------------------------------------

    def _create_toolbar(self, container):
        toolbar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar_box.set_margin_start(6)
        toolbar_box.set_margin_end(6)
        toolbar_box.set_margin_top(6)
        toolbar_box.set_margin_bottom(6)

        theme = Gtk.IconTheme.get_default()

        def button(icon, tip, cb, label=None, fallbacks=()):
            if not label:
                for name in (icon,) + tuple(fallbacks):
                    if name and theme.has_icon(name):
                        icon = name
                        break
                else:
                    label = tip.split(" (")[0]
            b = Gtk.Button.new_with_label(label) if label else \
                Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            b.set_tooltip_text(tip)
            b.set_focus_on_click(False)
            b.connect("clicked", cb)
            return b

        toolbar_box.pack_start(button("go-previous-symbolic", "Eine Ebene hoch (Rücktaste)",
                                      self._on_go_back), False, False, 0)
        toolbar_box.pack_start(button("go-home-symbolic", "Persönlicher Ordner (Alt+Pos1)",
                                      self._on_go_home), False, False, 0)
        toolbar_box.pack_start(button(None, "Startordner (Esc)", self._on_go_start,
                                      label="Start"), False, False, 0)

        toolbar_box.pack_start(button("folder-new-symbolic", "Neuer Ordner (Strg+Shift+N)",
                                      lambda *_: self.new_folder(),
                                      fallbacks=("folder-new", "list-add-symbolic", "list-add")),
                               False, False, 0)

        self.sort_btn = Gtk.Button()
        self.sort_btn.set_tooltip_text("Sortieren")
        self.sort_btn.set_focus_on_click(False)
        self._sort_icons = {
            False: next((n for n in ("view-sort-ascending-symbolic", "view-sort-ascending")
                         if theme.has_icon(n)), None),
            True: next((n for n in ("view-sort-descending-symbolic", "view-sort-descending")
                        if theme.has_icon(n)), None)}
        if self._sort_icons[False]:
            self.sort_btn.set_image(Gtk.Image.new_from_icon_name(self._sort_icons[False],
                                                                 Gtk.IconSize.BUTTON))
        else:
            self.sort_btn.set_label("Sortieren")
        self.sort_btn.connect("clicked", self._on_sort_btn_clicked)
        toolbar_box.pack_start(self.sort_btn, False, False, 0)

        self.gallery_btn = Gtk.ToggleButton()
        self.gallery_btn.set_tooltip_text("Galerie-Ansicht für diesen Ordner (Strg+G)")
        self.gallery_btn.set_focus_on_click(False)
        gicon = next((n for n in ("view-grid-symbolic", "view-app-grid-symbolic", "view-grid",
                                  "view-list-icons-symbolic", "view-list-icons")
                      if theme.has_icon(n)), None)
        if gicon:
            self.gallery_btn.set_image(Gtk.Image.new_from_icon_name(gicon, Gtk.IconSize.BUTTON))
        else:
            self.gallery_btn.set_label("Galerie")
        self._gallery_handler = self.gallery_btn.connect("toggled", lambda *_: self.toggle_gallery())
        toolbar_box.pack_start(self.gallery_btn, False, False, 0)

        # Pfadleiste: Knoepfe oder (Strg+L) bearbeitbares Feld
        self.path_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.path_bar.get_style_context().add_class("path-bar")
        path_scroll = Gtk.ScrolledWindow()
        path_scroll.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        path_scroll.add(self.path_bar)
        # immer das Ende des Pfads (aktueller Ordner) zeigen
        phadj = path_scroll.get_hadjustment()
        phadj.connect("changed", lambda adj: adj.set_value(adj.get_upper() - adj.get_page_size()))
        path_events = Gtk.EventBox()
        path_events.add(path_scroll)
        path_events.connect("button-press-event", self._on_path_bar_click)

        self.path_entry = Gtk.Entry()
        self.path_entry.set_placeholder_text("Pfad eingeben, Enter zum Öffnen, Esc zum Abbrechen")
        self.path_entry.connect("activate", self._on_path_entry_activate)
        self.path_entry.connect("focus-out-event", self._on_path_entry_focus_out)
        completion = Gtk.EntryCompletion()
        self._path_completion_store = Gtk.ListStore(str)
        completion.set_model(self._path_completion_store)
        completion.set_text_column(0)
        completion.set_inline_completion(True)
        completion.set_popup_single_match(False)
        completion.set_match_func(lambda c, key, it, data: True, None)
        self.path_entry.set_completion(completion)
        self.path_entry.connect("changed", self._on_path_entry_changed)

        self.path_stack = Gtk.Stack()
        self.path_stack.add_named(path_events, "buttons")
        self.path_stack.add_named(self.path_entry, "entry")
        toolbar_box.pack_start(self.path_stack, True, True, 0)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Dateien und Inhalte suchen …")
        self.search_entry.set_width_chars(25)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("stop-search", self._on_search_stopped)
        toolbar_box.pack_start(self.search_entry, False, False, 0)

        menu_btn = Gtk.MenuButton()
        menu_btn.set_tooltip_text("Menü")
        menu_btn.set_focus_on_click(False)
        icon_name = next((n for n in ("open-menu-symbolic", "open-menu", "view-more-symbolic",
                                      "application-menu-symbolic") if theme.has_icon(n)), None)
        if icon_name:
            menu_btn.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON))
        else:
            menu_btn.set_label("☰")
        menu = Gtk.Menu()
        item = Gtk.MenuItem.new_with_label("Einstellungen …")
        item.get_child().set_accel(Gdk.KEY_comma, Gdk.ModifierType.CONTROL_MASK)
        item.connect("activate", lambda *_: self.app.show_settings(self))
        menu.append(item)
        self._hidden_item = Gtk.CheckMenuItem.new_with_label("Versteckte Dateien anzeigen")
        self._hidden_item.get_child().set_accel(Gdk.KEY_h, Gdk.ModifierType.CONTROL_MASK)
        self._hidden_handler = self._hidden_item.connect("toggled", lambda *_: self.toggle_hidden())
        menu.append(self._hidden_item)
        menu.append(Gtk.SeparatorMenuItem())
        for label, key, cb in (("Schrift vergrößern", Gdk.KEY_plus, lambda: self.app.change_font(+1)),
                               ("Schrift verkleinern", Gdk.KEY_minus, lambda: self.app.change_font(-1)),
                               ("Normale Schriftgröße", Gdk.KEY_0, lambda: self.app.change_font(0))):
            it = Gtk.MenuItem.new_with_label(label)
            it.get_child().set_accel(key, Gdk.ModifierType.CONTROL_MASK)
            it.connect("activate", lambda _w, cb=cb: cb())
            menu.append(it)
        menu.append(Gtk.SeparatorMenuItem())
        it = Gtk.MenuItem.new_with_label("Tastenkürzel")
        it.connect("activate", lambda *_: self.show_shortcuts())
        menu.append(it)
        menu.show_all()
        menu.connect("show", lambda *_: self._sync_hidden_item())
        menu_btn.set_popup(menu)
        toolbar_box.pack_end(menu_btn, False, False, 0)

        toolbar_box.pack_end(button("folder-open-symbolic", "Im normalen Nemo öffnen",
                                    self._on_open_in_nemo), False, False, 0)
        toolbar_box.pack_end(button("utilities-terminal-symbolic", "Terminal hier öffnen",
                                    self._on_open_terminal), False, False, 0)
        container.pack_start(toolbar_box, False, False, 0)

    def edit_path(self):
        """Strg+L: Pfadleiste wird zum Eingabefeld"""
        self._path_editing = True
        self.path_entry.set_text(str(self.current_path))
        self.path_stack.set_visible_child_name("entry")
        self.path_entry.grab_focus()
        self.path_entry.select_region(0, -1)

    def stop_path_edit(self, refocus=True):
        if not getattr(self, '_path_editing', False):
            return
        self._path_editing = False
        self.path_stack.set_visible_child_name("buttons")
        if refocus and self.columns:
            GLib.idle_add(self._focus_column, self._active_column() or self.columns[-1])

    def _on_path_bar_click(self, widget, event):
        # Klick auf freie Flaeche der Pfadleiste (nicht auf einen Ordnerknopf)
        if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS:
            w = Gtk.get_event_widget(event)
            while w is not None and w is not widget:
                if isinstance(w, Gtk.Button):
                    return False
                w = w.get_parent()
            self.edit_path()
            return True
        return False

    def _on_path_entry_activate(self, entry):
        text = entry.get_text().strip()
        if not text:
            self.stop_path_edit()
            return
        if text.startswith('file://'):
            text = urllib.parse.unquote(text[7:])
        p = Path(os.path.expanduser(text))
        if not p.is_absolute():
            p = self.current_path / p
        if not p.exists():
            entry.get_style_context().add_class("error")
            self._flash(f"„{text}“ gibt es nicht")
            return
        entry.get_style_context().remove_class("error")
        self._path_editing = False
        self.path_stack.set_visible_child_name("buttons")
        self._navigate_to(p, reset_root=True)

    def _on_path_entry_focus_out(self, entry, event):
        GLib.idle_add(lambda: self.stop_path_edit(refocus=False) and False)
        return False

    def _on_path_entry_changed(self, entry):
        entry.get_style_context().remove_class("error")
        text = os.path.expanduser(entry.get_text())
        store = self._path_completion_store
        store.clear()
        if '/' not in text:
            return
        folder, _, prefix = text.rpartition('/')
        folder = folder or '/'
        try:
            names = sorted((e.name for e in os.scandir(folder)
                            if e.is_dir() and (ColumnView.show_hidden or not e.name.startswith('.'))
                            and e.name.casefold().startswith(prefix.casefold())),
                           key=str.casefold)
        except OSError:
            return
        shown_folder = entry.get_text().rpartition('/')[0]
        for n in names[:200]:
            store.append([f"{shown_folder}/{n}"])

    def _update_title(self):
        name = self.current_path.name or str(self.current_path)
        self.set_title(f"{name} – Spaltenansicht")

    def _update_path_bar(self):
        self._update_title()
        for child in self.path_bar.get_children():
            self.path_bar.remove(child)
        home = Path.home()
        cur = self.current_path
        # Bestandteile: (Beschriftung, Pfad); der persoenliche Ordner wird zum Haus-Symbol
        segments = []
        if cur == home or cur.is_relative_to(home):
            segments.append((None, home))
            base = home
            rel = cur.relative_to(home).parts
        else:
            segments.append(("/", Path("/")))
            base = Path("/")
            rel = cur.parts[1:]
        for part in rel:
            base = base / part
            segments.append((part, base))
        theme = Gtk.IconTheme.get_default()
        for i, (label, path) in enumerate(segments):
            if i > 0:
                sep = Gtk.Label(label="/")
                sep.set_opacity(0.5)
                self.path_bar.pack_start(sep, False, False, 0)
            if label is None:
                icon = next((n for n in ("user-home-symbolic", "go-home-symbolic", "user-home")
                             if theme.has_icon(n)), None)
                btn = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON) if icon \
                    else Gtk.Button(label="~")
                btn.set_tooltip_text(str(path))
            else:
                btn = Gtk.Button()
                lbl = Gtk.Label(label=label)
                # sehr lange Ordnernamen in der Mitte kuerzen, voller Name als Tooltip
                if len(label) > 28:
                    lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                    lbl.set_max_width_chars(24)
                    lbl.set_width_chars(24)
                    btn.set_tooltip_text(label)
                btn.add(lbl)
            btn.get_style_context().add_class("path-button")
            btn.get_style_context().add_class("flat")
            btn.set_focus_on_click(False)
            btn.path = path
            btn.connect("clicked", lambda b: self._navigate_to(b.path, reset_root=True))
            self.path_bar.pack_start(btn, False, False, 0)
        self.path_bar.show_all()

    def set_status(self, text):
        if text:
            self.status_label.set_text(text)
            self.status_box.show()
            self.status_spinner.start()
        else:
            self.status_spinner.stop()
            self.status_box.hide()

    # --- Navigation -------------------------------------------------------

    @property
    def columns(self):
        return self.columns_container.columns

    def _navigate_to(self, path, reset_root=False, focus=True):
        path = Path(os.path.expanduser(str(path))).resolve()
        if not path.exists():
            return
        select_file = None
        if not path.is_dir():
            select_file = path
            path = path.parent
        if reset_root:
            self.root_path = None

        self.columns_container.clear()
        if self.root_path is None or not path.is_relative_to(self.root_path):
            self.root_path = path
        parts = path.relative_to(self.root_path).parts
        current = self.root_path

        self._navigating = True
        try:
            self.columns_container.add_column(current)
            for part in parts:
                next_path = current / part
                if next_path.is_dir():
                    self.columns[-1].select_path(next_path)
                    self.columns_container.add_column(next_path)
                    current = next_path
        finally:
            self._navigating = False

        if self.want_gallery(self.columns[-1].path):
            self._replace_column(self.columns[-1], gallery=True)
        self.current_path = path
        self._update_path_bar()
        if select_file is not None:
            self.columns[-1].select_path(select_file)
        self.update_preview()
        if focus:
            GLib.idle_add(self._focus_column, self.columns[-1])

    # --- Galerie ------------------------------------------------------------

    def want_gallery(self, path):
        key = str(path)
        if key in self.app.gallery_override:
            return self.app.gallery_override[key]
        return self.app.settings["gallery_auto"] and looks_like_picture_folder(path)

    def _replace_column(self, col, gallery):
        """Spalte als Liste oder Galerie neu aufbauen; Auswahl bleibt erhalten"""
        cc = self.columns_container
        idx = self.columns.index(col)
        path, sel = col.path, col.selected_paths()
        self._navigating = True
        try:
            if idx == 0:
                cc.clear()
            else:
                cc.remove_columns_after(self.columns[idx - 1])
            new = cc.add_column(path, gallery=gallery)
            new.select_paths(sel)
        finally:
            self._navigating = False
        return new

    def toggle_gallery(self):
        """Strg+G / Knopf: aktuellen Ordner zwischen Liste und Galerie umschalten"""
        if getattr(self, "_syncing_gallery", False) or not self.columns:
            return
        cc = self.columns_container
        if cc.is_gallery():
            col = self.columns[-1]
            self.app.gallery_override[str(col.path)] = False
            new = self._replace_column(col, gallery=False)
            cc.set_working(new)
            if new.selected_rows():
                self.on_selection_changed(new)
        else:
            col = self.columns[cc.working_index()]
            self.app.gallery_override[str(col.path)] = True
            new = self._replace_column(col, gallery=True)
            cc.set_working(new)
        self.current_path = new.path
        self._update_path_bar()
        self.update_preview()
        GLib.idle_add(self._focus_column, new)

    # --- Sortierung -----------------------------------------------------------

    def _sort_folder(self):
        """Ordner, auf den sich die Sortierung bezieht: der, in dem du gerade arbeitest"""
        col = self._active_column()
        return col.path if col else self.current_path

    def sort_menu(self, folder):
        """Menue mit Kriterium, Richtung, Ordner zuerst und 'nur dieser Ordner'"""
        sorting = self.app.sorting
        cfg = sorting.get(folder)
        menu = Gtk.Menu()
        ctrl_alt = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
        group = None
        for n, (key, label) in enumerate(SORT_KEYS, 1):
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            group = group or item
            item.get_child().set_accel(Gdk.KEY_0 + n, ctrl_alt)
            item.set_active(cfg["key"] == key)
            item.connect("toggled", lambda w, k=key: w.get_active() and self.set_sort(folder, key=k))
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        group = None
        for desc, label in ((False, "Aufsteigend"), (True, "Absteigend")):
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            group = group or item
            item.set_active(cfg["desc"] == desc)
            item.connect("toggled", lambda w, d=desc: w.get_active() and self.set_sort(folder, desc=d))
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        item = Gtk.CheckMenuItem.new_with_label("Ordner zuerst")
        item.set_active(cfg["dirs_first"])
        item.connect("toggled", lambda w: self.set_sort(folder, dirs_first=w.get_active()))
        menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        name = folder.name or str(folder)
        item = Gtk.CheckMenuItem.new_with_label(f"Nur für „{name}“ speichern")
        item.set_active(sorting.has_own(folder))
        item.set_tooltip_text("An: dieser Ordner behält seine eigene Sortierung. "
                              "Aus: er folgt dem Standard für alle Ordner.")
        item.connect("toggled", lambda w: self.set_sort_own(folder, w.get_active()))
        menu.append(item)
        menu.show_all()
        return menu

    def _on_sort_btn_clicked(self, btn):
        menu = self.sort_menu(self._sort_folder())
        menu.attach_to_widget(btn, None)
        menu.popup_at_widget(btn, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST,
                             Gtk.get_current_event())

    def set_sort(self, folder, key=None, desc=None, dirs_first=None):
        sorting = self.app.sorting
        cfg = sorting.get(folder)
        if key is not None:
            cfg["key"] = key
        if desc is not None:
            cfg["desc"] = desc
        if dirs_first is not None:
            cfg["dirs_first"] = dirs_first
        sorting.set(folder, cfg)
        self.app.resort(None if not sorting.has_own(folder) else folder)
        self._flash(f"Sortiert nach {sorting.describe(folder)}")

    def set_sort_own(self, folder, own):
        self.app.sorting.set_own(folder, own)
        self.app.resort(folder)
        self._flash(f"Sortiert nach {self.app.sorting.describe(folder)}")

    def sort_by_key(self, key):
        """Strg+Alt+1..4; dasselbe Kuerzel nochmal dreht die Richtung um"""
        folder = self._sort_folder()
        cfg = self.app.sorting.get(folder)
        if cfg["key"] == key:
            self.set_sort(folder, desc=not cfg["desc"])
        else:
            self.set_sort(folder, key=key, desc=False)

    def _sync_sort_button(self):
        folder = self._sort_folder()
        cfg = self.app.sorting.get(folder)
        icon = self._sort_icons.get(cfg["desc"])
        if icon:
            self.sort_btn.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        self.sort_btn.set_tooltip_text(f"Sortieren – {self.app.sorting.describe(folder)} "
                                       "(Strg+Alt+1 bis 4)")

    def _sync_gallery_button(self):
        self._syncing_gallery = True
        try:
            self.gallery_btn.set_active(self.columns_container.is_gallery())
        finally:
            self._syncing_gallery = False

    def update_preview(self):
        """Platz rechts der Arbeitsspalte fuellen: Ordnerinhalt mit Infozeile oder Vorschau"""
        cc = self.columns_container
        if not self.columns:
            return
        self._sync_gallery_button()
        self._sync_sort_button()
        if cc.is_gallery():
            for c in self.columns[:-1]:
                c.set_info_visible(False)
            cc.show_preview(False)
            self.columns[-1].update_info()
            cc.queue_distribute()
            return
        f = cc.working_index()
        col = self.columns[f]
        for i, c in enumerate(self.columns):
            if i != f + 1:
                c.set_info_visible(False)
        if f + 1 < len(self.columns):
            cc.show_preview(False)
            self.columns[f + 1].show_info()
        else:
            rows = col.selected_rows()
            cc.show_preview(True)
            if len(rows) == 1:
                cc.preview.show_file(rows[0].item)
            elif rows:
                cc.preview.show_multi([r.item.path for r in rows])
            else:
                cc.preview.show_folder(col.path)
        cc.queue_distribute()

    def _focus_column(self, col):
        if col not in self.columns:
            return False
        rows = col.selected_rows()
        row = rows[0] if rows else (col.rows()[0] if col.rows() else None)
        if row is not None:
            col.focus_row(row)
        else:
            col.listbox.grab_focus()
        self._sync_sort_button()
        return False

    def _on_columns_resized(self, widget, alloc):
        if alloc.width != self._last_scroll_width:
            self._last_scroll_width = alloc.width
            self.columns_container.queue_distribute()

    def on_selection_changed(self, column):
        if self._navigating or column not in self.columns:
            return
        if isinstance(column, GalleryColumn):
            self.columns_container.set_working(column)
            rows = column.selected_rows()
            self.current_path = column.path
            self._update_path_bar()
            self.update_preview()
            return
        if (column is self.columns[-1] and column.selected_rows()
                and len(self.columns) > 1 and self.want_gallery(column.path)):
            # in einen Bildordner hineingeklickt: als Galerie zeigen
            new = self._replace_column(column, gallery=True)
            self.columns_container.set_working(new)
            self.current_path = new.path
            self._update_path_bar()
            self.update_preview()
            rows = new.selected_rows()
            if rows:
                GLib.idle_add(lambda: new.focus_row(rows[0]) and False)
            return
        if column.selected_rows():
            self.columns_container.set_working(column)
        rows = column.selected_rows()
        cols = self.columns
        i = cols.index(column)

        if len(rows) == 1:
            item = rows[0].item
            if item.is_dir and i + 1 < len(cols) and cols[i + 1].path == item.path:
                self.current_path = item.path
            else:
                self.columns_container.remove_columns_after(column)
                if item.is_dir:
                    self.columns_container.add_column(item.path)
                    self.current_path = item.path
                else:
                    self.current_path = item.path.parent
        else:
            self.columns_container.remove_columns_after(column)
            self.current_path = column.path
        self._update_path_bar()
        self.update_preview()

    def on_item_activated(self, column, row):
        self._open_items(column, [row])

    @staticmethod
    def _content_type(path):
        try:
            info = Gio.File.new_for_path(str(path)).query_info(
                "standard::content-type", Gio.FileQueryInfoFlags.NONE, None)
            return info.get_content_type()
        except Exception:
            return "application/octet-stream"

    def _launch_with(self, app_info, paths):
        files = [Gio.File.new_for_path(str(p)) for p in paths]
        ctx = self.get_display().get_app_launch_context()
        try:
            if len(files) > 1 and not app_info.supports_files():
                for f in files:
                    app_info.launch([f], ctx)
            else:
                app_info.launch(files, ctx)
        except GLib.Error as e:
            self._error(f"{app_info.get_display_name()} kann nicht gestartet werden", e.message)

    def _apps_for(self, path):
        ctype = self._content_type(path)
        default = Gio.AppInfo.get_default_for_type(ctype, False)
        apps = []
        seen = set()
        for a in ([default] if default else []) + list(Gio.AppInfo.get_recommended_for_type(ctype)):
            if a is None or a.get_id() in seen or not a.should_show():
                continue
            seen.add(a.get_id())
            apps.append(a)
        return ctype, default, apps

    def open_with_dialog(self, paths=None):
        """Strg+O: Programm auswaehlen"""
        if paths is None:
            col = self._active_column()
            paths = col.selected_paths() if col else []
            if not paths and col is not None:
                row = col.focused_row()
                paths = [row.item.path] if row is not None else []
        if not paths:
            return
        first = paths[0]
        ctype = self._content_type(first)
        dlg = Gtk.AppChooserDialog.new(self, Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                                       Gio.File.new_for_path(str(first)))
        dlg.set_title("Öffnen mit")
        n = len(paths)
        dlg.set_heading(f"<b>„{GLib.markup_escape_text(first.name)}“ öffnen mit:</b>" if n == 1
                        else f"<b>{n} Elemente öffnen mit:</b>")
        widget = dlg.get_widget()
        widget.set_show_default(True)
        widget.set_show_recommended(True)
        widget.set_show_fallback(True)
        desc = Gio.content_type_get_description(ctype)
        check = Gtk.CheckButton(label=f"Immer für „{desc}“ verwenden")
        check.set_margin_start(6)
        dlg.get_content_area().pack_end(check, False, False, 6)
        check.show()
        r = dlg.run()
        app = dlg.get_app_info() if r == Gtk.ResponseType.OK else None
        set_default = check.get_active()
        dlg.destroy()
        if app is None:
            return
        if set_default:
            try:
                app.set_as_default_for_type(ctype)
            except GLib.Error as e:
                self._error("Standardprogramm konnte nicht gesetzt werden", e.message)
        self._launch_with(app, paths)

    def _build_open_with_menu(self, paths):
        sub = Gtk.Menu()
        ctype, default, apps = self._apps_for(paths[0])
        for a in apps:
            label = a.get_display_name()
            if default is not None and a.get_id() == default.get_id():
                label += " (Standard)"
            item = Gtk.ImageMenuItem.new_with_label(label)
            icon = a.get_icon()
            if icon is not None:
                item.set_image(Gtk.Image.new_from_gicon(icon, Gtk.IconSize.MENU))
                item.set_always_show_image(True)
            item.connect("activate", lambda _w, app=a: self._launch_with(app, paths))
            sub.append(item)
        if apps:
            sub.append(Gtk.SeparatorMenuItem())
        other = Gtk.MenuItem.new_with_label("Andere Anwendung …")
        other.get_child().set_accel(Gdk.KEY_o, Gdk.ModifierType.CONTROL_MASK)
        other.connect("activate", lambda *_: self.open_with_dialog(paths))
        sub.append(other)
        return sub

    def _open_items(self, column, rows):
        files = [r.item.path for r in rows if not r.item.is_dir]
        dirs = [r for r in rows if r.item.is_dir]
        if not files and len(dirs) == 1:
            if isinstance(column, GalleryColumn):
                self._navigate_to(dirs[0].item.path)
            else:
                self._enter_column(column)
            return
        for p in files[:20]:
            try:
                subprocess.Popen(['xdg-open', str(p)])
            except Exception as e:
                self._error("Datei kann nicht geöffnet werden", str(e))

    def _active_column(self):
        cols = self.columns
        if not cols:
            return None
        for col in cols:
            if col.contains_focus():
                return col
        # leerer aktueller Ordner (kann keinen Fokus bekommen): er ist die aktive Spalte
        last = cols[-1]
        if not last.rows() and last.path == self.current_path:
            return last
        for col in reversed(cols):
            if col.selected_rows():
                return col
        return cols[-1]

    def _enter_column(self, col):
        """Pfeil rechts: in den markierten Ordner hinein"""
        cols = self.columns
        i = cols.index(col)
        rows = col.selected_rows()
        if not rows:
            row = col.focused_row()
            if row is not None:
                col.listbox.select_row(row)
            return
        if len(rows) != 1 or not rows[0].item.is_dir:
            return
        if i + 1 >= len(cols) or cols[i + 1].path != rows[0].item.path:
            return
        nxt = cols[i + 1]
        if not isinstance(nxt, GalleryColumn) and self.want_gallery(nxt.path):
            nxt = self._replace_column(nxt, gallery=True)
            self.columns_container.set_working(nxt)
        first = nxt.rows()
        if not first:
            return
        target = nxt.selected_rows()[0] if nxt.selected_rows() else first[0]
        nxt.listbox.unselect_all()
        nxt.listbox.select_row(target)
        nxt.focus_row(target)

    def _leave_column(self, col):
        """Pfeil links: eine Spalte zurueck"""
        cols = self.columns
        i = cols.index(col)
        if i == 0:
            return
        prev = cols[i - 1]
        self._navigating = True
        try:
            col.listbox.unselect_all()
            self.columns_container.remove_columns_after(col)
        finally:
            self._navigating = False
        self.current_path = col.path
        self._update_path_bar()
        self.columns_container.set_working(prev)
        rows = prev.selected_rows()
        self.update_preview()
        if rows:
            prev.focus_row(rows[0])

    def _on_go_back(self, *args):
        """Eine Ebene hoeher (Ruecktaste): der bisherige Ordner bleibt markiert"""
        col = self._active_column()
        base = col.path if col else self.current_path
        parent = base.parent
        if parent == base:
            return
        self._navigate_to(parent, focus=False)
        target_col = self.columns[-1]
        if target_col.path == parent and target_col.select_path(base):
            GLib.idle_add(lambda: target_col.focus_row(target_col.row_for_path(base)) and False)
        else:
            GLib.idle_add(self._focus_column, target_col)

    def _on_go_start(self, *args):
        self._navigate_to(self._read_default_start_path(), reset_root=True)

    def _on_go_home(self, *args):
        self._navigate_to(Path.home(), reset_root=True)

    def refresh_column(self, col):
        if col not in self.columns:
            return
        if not col.path.is_dir():
            p = col.path
            while not p.is_dir() and p.parent != p:
                p = p.parent
            idx = self.columns.index(col)
            if idx > 0:
                self.refresh_column(self.columns[idx - 1])
            else:
                self._navigate_to(p, reset_root=True)
            return
        old_sel = col.selected_paths()
        had_focus = col.contains_focus()
        focused = col.focused_row()
        focused_path = focused.item.path if focused is not None else None
        vadj = col.scroll.get_vadjustment()
        scroll_pos = vadj.get_value()
        self._navigating = True
        try:
            col.populate()
            col.select_paths(old_sel)
        finally:
            self._navigating = False
        GLib.idle_add(lambda: vadj.set_value(scroll_pos) and False)
        new_sel = col.selected_paths()
        if set(new_sel) != set(old_sel):
            self.on_selection_changed(col)
        else:
            self.update_preview()
        if had_focus:
            row = col.row_for_path(focused_path) if focused_path else None
            if row is None:
                rows = col.selected_rows() or col.rows()
                row = rows[0] if rows else None
            if row is not None:
                row.grab_focus()
            else:
                col.listbox.grab_focus()

    def refresh_all(self):
        for col in list(self.columns):
            self.refresh_column(col)

    # --- Dateioperationen -------------------------------------------------

    def _selected_or_none(self):
        col = self._active_column()
        if col is None:
            return None, []
        return col, col.selected_paths()

    def copy_selection(self, cut=False):
        col, paths = self._selected_or_none()
        if paths:
            self.app.clipboard.set("cut" if cut else "copy", paths)
            n = len(paths)
            what = "Element" if n == 1 else "Elemente"
            self._flash(f"{n} {what} {'ausgeschnitten' if cut else 'kopiert'}")

    def paste(self, target_dir=None):
        if target_dir is None:
            col = self._active_column()
            if col is None:
                return
            target_dir = col.path
        mode, paths = self.app.clipboard.get()
        if not paths:
            self._flash("Zwischenablage ist leer")
            return
        self.app.file_ops.start("move" if mode == "cut" else "copy", paths, target_dir, self)
        if mode == "cut":
            self.app.clipboard.clear()

    def trash_selection(self, permanent=False):
        col, paths = self._selected_or_none()
        if not paths:
            return
        if permanent:
            n = len(paths)
            text = f"„{paths[0].name}“ endgültig löschen?" if n == 1 else f"{n} Elemente endgültig löschen?"
            dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                    message_type=Gtk.MessageType.WARNING,
                                    buttons=Gtk.ButtonsType.NONE, text=text)
            dlg.format_secondary_text("Das kann nicht rückgängig gemacht werden.")
            dlg.add_button("Abbrechen", Gtk.ResponseType.CANCEL)
            dlg.add_button("Endgültig löschen", Gtk.ResponseType.OK)
            dlg.set_default_response(Gtk.ResponseType.CANCEL)
            r = dlg.run()
            dlg.destroy()
            if r != Gtk.ResponseType.OK:
                return
        self._prepare_focus_after_removal(col, paths)
        self.app.file_ops.start("delete" if permanent else "trash", paths, None, self)

    def _prepare_focus_after_removal(self, col, paths):
        """Merkt sich den Nachbarn, damit danach dort weitergearbeitet werden kann"""
        rows = col.rows()
        sel = set(paths)
        idx = [i for i, r in enumerate(rows) if r.item.path in sel]
        if not idx:
            return
        after = [r for r in rows[idx[-1] + 1:] if r.item.path not in sel]
        before = [r for r in rows[:idx[0]] if r.item.path not in sel]
        nxt = after[0] if after else (before[-1] if before else None)
        self._after_removal = (col, nxt.item.path if nxt else None)

    def rename_selection(self):
        col, paths = self._selected_or_none()
        if len(paths) != 1:
            return
        src = paths[0]
        name = self._ask_text("Umbenennen", "Neuer Name:", src.name,
                              select_stem=not src.is_dir())
        if not name or name == src.name:
            return
        if '/' in name or name in ('.', '..'):
            self._error("Ungültiger Name", "Der Name darf keinen Schrägstrich enthalten.")
            return
        target = src.parent / name
        if target.exists() and target != src and name.casefold() != src.name.casefold():
            self._error("Umbenennen nicht möglich", f"„{name}“ ist bereits vorhanden.")
            return
        try:
            os.rename(src, target)
        except OSError as e:
            self._error("Umbenennen nicht möglich", str(e))
            return
        self.refresh_column(col)
        col.select_path(target)
        row = col.row_for_path(target)
        if row is not None:
            col.focus_row(row)

    def new_folder(self, target_dir=None):
        col = self._active_column()
        if target_dir is None:
            if col is None:
                return
            target_dir = col.path
        default = "Neuer Ordner"
        if (target_dir / default).exists():
            default = unique_name(target_dir, default, copy=False).name
        name = self._ask_text("Neuer Ordner", "Name des Ordners:", default)
        if not name:
            return
        if '/' in name or name in ('.', '..'):
            self._error("Ungültiger Name", "Der Name darf keinen Schrägstrich enthalten.")
            return
        target = target_dir / name
        try:
            target.mkdir()
        except OSError as e:
            self._error("Ordner kann nicht erstellt werden", str(e))
            return
        for c in self.columns:
            if c.path == target_dir:
                self.refresh_column(c)
                c.select_path(target)
                row = c.row_for_path(target)
                if row is not None:
                    c.focus_row(row)
                break

    def extract(self, archives, here):
        archives = [Path(a) for a in archives]
        if not archives:
            return
        self.app.file_ops.start("extract_here" if here else "extract_folder",
                                archives, archives[0].parent, self)

    def drop_action(self, context, target):
        keymap = Gdk.Keymap.get_for_display(self.get_display())
        state = keymap.get_modifier_state()
        actions = context.get_actions()
        if state & Gdk.ModifierType.CONTROL_MASK and actions & Gdk.DragAction.COPY:
            return Gdk.DragAction.COPY
        if state & Gdk.ModifierType.SHIFT_MASK and actions & Gdk.DragAction.MOVE:
            return Gdk.DragAction.MOVE
        if actions & Gdk.DragAction.MOVE:
            return Gdk.DragAction.MOVE
        return Gdk.DragAction.COPY

    def decide_drop_kind(self, action, paths, target):
        keymap = Gdk.Keymap.get_for_display(self.get_display())
        state = keymap.get_modifier_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            return "copy"
        if state & Gdk.ModifierType.SHIFT_MASK:
            return "move"
        if action == Gdk.DragAction.COPY:
            return "copy"
        try:
            dev = os.stat(target).st_dev
            if all(os.lstat(p).st_dev == dev for p in paths):
                return "move"
        except OSError:
            pass
        return "copy"

    def after_operation(self, kind, created, dest):
        removal = getattr(self, '_after_removal', None)
        self._after_removal = None
        self.refresh_all()
        if kind in ("extract_here", "extract_folder") and created:
            dest = created[0].parent
        if kind in ("copy", "move", "extract_here", "extract_folder") and created and dest is not None:
            for c in self.columns:
                if c.path == dest:
                    if c.select_paths([p for p in created if p.parent == dest]):
                        rows = c.selected_rows()
                        if rows:
                            c.focus_row(rows[0])
                    break
        elif kind in ("trash", "delete") and removal:
            col, nxt = removal
            if col in self.columns and nxt is not None:
                if col.select_path(nxt):
                    col.focus_row(col.row_for_path(nxt))

    # --- Dialoge ----------------------------------------------------------

    def _ask_text(self, title, label, initial, select_stem=False):
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dlg.add_button("Abbrechen", Gtk.ResponseType.CANCEL)
        dlg.add_button("OK", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        lbl = Gtk.Label(label=label)
        lbl.set_xalign(0)
        box.pack_start(lbl, False, False, 0)
        entry = Gtk.Entry()
        entry.set_text(initial)
        entry.set_activates_default(True)
        entry.set_width_chars(40)
        box.pack_start(entry, False, False, 0)
        dlg.show_all()
        stem_len = len(Path(initial).stem) if select_stem and not initial.startswith('.') else len(initial)
        entry.grab_focus()
        entry.select_region(0, stem_len)
        r = dlg.run()
        text = entry.get_text().strip()
        dlg.destroy()
        return text if r == Gtk.ResponseType.OK else None

    def _error(self, title, text):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.OK, text=title)
        dlg.format_secondary_text(text)
        dlg.run()
        dlg.destroy()

    def _flash(self, text):
        self.status_label.set_text(text)
        self.status_spinner.stop()
        self.status_box.show()

        def hide():
            if not self.app.status_text:
                self.status_box.hide()
            return False
        GLib.timeout_add(2000, hide)

    # --- Kontextmenue -----------------------------------------------------

    def show_context_menu(self, col, event, row):
        menu = Gtk.Menu()
        menu.attach_to_widget(self, None)
        paths = col.selected_paths()
        mode, clip_paths = self.app.clipboard.get()

        def add(label, cb, key=None, mods=0, sensitive=True):
            item = Gtk.MenuItem.new_with_label(label)
            if key:
                item.get_child().set_accel(key, mods)
            item.set_sensitive(sensitive)
            item.connect("activate", lambda *_: cb())
            menu.append(item)

        def sep():
            menu.append(Gtk.SeparatorMenuItem())

        ctrl = Gdk.ModifierType.CONTROL_MASK
        if row is not None:
            add("Öffnen", lambda: self._open_items(col, col.selected_rows()), Gdk.KEY_Return)
            ow = Gtk.MenuItem.new_with_label("Öffnen mit")
            ow.set_submenu(self._build_open_with_menu(paths or [row.item.path]))
            menu.append(ow)
            archives = [p for p in (paths or [row.item.path]) if archive_kind(p)]
            if archives and len(archives) == len(paths or [row.item.path]):
                sep()
                add("Hier entpacken", lambda: self.extract(archives, here=True))
                if len(archives) == 1:
                    add(f"In Ordner „{archive_stem(archives[0])}“ entpacken",
                        lambda: self.extract(archives, here=False))
                else:
                    add("Jeweils in eigenen Ordner entpacken",
                        lambda: self.extract(archives, here=False))
            sep()
            add("Ausschneiden", lambda: self.copy_selection(cut=True), Gdk.KEY_x, ctrl)
            add("Kopieren", lambda: self.copy_selection(), Gdk.KEY_c, ctrl)
            if row.item.is_dir and len(paths) == 1:
                add("In diesen Ordner einfügen", lambda: self.paste(row.item.path),
                    sensitive=bool(clip_paths))
            else:
                add("Einfügen", lambda: self.paste(col.path), Gdk.KEY_v, ctrl,
                    sensitive=bool(clip_paths))
            sep()
            add("Umbenennen …", self.rename_selection, Gdk.KEY_u, ctrl, sensitive=len(paths) == 1)
            add("In den Papierkorb verschieben", lambda: self.trash_selection(), Gdk.KEY_Delete)
            sep()
            add("Neuer Ordner …", lambda: self.new_folder(col.path), Gdk.KEY_n,
                ctrl | Gdk.ModifierType.SHIFT_MASK)
        else:
            add("Neuer Ordner …", lambda: self.new_folder(col.path), Gdk.KEY_n,
                ctrl | Gdk.ModifierType.SHIFT_MASK)
            add("Einfügen", lambda: self.paste(col.path), Gdk.KEY_v, ctrl,
                sensitive=bool(clip_paths))
            sep()
            sort_item = Gtk.MenuItem.new_with_label("Sortieren nach")
            sort_item.set_submenu(self.sort_menu(col.path))
            menu.append(sort_item)
            item = Gtk.CheckMenuItem.new_with_label("Versteckte Dateien anzeigen")
            item.get_child().set_accel(Gdk.KEY_h, ctrl)
            item.set_active(ColumnView.show_hidden)
            item.connect("toggled", lambda *_: self.toggle_hidden())
            menu.append(item)
        sep()
        add("Im normalen Nemo öffnen", lambda: self._open_nemo(col.path))
        add("Terminal hier öffnen", lambda: self._open_terminal(col.path))
        menu.show_all()
        if event is not None:
            menu.popup_at_pointer(event)
        else:
            target = row if row is not None else col
            menu.popup_at_widget(target, Gdk.Gravity.CENTER, Gdk.Gravity.NORTH_WEST, None)

    def _sync_hidden_item(self):
        self._hidden_item.handler_block(self._hidden_handler)
        self._hidden_item.set_active(ColumnView.show_hidden)
        self._hidden_item.handler_unblock(self._hidden_handler)

    def show_shortcuts(self):
        rows = [
            ("Navigation", None),
            ("↑ / ↓", "Eintrag wählen"), ("→ / ←", "In Ordner hinein / eine Spalte zurück"),
            ("Enter", "Öffnen"), ("Pos1 / Ende", "Erster / letzter Eintrag"),
            ("Buchstaben tippen", "Zum Eintrag springen"), ("Rücktaste", "Eine Ebene hoch"),
            ("Esc", "Startordner"), ("Alt+Pos1", "Persönlicher Ordner"),
            ("Strg+L", "Pfad bearbeiten"),
            ("Dateien", None),
            ("Strg+C / X / V", "Kopieren / Ausschneiden / Einfügen"),
            ("Strg+U oder F2", "Umbenennen"), ("Strg+O", "Öffnen mit …"),
            ("Entf", "In den Papierkorb"), ("Shift+Entf", "Endgültig löschen"),
            ("Strg+Shift+N", "Neuer Ordner"), ("Strg+A", "Alles markieren"),
            ("Fenster und Ansicht", None),
            ("Strg+N / Strg+W", "Neues Fenster / schließen"), ("Strg+F", "Suchen"),
            ("F5", "Aktualisieren"), ("Strg+H", "Versteckte Dateien"),
            ("Strg+G", "Galerie-Ansicht ein/aus"),
            ("Strg+Alt+1 / 2 / 3 / 4", "Sortieren nach Name / Geändert / Größe / Art"),
            ("", "nochmal drücken: Richtung umkehren"),
            ("Strg+Plus / Minus / 0", "Schrift größer / kleiner / normal"),
            ("Strg+Komma", "Einstellungen"),
        ]
        dlg = Gtk.Dialog(title="Tastenkürzel", transient_for=self, modal=True)
        dlg.add_button("Schließen", Gtk.ResponseType.CLOSE)
        grid = Gtk.Grid(column_spacing=24, row_spacing=4)
        grid.set_margin_start(18); grid.set_margin_end(18)
        grid.set_margin_top(12); grid.set_margin_bottom(12)
        r = 0
        for k, v in rows:
            if v is None:
                lbl = Gtk.Label()
                lbl.set_markup(f"<b>{k}</b>")
                lbl.set_xalign(0)
                if r:
                    lbl.set_margin_top(10)
                grid.attach(lbl, 0, r, 2, 1)
            else:
                a = Gtk.Label(label=k); a.set_xalign(0)
                b = Gtk.Label(label=v); b.set_xalign(0)
                a.get_style_context().add_class("dim-label")
                grid.attach(a, 0, r, 1, 1); grid.attach(b, 1, r, 1, 1)
            r += 1
        dlg.get_content_area().add(grid)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def toggle_hidden(self):
        ColumnView.show_hidden = not ColumnView.show_hidden
        self.app.settings["show_hidden"] = ColumnView.show_hidden
        for w in self.app.get_windows():
            if isinstance(w, MillerColumnsWindow):
                w.refresh_all()
        if self.app.settings_dialog is not None:
            self.app.settings_dialog.sync()

    # --- Externe Programme ------------------------------------------------

    def _on_open_in_nemo(self, *args):
        col = self._active_column()
        self._open_nemo(col.path if col else self.current_path)

    def _open_nemo(self, path):
        try:
            subprocess.Popen(['nemo', str(path)])
        except Exception as e:
            self._error("Nemo kann nicht gestartet werden", str(e))

    def _on_open_terminal(self, *args):
        col = self._active_column()
        self._open_terminal(col.path if col else self.current_path)

    def _open_terminal(self, path):
        for term in ['x-terminal-emulator', 'gnome-terminal', 'xfce4-terminal', 'konsole', 'xterm']:
            try:
                if term in ('x-terminal-emulator', 'xterm'):
                    subprocess.Popen([term], cwd=str(path))
                else:
                    subprocess.Popen([term, '--working-directory', str(path)])
                return
            except FileNotFoundError:
                continue

    # --- Suche ------------------------------------------------------------

    def _on_search_changed(self, search_entry):
        if self.search_timeout_id:
            GLib.source_remove(self.search_timeout_id)
            self.search_timeout_id = None
        query = search_entry.get_text().strip()
        if not query:
            self._exit_search_mode()
            return
        self.search_timeout_id = GLib.timeout_add(300, self._start_search, query)

    def _on_search_stopped(self, search_entry):
        self._exit_search_mode()
        GLib.idle_add(self._focus_column, self._active_column() or self.columns[-1])

    def _start_search(self, query):
        self.search_timeout_id = None
        if self.search_thread and self.search_thread.is_alive():
            self.search_engine.cancel()
            self.search_thread.join(timeout=0.5)
        self.search_mode = True
        self.content_stack.set_visible_child_name("search")
        self.search_results_view.start_search()
        col = self._active_column()
        root = col.path if col else self.current_path
        self.search_thread = threading.Thread(target=self._search_thread_func,
                                              args=(root, query), daemon=True)
        self.search_thread.start()
        return False

    def _search_thread_func(self, root_path, query):
        try:
            for result in self.search_engine.search(root_path, query):
                if self.search_engine.cancelled:
                    break
                GLib.idle_add(self.search_results_view.add_result, result)
        finally:
            GLib.idle_add(self.search_results_view.stop_search)

    def _exit_search_mode(self):
        if self.search_thread and self.search_thread.is_alive():
            self.search_engine.cancel()
        self.search_mode = False
        self.content_stack.set_visible_child_name("columns")
        if self.search_entry.get_text():
            self.search_entry.set_text("")
        self.search_results_view.clear()

    def _on_search_result_activated(self, result: SearchResult):
        self._exit_search_mode()
        self._navigate_to(result.path, reset_root=True)
        if not result.is_dir:
            try:
                subprocess.Popen(['xdg-open', str(result.path)])
            except Exception as e:
                print(f"Fehler beim Öffnen: {e}")

    # --- Tastatur ---------------------------------------------------------

    def _on_key_press(self, widget, event):
        key = event.keyval
        state = event.state & Gtk.accelerator_get_default_mod_mask()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.MOD1_MASK)
        lower = Gdk.keyval_to_lower(key)
        focus = self.get_focus()
        in_entry = isinstance(focus, Gtk.Entry)

        # Strg+Alt+1..4: Sortierung (auch Ziffernblock)
        if ctrl and alt and not in_entry:
            digit = {Gdk.KEY_1: 0, Gdk.KEY_2: 1, Gdk.KEY_3: 2, Gdk.KEY_4: 3,
                     Gdk.KEY_KP_1: 0, Gdk.KEY_KP_2: 1, Gdk.KEY_KP_3: 2, Gdk.KEY_KP_4: 3,
                     Gdk.KEY_KP_End: 0, Gdk.KEY_KP_Down: 1, Gdk.KEY_KP_Next: 2,
                     Gdk.KEY_KP_Left: 3}.get(key)
            if digit is not None:
                self.sort_by_key(SORT_KEYS[digit][0])
                return True

        # Tasten, die immer gelten
        if ctrl and not alt:
            if lower == Gdk.KEY_n and not shift:
                MillerColumnsWindow(self.app, None).present()
                return True
            if lower == Gdk.KEY_w:
                self.close()
                return True
            if lower == Gdk.KEY_f:
                self.search_entry.grab_focus()
                return True
            if lower == Gdk.KEY_l:
                self.edit_path()
                return True
            if key in (Gdk.KEY_plus, Gdk.KEY_KP_Add, Gdk.KEY_equal):
                self.app.change_font(+1)
                return True
            if key in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
                self.app.change_font(-1)
                return True
            if key in (Gdk.KEY_0, Gdk.KEY_KP_0):
                self.app.change_font(0)
                return True
            if key == Gdk.KEY_comma:
                self.app.show_settings(self)
                return True
            if lower == Gdk.KEY_g and not shift:
                self.toggle_gallery()
                return True

        if key == Gdk.KEY_Escape and getattr(self, '_path_editing', False):
            self.stop_path_edit()
            return True
        if key == Gdk.KEY_Escape:
            if self.search_mode or in_entry:
                self._exit_search_mode()
                GLib.idle_add(self._focus_column, self._active_column() or self.columns[-1])
            else:
                self._on_go_start()
            return True

        if in_entry:
            return False

        if self.search_mode:
            return False

        if key == Gdk.KEY_F5 or (ctrl and lower == Gdk.KEY_r):
            self.app.sizer.invalidate()
            self.refresh_all()
            return True
        if ctrl and lower == Gdk.KEY_h:
            self.toggle_hidden()
            return True
        if alt and key in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
            self._on_go_home()
            return True
        if (alt and key == Gdk.KEY_Up) or key == Gdk.KEY_BackSpace:
            self._on_go_back()
            return True

        col = self._active_column()
        if col is None:
            return False

        if ctrl and shift and lower == Gdk.KEY_n:
            self.new_folder(col.path)
            return True
        if ctrl and lower == Gdk.KEY_a:
            col.listbox.select_all()
            return True
        if ctrl and lower == Gdk.KEY_c:
            self.copy_selection()
            return True
        if ctrl and lower == Gdk.KEY_x:
            self.copy_selection(cut=True)
            return True
        if ctrl and lower == Gdk.KEY_v:
            self.paste(col.path)
            return True
        if key in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            self.trash_selection(permanent=shift)
            return True
        if key == Gdk.KEY_F2 or (ctrl and not shift and lower == Gdk.KEY_u):
            self.rename_selection()
            return True
        if ctrl and not shift and lower == Gdk.KEY_o:
            self.open_with_dialog()
            return True
        if key == Gdk.KEY_Menu or (shift and key == Gdk.KEY_F10):
            row = col.focused_row()
            if row is not None and not row.is_selected():
                col.listbox.unselect_all()
                col.listbox.select_row(row)
            self.show_context_menu(col, None, col.selected_rows()[0] if col.selected_rows() else None)
            return True

        if isinstance(col, GalleryColumn) and shift and not ctrl and key in (
                Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Home,
                Gdk.KEY_End, Gdk.KEY_KP_Left, Gdk.KEY_KP_Right, Gdk.KEY_KP_Up, Gdk.KEY_KP_Down):
            col.extend_selection(key)
            return True
        if isinstance(col, GalleryColumn) and col.contains_focus() and key in (
                Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Home,
                Gdk.KEY_End, Gdk.KEY_Page_Up, Gdk.KEY_Page_Down, Gdk.KEY_KP_Left,
                Gdk.KEY_KP_Right, Gdk.KEY_KP_Up, Gdk.KEY_KP_Down):
            return False      # im Raster bewegt die Galerie selbst
        if not col.contains_focus() and key in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Left,
                                                 Gdk.KEY_Right, Gdk.KEY_Home, Gdk.KEY_End,
                                                 Gdk.KEY_Page_Up, Gdk.KEY_Page_Down):
            self._focus_column(col)
            return True

        if key in (Gdk.KEY_Right, Gdk.KEY_KP_Right) and not ctrl:
            self._enter_column(col)
            return True
        if key in (Gdk.KEY_Left, Gdk.KEY_KP_Left) and not ctrl:
            self._leave_column(col)
            return True
        if key in (Gdk.KEY_Down, Gdk.KEY_Up) and not ctrl and not shift and not col.selected_rows():
            rows = col.rows()
            if rows:
                target = rows[0] if key == Gdk.KEY_Down else rows[-1]
                focused = col.focused_row()
                if focused is not None and key == Gdk.KEY_Down:
                    target = focused
                col.listbox.select_row(target)
                col.focus_row(target)
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            rows = col.selected_rows()
            if not rows:
                row = col.focused_row()
                rows = [row] if row is not None else []
            if rows:
                self._open_items(col, rows)
            return True

        # Tippen springt zum passenden Eintrag
        if not ctrl and not alt:
            ch = Gdk.keyval_to_unicode(key)
            if ch and chr(ch).isprintable() and not chr(ch).isspace():
                self._typeahead_jump(col, chr(ch))
                return True
        return False

    def _typeahead_jump(self, col, ch):
        now = time.monotonic()
        if now - self._typeahead_time > 1.0:
            self._typeahead = ""
        self._typeahead_time = now
        self._typeahead += ch.casefold()
        rows = col.rows()
        if not rows:
            return
        prefix = self._typeahead
        current = col.focused_row()
        start = rows.index(current) if current in rows else -1
        if len(set(prefix)) == 1:
            # gleicher Buchstabe mehrfach: durch passende Eintraege blaettern
            order = rows[start + 1:] + rows[:start + 1]
            matches = [r for r in order if r.item.name.casefold().startswith(prefix[0])]
        else:
            matches = [r for r in rows if r.item.name.casefold().startswith(prefix)]
        if matches:
            col.listbox.unselect_all()
            col.listbox.select_row(matches[0])
            col.focus_row(matches[0])


class SettingsDialog(Gtk.Window):
    """Einstellungen: wirken sofort, werden sofort gespeichert"""

    def __init__(self, app, parent):
        super().__init__(title="Einstellungen – Spaltenansicht")
        self.app = app
        self.set_transient_for(parent)
        self.set_destroy_with_parent(False)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_default_size(460, -1)
        self.set_resizable(False)
        self._syncing = False
        s = app.settings

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(20); box.set_margin_end(20)
        box.set_margin_top(16); box.set_margin_bottom(16)
        self.add(box)

        def heading(text, first=False):
            l = Gtk.Label()
            l.set_markup(f"<b>{text}</b>")
            l.set_xalign(0)
            if not first:
                l.set_margin_top(14)
            box.pack_start(l, False, False, 0)

        def row(label, widget, hint=None):
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            h.set_margin_start(12)
            l = Gtk.Label(label=label)
            l.set_xalign(0)
            h.pack_start(l, True, True, 0)
            h.pack_end(widget, False, False, 0)
            box.pack_start(h, False, False, 0)
            if hint:
                d = Gtk.Label(label=hint)
                d.set_xalign(0)
                d.set_margin_start(12)
                d.set_line_wrap(True)
                d.set_max_width_chars(55)
                d.get_style_context().add_class("dim-label")
                box.pack_start(d, False, False, 0)

        # Darstellung
        heading("Darstellung", first=True)
        fbox = Gtk.Box(spacing=8)
        self.system_font = Gtk.CheckButton(label="Wie System")
        self.font_spin = Gtk.SpinButton.new_with_range(6, 28, 1)
        self.font_spin.set_width_chars(3)
        pt = Gtk.Label(label="pt")
        fbox.pack_start(self.system_font, False, False, 0)
        fbox.pack_start(self.font_spin, False, False, 0)
        fbox.pack_start(pt, False, False, 0)
        row("Schriftgröße", fbox, "Auch mit Strg+Plus / Strg+Minus, Strg+0 setzt zurück.")
        self.system_font.connect("toggled", self._on_font_changed)
        self.font_spin.connect("value-changed", self._on_font_changed)

        zl = Gtk.Label(label="Zeilenabstand")
        zl.set_xalign(0)
        zl.set_margin_start(12)
        box.pack_start(zl, False, False, 0)
        self.density = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.density.set_draw_value(False)
        self.density.set_hexpand(True)
        self.density.add_mark(Settings.DENSITY_NORMAL, Gtk.PositionType.BOTTOM, "Normal")
        self.density.connect("value-changed", self._on_density_changed)
        self._density_id = None
        dc = Gtk.Box(spacing=10)
        dc.set_margin_start(12)
        da = Gtk.Label(label="Sehr eng")
        db = Gtk.Label(label="Sehr locker")
        da.get_style_context().add_class("dim-label")
        db.get_style_context().add_class("dim-label")
        dc.pack_start(da, False, False, 0)
        dc.pack_start(self.density, True, True, 0)
        dc.pack_start(db, False, False, 0)
        box.pack_start(dc, False, False, 0)

        self.hidden_switch = Gtk.Switch()
        self.hidden_switch.connect("notify::active", self._on_hidden_changed)
        row("Versteckte Dateien anzeigen", self.hidden_switch)

        # Spalten
        heading("Spaltenbreite")
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.add_mark(50, Gtk.PositionType.BOTTOM, None)
        self.scale.connect("value-changed", self._on_emphasis_changed)
        sc = Gtk.Box(spacing=10)
        sc.set_margin_start(12)
        a = Gtk.Label(label="Gleich breit")
        b = Gtk.Label(label="Maximal betont")
        a.get_style_context().add_class("dim-label")
        b.get_style_context().add_class("dim-label")
        sc.pack_start(a, False, False, 0)
        sc.pack_start(self.scale, True, True, 0)
        sc.pack_start(b, False, False, 0)
        box.pack_start(sc, False, False, 0)
        hint = Gtk.Label(label="Betont: Die Spalte, in der du arbeitest, wird breiter, "
                               "Spalten weiter links schmaler. Doppelklick auf eine Trennlinie "
                               "passt eine Spalte an den längsten Namen an.")
        hint.set_xalign(0); hint.set_margin_start(12)
        hint.set_line_wrap(True); hint.set_max_width_chars(55)
        hint.get_style_context().add_class("dim-label")
        box.pack_start(hint, False, False, 0)

        # Galerie
        heading("Galerie")
        self.gallery_switch = Gtk.Switch()
        self.gallery_switch.connect("notify::active", self._on_gallery_auto)
        row("Bildordner automatisch als Galerie", self.gallery_switch,
            "Ordner, die überwiegend Bilder oder Videos enthalten. Umschalten jederzeit "
            "mit dem Galerie-Knopf oder Strg+G.")
        self.thumb_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 96, 256, 8)
        self.thumb_scale.set_draw_value(False)
        self.thumb_scale.set_hexpand(True)
        self.thumb_scale.add_mark(160, Gtk.PositionType.BOTTOM, None)
        self.thumb_scale.connect("value-changed", self._on_thumb_size)
        self._thumb_id = None
        tc = Gtk.Box(spacing=10)
        tc.set_margin_start(12)
        ta = Gtk.Label(label="Kleine Bilder")
        tb = Gtk.Label(label="Große Bilder")
        ta.get_style_context().add_class("dim-label")
        tb.get_style_context().add_class("dim-label")
        tc.pack_start(ta, False, False, 0)
        tc.pack_start(self.thumb_scale, True, True, 0)
        tc.pack_start(tb, False, False, 0)
        box.pack_start(tc, False, False, 0)

        # Sortierung
        heading("Sortierung (Standard für alle Ordner)")
        self.sort_combo = Gtk.ComboBoxText()
        for key, label in SORT_KEYS:
            self.sort_combo.append(key, label)
        self.sort_combo.connect("changed", self._on_sort_default)
        row("Sortieren nach", self.sort_combo)
        self.sort_desc_switch = Gtk.Switch()
        self.sort_desc_switch.connect("notify::active", self._on_sort_default)
        row("Absteigend", self.sort_desc_switch)
        self.dirs_first_switch = Gtk.Switch()
        self.dirs_first_switch.connect("notify::active", self._on_sort_default)
        row("Ordner zuerst", self.dirs_first_switch)
        self.sort_reset = Gtk.Button()
        self.sort_reset.connect("clicked", self._on_sort_reset)
        row("Ordner mit eigener Sortierung", self.sort_reset,
            "Einzelne Ordner lassen sich über den Sortieren-Knopf abweichend speichern.")

        # Allgemein
        heading("Allgemein")
        self.folder_btn = Gtk.FileChooserButton.new("Startordner wählen", Gtk.FileChooserAction.SELECT_FOLDER)
        self.folder_btn.set_width_chars(22)
        self.folder_btn.set_filename(str(read_start_folder()))
        self.folder_btn.connect("file-set", self._on_start_folder)
        row("Startordner", self.folder_btn, "Wird beim Start und mit „Start“ bzw. Esc ganz links geöffnet.")

        close = Gtk.Button.new_with_label("Schließen")
        close.connect("clicked", lambda *_: self.destroy())
        bb = Gtk.Box()
        bb.set_margin_top(16)
        bb.pack_end(close, False, False, 0)
        box.pack_start(bb, False, False, 0)

        self.connect("key-press-event", self._on_key)
        self.sync()
        self.show_all()

    def sync(self):
        s = self.app.settings
        self._syncing = True
        try:
            self.system_font.set_active(s["font_size"] == 0)
            self.font_spin.set_value(s.effective_font_size())
            self.font_spin.set_sensitive(s["font_size"] != 0)
            self.density.set_value(s["row_density"])
            self.hidden_switch.set_active(s["show_hidden"])
            self.scale.set_value(s["emphasis"])
            self.gallery_switch.set_active(s["gallery_auto"])
            self.thumb_scale.set_value(s["thumb_size"])
            self.sort_combo.set_active_id(s["sort_key"])
            self.sort_desc_switch.set_active(s["sort_desc"])
            self.dirs_first_switch.set_active(s["dirs_first"])
            n = len(self.app.sorting.folders)
            self.sort_reset.set_label(f"{n} zurücksetzen" if n else "keine")
            self.sort_reset.set_sensitive(n > 0)
        finally:
            self._syncing = False

    def _on_key(self, w, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def _on_font_changed(self, *args):
        if self._syncing:
            return
        s = self.app.settings
        if self.system_font.get_active():
            s["font_size"] = 0
        else:
            s["font_size"] = int(self.font_spin.get_value())
        self.font_spin.set_sensitive(not self.system_font.get_active())
        self.app.apply_appearance()

    def _on_density_changed(self, scale):
        if self._syncing:
            return
        # beim Ziehen nicht bei jedem Pixel neu aufbauen
        if self._density_id:
            GLib.source_remove(self._density_id)

        def apply():
            self._density_id = None
            self.app.settings["row_density"] = int(round(self.density.get_value()))
            self.app.apply_appearance()
            return False
        self._density_id = GLib.timeout_add(120, apply)

    def _on_hidden_changed(self, sw, _p):
        if self._syncing:
            return
        self.app.settings["show_hidden"] = sw.get_active()
        self.app.apply_appearance()

    def _on_sort_default(self, *args):
        if self._syncing:
            return
        s = self.app.settings
        s["sort_key"] = self.sort_combo.get_active_id() or "name"
        s["sort_desc"] = self.sort_desc_switch.get_active()
        s["dirs_first"] = self.dirs_first_switch.get_active()
        self.app.resort(None)

    def _on_sort_reset(self, *args):
        self.app.sorting.clear_all()
        self.app.resort(None)
        for w in self.app.windows():
            w.refresh_all()
        self.sync()

    def _on_gallery_auto(self, sw, _p):
        if self._syncing:
            return
        self.app.settings["gallery_auto"] = sw.get_active()

    def _on_thumb_size(self, scale):
        if self._syncing:
            return
        if self._thumb_id:
            GLib.source_remove(self._thumb_id)

        def apply():
            self._thumb_id = None
            self.app.settings["thumb_size"] = int(round(self.thumb_scale.get_value()))
            for w in self.app.windows():
                if w.columns_container.is_gallery():
                    w.refresh_column(w.columns[-1])
            return False
        self._thumb_id = GLib.timeout_add(150, apply)

    def _on_emphasis_changed(self, scale):
        if self._syncing:
            return
        self.app.settings["emphasis"] = int(round(scale.get_value()))
        self.app.apply_widths()

    def _on_start_folder(self, btn):
        f = btn.get_filename()
        if f and Path(f).is_dir():
            write_start_folder(f)


class MillerColumnsApp(Gtk.Application):

    def __init__(self, start_path=None):
        super().__init__(application_id="org.nemo.miller-columns",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.start_path = start_path
        self.status_text = ""
        self.clipboard = None
        self.file_ops = None
        self.settings = None
        self.font_provider = None
        self.settings_dialog = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.settings = Settings()
        ColumnView.show_hidden = self.settings["show_hidden"]
        self._setup_css()
        self.font_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self.font_provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self._update_font_css()
        self.clipboard = ClipboardHelper()
        self.file_ops = FileOperations(self)
        self.sizer = FolderSizer()
        self.thumbs = ThumbnailLoader()
        self.gallery_override = {}
        self.sorting = FolderSorting(self.settings)

    def _setup_css(self):
        css = b"""
        .miller-columns-container { background-color: @theme_base_color; }
        .miller-column { background-color: @theme_base_color; }
        .miller-column row { padding: 0; min-height: 0; }
        .miller-column row:selected {
            background-color: @theme_selected_bg_color;
            color: @theme_selected_fg_color;
        }
        .preview-column, .preview-column viewport { background-color: @theme_base_color; }
        .preview-title { font-size: 14px; }
        .path-bar { padding: 4px 8px; background-color: @theme_bg_color; }
        .path-button { padding: 2px 6px; min-height: 24px; }
        .resize-handle { background-color: @borders; min-width: 6px; }
        .resize-handle:hover { background-color: @theme_selected_bg_color; }
        .gallery { background-color: @theme_base_color; }
        .gallery flowboxchild { border-radius: 6px; padding: 0; }
        .gallery flowboxchild:selected {
            background-color: @theme_selected_bg_color;
            color: @theme_selected_fg_color;
        }
        .search-results { background-color: @theme_base_color; }
        .search-results row { padding: 4px; }
        .search-results row:selected {
            background-color: @theme_selected_bg_color;
            color: @theme_selected_fg_color;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = MillerColumnsWindow(self, self.start_path)
        win.present()

    # --- Einstellungen ------------------------------------------------------

    def windows(self):
        return [w for w in self.get_windows() if isinstance(w, MillerColumnsWindow)]

    def _update_font_css(self):
        size = self.settings["font_size"]
        css = (f".miller-column, .search-results {{ font-size: {size}pt; }}" if size else "")
        self.font_provider.load_from_data(css.encode())

    def apply_appearance(self):
        """Schrift, Zeilenabstand oder versteckte Dateien geaendert: alles neu aufbauen"""
        self._update_font_css()
        ColumnView.show_hidden = self.settings["show_hidden"]
        for w in self.windows():
            w.refresh_all()
            w.columns_container.queue_distribute()

    def resort(self, folder=None):
        """Spalten neu sortieren: folder=None -> alle, die dem Standard folgen"""
        for w in self.windows():
            for c in list(w.columns):
                if folder is None and not self.sorting.has_own(c.path):
                    w.refresh_column(c)
                elif folder is not None and c.path == Path(folder):
                    w.refresh_column(c)
            w.update_preview()
        if self.settings_dialog is not None:
            self.settings_dialog.sync()

    def apply_widths(self):
        for w in self.windows():
            w.columns_container._distribute_widths()

    def change_font(self, step):
        if step == 0:
            self.settings["font_size"] = 0
        else:
            cur = self.settings.effective_font_size()
            self.settings["font_size"] = max(6, min(28, cur + step))
        self.apply_appearance()
        if self.settings_dialog is not None:
            self.settings_dialog.sync()

    def show_settings(self, parent):
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(self, parent)
            self.settings_dialog.connect("destroy", lambda *_: setattr(self, "settings_dialog", None))
        else:
            self.settings_dialog.set_transient_for(parent)
        self.settings_dialog.present()

    def do_command_line(self, command_line):
        args = command_line.get_arguments()
        self.start_path = None
        if len(args) > 1:
            path = args[1]
            if path.startswith('file://'):
                path = urllib.parse.unquote(path[7:])
            self.start_path = path
        self.activate()
        return 0

    def set_status(self, text):
        self.status_text = text
        for w in self.get_windows():
            if isinstance(w, MillerColumnsWindow):
                w.set_status(text)

    def operation_finished(self, kind, errors, created, dest, origin_win):
        self.set_status("")
        self.sizer.invalidate()
        for w in self.get_windows():
            if isinstance(w, MillerColumnsWindow):
                if w is origin_win:
                    w.after_operation(kind, created, dest)
                else:
                    w.refresh_all()
        if errors:
            parent = origin_win if origin_win in self.get_windows() else None
            dlg = Gtk.MessageDialog(transient_for=parent, modal=True,
                                    message_type=Gtk.MessageType.ERROR,
                                    buttons=Gtk.ButtonsType.OK,
                                    text="Nicht alles hat geklappt")
            more = f"\n… und {len(errors) - 10} weitere" if len(errors) > 10 else ""
            dlg.format_secondary_text("\n".join(errors[:10]) + more)
            dlg.run()
            dlg.destroy()


def main():
    start_path = None
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if path.startswith('file://'):
            path = urllib.parse.unquote(path[7:])
        start_path = path
    GLib.set_prgname("nemo-miller-columns")
    Gdk.set_program_class("nemo-miller-columns")
    theme = Gtk.IconTheme.get_default()
    Gtk.Window.set_default_icon_name(APP_ICON if theme.has_icon(APP_ICON) else "system-file-manager")
    app = MillerColumnsApp(start_path)
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
