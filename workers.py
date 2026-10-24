# workers.py
import concurrent.futures
import http.client
import json
import subprocess
import threading
import re
import urllib.parse
import urllib.request
import shlex
import shutil
import os
import pty  # Required to force progress bar output
import gi

gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GLib, Gio, GdkPixbuf  # noqa: E402

import catalog  # noqa: E402

# Regex to capture percentage (e.g., "54.3%" or "54%")
PROGRESS_REGEX = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
# PTY output uses \r for in-place progress updates and \n for normal lines
LINE_END_REGEX = re.compile(r"[\r\n]")

USER_AGENT = "GiPlayer/1.0 (get_iplayer front-end)"

# Programme metadata, the source get_iplayer --info reads categories from.
# It answers one pid per request, so the lookup runs a few workers, each
# on its own persistent connection: opening a fresh TLS connection per
# request made a noticeable share of them stall until they timed out.
PROGRAMME_HOST = "www.bbc.co.uk"
PROGRAMME_JSON_PATH = "/programmes/{pid}.json"
CATEGORY_WORKERS = 3
CATEGORY_REPORT_EVERY = 25   # results per on_batch call
CATEGORY_TIMEOUT = 15        # seconds per request

# External tools the app relies on. 'critical' tools block the whole app when
# missing; non-critical 'required' tools only impair some features.
PREREQUISITES = [
    {
        "name": "get_iplayer",
        "required": True,
        "critical": True,
        "detail": "Core BBC download tool. Install it via your package manager "
                  "(e.g. 'sudo apt install get-iplayer' or "
                  "'sudo dnf install get_iplayer').",
    },
    {
        "name": "ffmpeg",
        "required": True,
        "critical": False,
        "detail": "Used by get_iplayer to convert/mux downloads. Searching and "
                  "previews work without it, but downloads will fail.",
    },
    {
        "name": "AtomicParsley",
        "required": False,
        "critical": False,
        "detail": "Optional: used by get_iplayer to write MP4 metadata tags.",
    },
]


def check_prerequisites():
    """
    Probe the system for required/optional external tools.

    Returns a list of dicts, one per tool:
        {'name', 'required', 'critical', 'found', 'path', 'detail'}
    """
    report = []
    for spec in PREREQUISITES:
        path = shutil.which(spec["name"])
        report.append({
            "name": spec["name"],
            "required": spec["required"],
            "critical": spec["critical"],
            "found": path is not None,
            "path": path or "",
            "detail": spec["detail"],
        })
    return report

def scan_channels(channels, on_log, on_channel_scanned, on_finished):
    """
    List every programme on each channel for the catalog (one get_iplayer
    run per channel, sequentially, so only the first run pays for a cache
    refresh).

    on_channel_scanned(channel, items) is invoked on the main loop after
    each channel; items is a list of dicts from catalog.parse_listing_line,
    or None if get_iplayer failed for that channel.
    """
    try:
        for channel in channels:
            items = _list_channel(channel, on_log)
            GLib.idle_add(on_channel_scanned, channel, items)
    finally:
        GLib.idle_add(on_finished)


def _list_channel(channel, on_log):
    """Programmes on one channel, or None on failure."""
    # The channel option is a regex with substring semantics; anchor it so
    # "BBC One" can never pick up another channel containing that text.
    cmd = ['get_iplayer', '--nocopyright', '--type', 'tv',
           '--channel', f'^{re.escape(channel)}$',
           '--listformat', catalog.LIST_FORMAT, '--', '*']
    # The format string holds control characters, so log a summary
    # instead of the exact command line.
    GLib.idle_add(on_log, f"Scanning {channel}...\n")

    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
    except FileNotFoundError:
        GLib.idle_add(on_log, "ERROR: 'get_iplayer' command not found.\n")
        return None
    except OSError as e:
        GLib.idle_add(on_log, f"ERROR: could not run get_iplayer: {e}\n")
        return None

    items = []
    assert process.stdout is not None  # guaranteed by stdout=PIPE
    for line in process.stdout:
        item = catalog.parse_listing_line(line)
        if item is not None:
            # Normalise to the requested spelling; the anchored regex is
            # case-insensitive so this only ever changes letter case.
            if item["channel"].lower() == channel.lower():
                item["channel"] = channel
                items.append(item)
            continue

        line = line.strip()
        if not line or line.startswith("Matches"):
            continue
        # Cache-refresh progress, warnings and errors are worth seeing.
        GLib.idle_add(on_log, line + "\n")

    process.wait()
    if process.returncode != 0:
        GLib.idle_add(
            on_log,
            f"Scan of {channel} failed (get_iplayer exit code "
            f"{process.returncode}).\n"
        )
        return None

    GLib.idle_add(on_log, f"  {channel}: {len(items)} programmes\n")
    return items

