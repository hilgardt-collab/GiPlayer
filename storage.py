# storage.py
"""
JSON persistence for user settings and the channel catalog.

Settings live in the XDG config dir, the catalog in the XDG data dir. Writes
go through a temp file and an atomic rename so a crash mid-write can't leave
a truncated file behind.
"""
import json
import os
import tempfile
from datetime import datetime

from gi.repository import GLib

from catalog import SCAN_CHANNELS

APP_DIR_NAME = "giplayer"
CATALOG_VERSION = 1


class StorageError(Exception):
    """A settings or catalog file could not be read or written."""


def settings_path():
    return os.path.join(GLib.get_user_config_dir(), APP_DIR_NAME, "settings.json")


def catalog_path():
    return os.path.join(GLib.get_user_data_dir(), APP_DIR_NAME, "catalog.json")


def default_settings():
    return {
        "download_path": None,
        "scan_channels": list(SCAN_CHANNELS),
        "show_only_new": False,
        "show_leaving_soon": False,
        # Type and category values unticked in the filter rows. Anything
        # not listed is shown, so a category new to the catalog shows up.
        # '' in hidden_categories stands for programmes without one.
        "hidden_types": [],
        "hidden_categories": [],
        # Whether the filter sidebar and the log pane are shown.
        "show_sidebar": True,
        "show_log": False,
        # [{'pid': str, 'title': str, 'force': bool}, ...] - downloads
        # pending or in progress when the app last closed, restored to the
        # queue on the next run. 'force' marks a deliberate re-download.
        "download_queue": [],
    }


def _read_json(path):
    """Parsed JSON for path, or None if the file doesn't exist."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise StorageError(f"{path}: {e}") from e


def _write_json(path, data):
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as e:
        raise StorageError(f"{path}: {e}") from e


def load_settings():
    """Saved settings merged over the defaults, with bad values dropped."""
    settings = default_settings()
    data = _read_json(settings_path())
    if not isinstance(data, dict):
        return settings

    path = data.get("download_path")
    if isinstance(path, str) and path:
        settings["download_path"] = path

    channels = data.get("scan_channels")
    if isinstance(channels, list):
        settings["scan_channels"] = [c for c in SCAN_CHANNELS if c in channels]

    settings["show_only_new"] = bool(data.get("show_only_new", False))
    settings["show_leaving_soon"] = bool(data.get("show_leaving_soon", False))
    settings["show_sidebar"] = bool(data.get("show_sidebar", True))
    settings["show_log"] = bool(data.get("show_log", False))

    for key in ("hidden_types", "hidden_categories"):
        values = data.get(key)
        if isinstance(values, list):
            settings[key] = sorted({v for v in values if isinstance(v, str)})

    queue = data.get("download_queue")
    if isinstance(queue, list):
        settings["download_queue"] = [
            {
                "pid": entry["pid"],
                "title": str(entry.get("title") or entry["pid"]),
                "force": bool(entry.get("force", False)),
            }
            for entry in queue
            if isinstance(entry, dict) and isinstance(entry.get("pid"), str)
            and entry["pid"]
        ]
    return settings


def save_settings(settings):
    _write_json(settings_path(), settings)


def load_catalog():
    """
    The catalog written by the last scan.

    Returns {'scanned_at': ISO string or None, 'items': {pid: item}}; both
    empty when no catalog has been saved yet.
    """
    empty = {"scanned_at": None, "items": {}}
    data = _read_json(catalog_path())
    if not isinstance(data, dict):
        return empty

    items = data.get("items")
    if not isinstance(items, dict):
        return empty

    scanned_at = data.get("scanned_at")
    return {
        "scanned_at": scanned_at if isinstance(scanned_at, str) else None,
        "items": {
            pid: item for pid, item in items.items()
            if isinstance(item, dict) and item.get("pid") == pid
        },
    }


def save_catalog(items):
    """Persist pid -> item as the catalog; returns the timestamp recorded."""
    scanned_at = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(catalog_path(), {
        "version": CATALOG_VERSION,
        "scanned_at": scanned_at,
        "items": items,
    })
    return scanned_at
