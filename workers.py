# workers.py
import subprocess
import re
import urllib.request
import shlex
import shutil
import os
import pty  # Required to force progress bar output
import gi

gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GLib, Gio, GdkPixbuf  # noqa: E402

# Compile regex once at module level
PID_REGEX = re.compile(r"^\s*(\w+):")
# Regex to capture percentage (e.g., "54.3%" or "54%")
PROGRESS_REGEX = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
# PTY output uses \r for in-place progress updates and \n for normal lines
LINE_END_REGEX = re.compile(r"[\r\n]")

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

def search(term, channel, category, on_log, on_result, on_finished):
    """
    Runs the get_iplayer search command.
    """
    try:
        # Note: <categories> isn't populated in plain search mode (it only
        # appears via --info), so we leave it out to avoid a literal
        # "<categories>" showing up in the results list.
        cmd = ['get_iplayer', '--listformat',
               '<pid>: <name> -- <episode> [<channel>]']

        if channel:
            cmd.extend(['--channel', channel])
        if category:
            cmd.extend(['--category', category])

        cmd.append('--')
        cmd.append(term)

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

        match_count = 0
        assert process.stdout is not None  # guaranteed by stdout=PIPE
        for line in process.stdout:
            line = line.strip()
            if not line or line.startswith(("Matches", "INFO:")):
                continue
            # get_iplayer logs warnings/errors to stdout too; they would
            # otherwise match PID_REGEX and show up as result rows.
            if line.startswith(("WARNING:", "ERROR:")):
                GLib.idle_add(on_log, line + "\n")
                continue

            match = PID_REGEX.match(line)
            if match:
                pid = match.group(1)
                GLib.idle_add(on_result, line, pid, line)
                match_count += 1

        process.wait()
        GLib.idle_add(on_log, f"Search complete. Found {match_count} matches.\n")

    except FileNotFoundError:
        GLib.idle_add(on_log, "ERROR: 'get_iplayer' command not found.\n")
    except Exception as e:
        GLib.idle_add(on_log, f"An error occurred during search: {e}\n")
    finally:
        GLib.idle_add(on_finished)

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
        headers = {'User-Agent': 'get_iplayer/3.30'}
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


def download(pid, download_path, on_log, on_progress, on_finished):
    """
    Downloads the program using a Pseudo-Terminal (PTY) to force progress output.

    Args:
        on_progress (func): Callback(float, str) with percentage and status text.
    """
    master_fd, slave_fd = None, None
    try:
        cmd = ['get_iplayer', '--get', f'--pid={pid}']
        if download_path:
            cmd.extend(['--output', download_path])

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
