# catalog.py
"""
Channel catalog logic.

Holds the get_iplayer listing format used by the scan, parsing of its output,
programme classification, the diff of a fresh scan against the catalog saved
by the previous run, and the mapping of iPlayer's category data onto a
programme. Everything here is a pure function: process handling lives in
workers.py and persistence in storage.py.
"""
from dataclasses import dataclass, field
from datetime import datetime

# Channels offered for the scan, in display order.
SCAN_CHANNELS = (
    "BBC One",
    "BBC Two",
    "BBC Three",
    "BBC Four",
    "BBC Scotland",
    "BBC Alba",
)

# ASCII "unit separator". Unlike '|' or tabs it never occurs in programme
# metadata, so a line can be split on it safely.
LIST_SEPARATOR = "\x1f"

# Fields requested from get_iplayer via --listformat, in output order.
# <categories> is deliberately absent: get_iplayer only fills it in --info
# mode, so a plain listing would print the literal token.
LIST_FIELDS = (
    "pid",
    "type",
    "name",
    "episode",
    "channel",
    "seriesnum",
    "episodenum",
    "available",
    "expires",
    "duration",
    "desc",
    "web",
    "thumbnail",
)
LIST_FORMAT = LIST_SEPARATOR.join(f"<{name}>" for name in LIST_FIELDS)

# Categories get_iplayer passes over when picking a programme's headline
# category, because they describe the format or the nation, not the genre.
IGNORED_CATEGORIES = (
    "Films", "Sign Zone", "Audio Described", "Northern Ireland", "Scotland",
    "Wales", "England",
)


def parse_listing_line(line):
    """
    Turn one line of --listformat output into an item dict.

    Returns None for anything that isn't a listing line (blank lines,
    "Matches:", INFO/WARNING messages, ...), so callers can log those instead.
    """
    parts = line.rstrip("\r\n").split(LIST_SEPARATOR)
    if len(parts) != len(LIST_FIELDS):
        return None

    item: dict[str, str] = dict(zip(LIST_FIELDS, (part.strip() for part in parts)))
    if not item["pid"]:
        return None

    # get_iplayer prints "-" for a programme without an episode title.
    if item["episode"] == "-":
        item["episode"] = ""
    item["kind"] = classify(item)
    return item


# Programme types as shown in the Type column and its filter row.
KIND_SERIES = "Series"
KIND_ONE_OFF = "One-off"
KINDS = (KIND_SERIES, KIND_ONE_OFF)


def classify(item):
    """
    Programme type as shown in the Type column.

    get_iplayer's cache carries no genre, so this is derived from structure:
    anything with an episode title or series/episode numbering is an episode
    of a series or strand ("Series"); the rest are standalone programmes such
    as films and single documentaries ("One-off").
    """
    if item.get("episode") or item.get("seriesnum") or item.get("episodenum"):
        return KIND_SERIES
    return KIND_ONE_OFF


def categories_from_programme(doc):
    """
    (category, categories) for a programme record from bbc.co.uk's
    programmes JSON, derived the way get_iplayer --info does it.

    Each category comes with its broader ancestors; the list is the
    broadest level first, then the middle, then the narrowest, without
    duplicates. The headline category is the first that isn't a format or
    nation. Both are '' when the record carries no categories.
    """
    levels = ([], [], [])  # broadest, middle, narrowest; each newest-first
    for cat1 in doc.get("categories") or []:
        chain = []
        node = cat1
        while isinstance(node, dict) and len(chain) < 3:
            chain.append(node.get("title") or "")
            node = (node.get("broader") or {}).get("category")
        # chain is narrowest-first; file each title under its level.
        for depth, title in enumerate(chain):
            levels[2 - depth].insert(0, title)

    titles = []
    for title in levels[0] + levels[1] + levels[2]:
        if title and title not in titles:
            titles.append(title)

    category = next(
        (
            title for title in titles
            if not any(title.lower() in ignored.lower()
                       for ignored in IGNORED_CATEGORIES)
        ),
        titles[0] if titles else "",
    )
    return category, ",".join(titles)


def pids_without_category(items):
    """pids of catalog items that have never had a category lookup."""
    return [pid for pid, item in items.items() if "category" not in item]


def available_timestamp(item):
    """Epoch seconds for the item's availability date, or 0 if unparseable."""
    value = item.get("available", "")
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except (TypeError, ValueError):
        return 0


def expires_timestamp(item):
    """Epoch seconds for the item's expiry, or 0 if unparseable."""
    try:
        return int(item.get("expires", ""))
    except (TypeError, ValueError):
        return 0


@dataclass
class ScanOutcome:
    """Result of reconciling a scan with the previously saved catalog."""

    # (item, is_new) for every programme on a successfully scanned channel.
    entries: list = field(default_factory=list)
    # pid -> item: the catalog to persist after this scan.
    merged: dict = field(default_factory=dict)
    new_count: int = 0
    # Channels whose listing failed or came back empty; their old entries
    # are kept so they don't all reappear as "new" on the next run.
    failed: list = field(default_factory=list)
    # Channels the baseline had never seen. Nothing on them is flagged as
    # new, otherwise a freshly enabled channel would light up wholesale.
    unseen: list = field(default_factory=list)


def apply_scan(baseline, previous, results):
    """
    Reconcile scan results with the stored catalogs.

    baseline: pid -> item as loaded at startup; "new" means absent from this.
    previous: pid -> item most recently written to disk; unscanned channels
              carry their entries over from here.
    results:  channel -> list of items, or None when get_iplayer failed.
    """
    baseline_channels = {item.get("channel") for item in baseline.values()}
    previous_channels = {item.get("channel") for item in previous.values()}

    outcome = ScanOutcome(merged=dict(previous))
    for channel, items in results.items():
        # An empty listing for a channel we know has programmes almost
        # certainly means get_iplayer's cache failed to refresh.
        if items is None or (not items and channel in previous_channels):
            outcome.failed.append(channel)
            continue

        outcome.merged = {
            pid: item for pid, item in outcome.merged.items()
            if item.get("channel") != channel
        }

        flag_new = channel in baseline_channels
        if not flag_new:
            outcome.unseen.append(channel)

        for item in items:
            # A fresh listing knows nothing of the categories looked up
            # for earlier runs; carry them over so only unseen programmes
            # need another lookup.
            earlier = previous.get(item["pid"])
            if earlier is not None and "category" in earlier:
                item["category"] = earlier["category"]
                item["categories"] = earlier.get("categories", "")
            outcome.merged[item["pid"]] = item
            is_new = flag_new and item["pid"] not in baseline
            outcome.entries.append((item, is_new))
            if is_new:
                outcome.new_count += 1

    return outcome