def fetch_categories(pids, on_log, on_batch, on_finished):
    """
    Look up each pid's categories from bbc.co.uk's programmes JSON, a few
    requests at a time.

    on_batch({pid: {'category': str, 'categories': str}}) is invoked on the
    main loop every CATEGORY_REPORT_EVERY results; both strings are '' when
    the BBC has no record of the pid. A request that fails for any other
    reason leaves its pid unreported so it's tried again next time, and
    on_finished(failed) gives the number of those.
    """
    failed = 0
    pending = {}

    def flush():
        nonlocal pending
        if pending:
            GLib.idle_add(on_batch, pending)
            pending = {}

    try:
        with concurrent.futures.ThreadPoolExecutor(CATEGORY_WORKERS) as pool:
            results = pool.map(_fetch_programme_categories, pids)
            for pid, result in zip(pids, results):
                if isinstance(result, Exception):
                    failed += 1
                    if failed <= 3:
                        GLib.idle_add(
                            on_log, f"Category lookup failed for {pid}: {result}\n"
                        )
                    continue
                pending[pid] = result
                if len(pending) >= CATEGORY_REPORT_EVERY:
                    flush()
        flush()
    finally:
        GLib.idle_add(on_finished, failed)


# One connection per worker thread, reused across requests.
_connections = threading.local()


def _programme_connection(fresh=False):
    connection = getattr(_connections, "programmes", None)
    if fresh and connection is not None:
        connection.close()
        connection = None
    if connection is None:
        connection = http.client.HTTPSConnection(
            PROGRAMME_HOST, timeout=CATEGORY_TIMEOUT
        )
        _connections.programmes = connection
    return connection


def _fetch_programme_categories(pid):
    """{'category', 'categories'} for pid, or the exception that stopped it."""
    unknown = {"category": "", "categories": ""}
    path = PROGRAMME_JSON_PATH.format(pid=urllib.parse.quote(pid, safe=""))
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    error = None
    # A dropped keep-alive connection fails the first request made on it;
    # one retry on a fresh connection covers that.
    for attempt in range(2):
        connection = _programme_connection(fresh=attempt > 0)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            body = response.read()
        except (OSError, http.client.HTTPException) as e:
            error = e
            continue

        if response.status == 404:
            return unknown  # the BBC has no programme page for this pid
        if response.status != 200:
            return http.client.HTTPException(
                f"HTTP {response.status} {response.reason}"
            )
        try:
            data = json.loads(body)
        except ValueError as e:
            return e
        doc = data.get("programme") if isinstance(data, dict) else None
        if not isinstance(doc, dict):
            return unknown
        category, categories = catalog.categories_from_programme(doc)
        return {"category": category, "categories": categories}

    return error if error is not None else OSError("no response")


def fetch_info(pid, on_log, on_update_preview, on_image_ready, on_finished):
    """
    Fetches details for a specific PID.
    """
    info_data = {}
    thumbnail_url = None

    try:
        cmd = ['get_iplayer', '--info', f'--pid={pid}']
        quoted_cmd = ' '.join(shlex.quote(arg) for arg in cmd)
        GLib.idle_add(on_log, f"Running command: {quoted_cmd}\n")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace'
        )

        assert process.stdout is not None  # guaranteed by stdout=PIPE
        for line in process.stdout:
            if "INFO:" in line:
                continue
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                info_data[key] = value

                if key == "thumbnail":
                    thumbnail_url = value

        process.wait()
        GLib.idle_add(on_update_preview, info_data)

        if thumbnail_url:
            fetch_image(thumbnail_url, on_image_ready, on_log)

    except Exception as e:
        GLib.idle_add(on_log, f"Error fetching info: {e}\n")
    finally:
        GLib.idle_add(on_finished)

