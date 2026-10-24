# history.py
"""
Read access to get_iplayer's download history.

get_iplayer appends a line to <profile dir>/download_history for every
programme it downloads and, unless run with --force, refuses to download a
pid on that list again. The app reads the file to mark those programmes in
the catalog; it never writes to it.
"""
import os

HISTORY_FILE = "download_history"

# Positions in a history line, from get_iplayer's @history_format:
#   pid|name|episode|type|timeadded|mode|filename|versions|duration|desc|...
# Nothing escapes the '|' separator, so a '|' inside a description shifts
# every later field; only fields before it are relied on.
PID_FIELD = 0
TIME_FIELD = 4
FILENAME_FIELD = 6
MIN_FIELDS = 7


def profile_dir():
    """get_iplayer's profile directory, resolved the way get_iplayer does."""
    for variable in ("GETIPLAYER_PROFILE", "GETIPLAYERUSERPREFS"):
        value = os.environ.get(variable)
        if value:
            return value
    return os.path.join(os.path.expanduser("~"), ".get_iplayer")


def history_path():
    return os.path.join(profile_dir(), HISTORY_FILE)


def parse_history_line(line):
    """
    (pid, record) for one line of the history file, or None if it isn't
    one. record is {'timeadded': epoch seconds (0 if unreadable),
    'filename': path the download was saved to}.
    """
    parts = line.rstrip("\r\n").split("|")
    if len(parts) < MIN_FIELDS or not parts[PID_FIELD]:
        return None
    try:
        timeadded = int(parts[TIME_FIELD])
    except ValueError:
        timeadded = 0
    return parts[PID_FIELD], {
        "timeadded": timeadded,
        "filename": parts[FILENAME_FIELD],
    }


def load_history():
    """
    pid -> record for every programme in get_iplayer's download history.
    A pid downloaded more than once (a forced re-download adds another
    line) keeps its latest record. Empty when the file doesn't exist; any
    other failure to read it is raised as OSError.
    """
    records = {}
    try:
        with open(history_path(), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parsed = parse_history_line(line)
                if parsed is not None:
                    records[parsed[0]] = parsed[1]
    except FileNotFoundError:
        pass
    return records