def fetch_image(url, on_image_ready, on_log):
    """
    Downloads the image and returns a GdkPixbuf.
    """
    try:
        headers = {'User-Agent': USER_AGENT}
        req = urllib.request.Request(url, headers=headers)

        with urllib.request.urlopen(req, timeout=15) as response:
            image_data = response.read()

        glib_bytes = GLib.Bytes.new(image_data)
        input_stream = Gio.MemoryInputStream.new_from_bytes(glib_bytes)

        pixbuf = GdkPixbuf.Pixbuf.new_from_stream(input_stream, None)

        GLib.idle_add(on_image_ready, pixbuf)

    except Exception as e:
        GLib.idle_add(on_log, f"Failed to download thumbnail: {e}\n")

def _handle_output_line(line, on_log, on_progress):
    """Route one line of get_iplayer output to the progress or log callback."""
    line = line.strip()
    if not line:
        return

    match_prog = PROGRESS_REGEX.search(line)
    if match_prog:
        percent = float(match_prog.group(1))
        # Pass both percent AND the full text line (e.g. "54% of ~200MB ...")
        GLib.idle_add(on_progress, percent, line)
        return

    # If not a progress line, log it
    GLib.idle_add(on_log, line + "\n")


def download_command(pid, download_path, force=False):
    """
    The get_iplayer command line that downloads one programme.

    With `force`, a programme get_iplayer has already fetched is downloaded
    again: --force ignores its download history and --overwrite replaces a
    file still in the download folder.
    """
    cmd = ['get_iplayer', '--get', f'--pid={pid}']
    if force:
        cmd.extend(['--force', '--overwrite'])
    if download_path:
        cmd.extend(['--output', download_path])
    return cmd


def download(pid, download_path, on_log, on_progress, on_finished,
             force=False):
    """
    Downloads the program using a Pseudo-Terminal (PTY) to force progress output.

    Args:
        on_progress (func): Callback(float, str) with percentage and status text.
        force (bool): Download again even if get_iplayer has the programme in
            its history or the file already exists (see download_command).
    """
    master_fd, slave_fd = None, None
    try:
        cmd = download_command(pid, download_path, force)

        quoted_cmd = ' '.join(shlex.quote(arg) for arg in cmd)
        GLib.idle_add(on_log, f"Running command: {quoted_cmd}\n")

        # --- FIX: Use PTY to force progress bar ---
        master_fd, slave_fd = pty.openpty()

        process = subprocess.Popen(
            cmd,
            stdout=slave_fd,
            stderr=slave_fd, # Merge stderr into stdout
            close_fds=True,
            text=True, # Ignored when using fds, but good practice intent
            bufsize=0
        )

        # Close slave in parent process so we get EOF when child exits
        os.close(slave_fd)
        slave_fd = None

        buffer = ""
        while True:
            try:
                # Read raw bytes from the pseudo-terminal
                data = os.read(master_fd, 1024)
            except OSError:
                # Linux throws EIO when the last slave is closed (process finished)
                break

            if not data:
                break

            # Decode bytes to string
            buffer += data.decode('utf-8', errors='replace')

            # Process every complete line (terminated by \r or \n); keep the
            # unterminated remainder in the buffer for the next read.
            *lines, buffer = LINE_END_REGEX.split(buffer)
            for line in lines:
                _handle_output_line(line, on_log, on_progress)

        # Flush whatever was left without a trailing newline (typically a
        # final error message) so it isn't silently dropped.
        _handle_output_line(buffer, on_log, on_progress)

        process.wait()
        # --- End PTY Fix ---

        if process.returncode == 0:
            GLib.idle_add(on_log, f"\nDownload complete for {pid}.\n")
            GLib.idle_add(on_progress, 100.0, "Done")
        else:
            GLib.idle_add(on_log, f"\nDownload failed (code: {process.returncode}).\n")

    except Exception as e:
        GLib.idle_add(on_log, f"Error during download: {e}\n")
    finally:
        # Clean up file descriptors
        if slave_fd is not None:
            os.close(slave_fd)
        if master_fd is not None:
            os.close(master_fd)
        GLib.idle_add(on_finished)
