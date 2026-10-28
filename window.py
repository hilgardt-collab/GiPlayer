# window.py
import os
import threading
import time
from datetime import datetime, timezone
from functools import partial

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

import catalog
import history
import storage
import workers

# A programme is "leaving soon" if it expires within this many days.
LEAVING_SOON_DAYS = 3

# Filter-row title for programmes without a category: not looked up yet,
# or unknown to the BBC.
UNCATEGORISED_TITLE = "Uncategorised"

# Adw.WrapBox (libadwaita 1.7) lets the preview's chips wrap onto further
# lines; on an older libadwaita a plain box stands in.
WRAP_BOX_CLASS = getattr(Adw, "WrapBox", None)

# Adw.Spinner (libadwaita 1.6) is the current indeterminate spinner; the
# Gtk.Spinner fallback keeps older systems working.
SPINNER_BASE = getattr(Adw, "Spinner", Gtk.Spinner)

# Bold text for catalog rows that are new since the previous run.
BOLD_ATTRS = Pango.AttrList()
BOLD_ATTRS.insert(Pango.attr_weight_new(Pango.Weight.BOLD))

# Window width below which the filter sidebar folds away into an overlay.
SIDEBAR_BREAKPOINT = "max-width: 1100sp"

# Height of the log pane when it is opened; the divider above it can be
# dragged to make it taller.
LOG_PANE_HEIGHT = 220

# Titles for the fields get_iplayer --info prints, where the field name
# alone wouldn't do; any other field is shown with its first letter
# capitalised.
DETAIL_TITLES = {
    "pid": "PID",
    "desclong": "Long description",
    "firstbcast": "First broadcast",
    "nameshort": "Short name",
    "longname": "Long name",
    "seriesnum": "Series number",
    "episodenum": "Episode number",
    "web": "Web page",
}

APP_CSS = """
/* Pill badge in the "New" column. Its own background keeps it legible
   even on a selected (accent-coloured) row. */
.new-badge {
  background-color: var(--accent-bg-color);
  color: var(--accent-fg-color);
  border-radius: 999px;
  padding: 1px 8px;
  font-size: 0.8em;
  font-weight: bold;
}

/* The header's own arrow only ever marks key 1; the numbered titles carry
   the direction of every key, so it's hidden. */
columnview.numbered-sort > header > button sort-indicator {
  -gtk-icon-source: none;
  min-width: 0;
  min-height: 0;
}

/* Programme preview: rounded thumbnail, tag chips, details card. */
.preview-thumbnail {
  border-radius: 12px;
}
.chip {
  background-color: alpha(currentColor, 0.1);
  border-radius: 999px;
  padding: 2px 10px;
  font-size: 0.85em;
  font-weight: 600;
}
.chip.accent {
  background-color: var(--accent-bg-color);
  color: var(--accent-fg-color);
}
.chip.success {
  background-color: var(--success-bg-color);
  color: var(--success-fg-color);
}
.preview-details {
  padding: 10px 14px;
}

/* Pending-download count on the Queue button. */
.queue-badge {
  background-color: var(--accent-bg-color);
  color: var(--accent-fg-color);
  border-radius: 999px;
  padding: 0 6px;
  font-size: 0.8em;
  font-weight: bold;
}

.log-view {
  font-size: 0.9em;
}
"""


def _make_spinner():
    """A busy spinner, hidden until _spin shows it."""
    spinner = SPINNER_BASE()
    spinner.set_visible(False)
    return spinner


def _spin(spinner, spinning):
    """Show the spinner while it spins and hide it otherwise. Adw.Spinner
    animates whenever it is visible; the Gtk.Spinner fallback is started
    and stopped as well."""
    if SPINNER_BASE is Gtk.Spinner:
        if spinning:
            spinner.start()
        else:
            spinner.stop()
    spinner.set_visible(spinning)


def _make_wrap_box(spacing):
    """A box that wraps its children onto further lines as the width
    requires (a single line on a libadwaita without Adw.WrapBox)."""
    if WRAP_BOX_CLASS is not None:
        box = WRAP_BOX_CLASS()
        box.set_child_spacing(spacing)
        box.set_line_spacing(spacing)
        return box
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)


def _remove_children(container):
    """Remove every child of a container with remove(child)."""
    while child := container.get_first_child():
        container.remove(child)


def _detail_title(key):
    """Row title for a field name from get_iplayer --info."""
    return DETAIL_TITLES.get(key) or (key[:1].upper() + key[1:])


def _detail_value(key, value):
    """A field value from get_iplayer --info in a readable form: dates in
    local time, the expiry as a date, the duration in minutes. Anything
    that doesn't parse is shown as printed."""
    if key in ("available", "firstbcast"):
        try:
            return datetime.fromisoformat(value).astimezone().strftime(
                "%Y-%m-%d %H:%M"
            )
        except (TypeError, ValueError):
            return value
    if key == "expires":
        try:
            return _format_datetime(int(value))
        except (TypeError, ValueError):
            return value
    if key == "duration":
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            return value
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes} min {seconds} s" if seconds else f"{minutes} min"
    return value


def _format_date(timestamp):
    """Local calendar date for an epoch timestamp, or '' when unknown."""
    if not timestamp:
        return ""
    local = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()
    return local.strftime("%Y-%m-%d")


def _format_datetime(timestamp):
    """Local 'YYYY-MM-DD HH:MM' for an epoch timestamp, or '' when unknown."""
    if not timestamp:
        return ""
    local = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()
    return local.strftime("%Y-%m-%d %H:%M")


def _format_iso(text):
    """Local 'YYYY-MM-DD HH:MM' for an ISO timestamp string."""
    try:
        return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return text or "unknown"


def _search_terms(text):
    """Case-folded words of a search string; a row must contain every one."""
    return text.casefold().split()


def _leaving_soon_window():
    """(now, now + LEAVING_SOON_DAYS) in epoch seconds: the expiry times the
    "Leaving soon" filter accepts. Already-expired programmes fall outside
    it, so stale rows from an old catalog don't count as leaving."""
    now = int(time.time())
    return now, now + LEAVING_SOON_DAYS * 86400


def _reversed_sorter(inner):
    """A sorter that orders items the opposite way round to `inner`, for
    sorters without a sort-order property of their own."""
    def compare(a, b, *_args):
        return -int(inner.compare(a, b))
    return Gtk.CustomSorter.new(compare)


def _narrows(old, new):
    """
    True if a search for `new` can only match a subset of what `old`
    matched: every old term still occurs inside some new term. Lets the
    filter model re-test only the rows currently shown when the user types
    another letter, rather than the whole catalog.
    """
    return all(any(term in candidate for candidate in new) for term in old)


class CatalogEntry(GObject.Object):
    """
    One programme from the catalog, as shown in the main list.

    The GObject properties feed the column view's sorters through property
    expressions; the plain attributes carry what the rest of the UI needs.
    """

    __gtype_name__ = "GiPlayerCatalogEntry"

    pid = GObject.Property(type=str, default="")
    name = GObject.Property(type=str, default="")
    episode = GObject.Property(type=str, default="")
    channel = GObject.Property(type=str, default="")
    kind = GObject.Property(type=str, default="")
    # Looked up from iPlayer after the scan, so it can arrive later.
    category = GObject.Property(type=str, default="")
    available = GObject.Property(type=str, default="")
    # Epoch seconds so the Available column sorts chronologically.
    available_ts = GObject.Property(type=GObject.TYPE_INT64, default=0)
    # 0 for new programmes, so an ascending sort lists them first.
    new_rank = GObject.Property(type=int, default=1)
    # Whether get_iplayer's download history lists the programme, and when
    # it was last downloaded (epoch seconds, 0 if never) for the Downloaded
    # column's sort. Both can change while the row is shown.
    downloaded = GObject.Property(type=bool, default=False)
    downloaded_ts = GObject.Property(type=GObject.TYPE_INT64, default=0)

    def __init__(self, item, is_new, download=None):
        """`download` is the programme's record from history.load_history,
        or None if it has never been downloaded."""
        available_ts = catalog.available_timestamp(item)
        super().__init__(
            pid=item["pid"],
            name=item.get("name", ""),
            episode=item.get("episode", ""),
            channel=item.get("channel", ""),
            kind=item.get("kind") or catalog.classify(item),
            category=item.get("category") or "",
            available=_format_date(available_ts),
            available_ts=available_ts,
            new_rank=0 if is_new else 1,
        )
        self.is_new = is_new
        self.desc = item.get("desc") or "No description available."
        # Epoch seconds (0 if unknown) for the "Leaving soon" filter.
        self.expires_ts = catalog.expires_timestamp(item)
        self.expires = _format_date(self.expires_ts)

        # Same shape as the queue titles.
        title = self.name
        if self.episode:
            title += f" -- {self.episode}"
        self.title_text = f"{title} [{self.channel}]"

        self.download_record = None
        self.downloaded_when = ""
        self.downloaded_file = ""
        self.set_downloaded(download)

        # Everything the search box is matched against.
        self._search_base = " ".join(
            part for part in (self.pid, self.name, self.episode,
                              item.get("desc", ""))
            if part
        ).casefold()
        self.search_text = self._search_base
        self.set_category(self.category, item.get("categories") or "")

    def set_downloaded(self, record):
        """Record the programme's entry in get_iplayer's download history
        (None if it has none); bound cells follow the properties."""
        self.download_record = record
        if record is None:
            self.downloaded_when = ""
            self.downloaded_file = ""
            self.downloaded_ts = 0
            self.downloaded = False
        else:
            self.downloaded_when = _format_datetime(record["timeadded"])
            self.downloaded_file = record["filename"]
            self.downloaded_ts = record["timeadded"]
            self.downloaded = True

    @property
    def tooltip(self):
        """Tooltip for the row's cells, composed when shown so a download
        that finished after the row was bound is reflected."""
        lines = [self.desc]
        if self.expires:
            lines.append(f"Expires {self.expires}")
        if self.downloaded:
            when = self.downloaded_when or "before"
            lines.append(f"Downloaded {when}")
        return "\n".join(lines)

    def set_category(self, category, categories):
        """Record looked-up categories; bound cells and the search follow.
        `category` is the headline genre shown in the column, `categories`
        the full comma-separated list."""
        self.category = category
        self.categories = categories
        self.search_text = self._search_base
        if categories or category:
            self.search_text += " " + (categories or category).casefold()


class GiPlayerWindow(Adw.ApplicationWindow):
    """
    The main application window for GiPlayer.

    The filter sidebar sits beside the catalog of the ticked channels,
    filtered live by the search box in the header, and the programme
    preview; the downloads bar runs along the bottom, and the log can be
    opened beneath the catalog.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.set_default_size(1400, 900)
        self.set_title("GiPlayer")

        # --- State Management ---
        # Settings are loaded first so widgets can be built in their saved
        # state; any read error is logged once the log pane exists.
        settings_error = None
        try:
            self.settings = storage.load_settings()
        except storage.StorageError as e:
            settings_error = str(e)
            self.settings = storage.default_settings()

        self.download_path = None
        self.download_queue = []  # List of dicts: {'pid': str, 'title': str}
        self.current_download = None  # The item being downloaded right now
        self.is_downloading = False
        # Incremented on every preview load; callbacks from an older info
        # fetch compare against it and drop their results if superseded.
        self._info_request = 0
        self._busy_count = 0
        self._prereqs_ok = True

        # Catalog scan state. The baseline is the catalog saved by the
        # previous run; "new" means absent from it. It stays fixed for the
        # whole session so a Rescan doesn't wipe the highlights.
        self._scanning = False
        self._scan_channels = []
        self._scan_results = {}
        self.catalog_baseline = {"scanned_at": None, "items": {}}
        self.catalog_saved = {}
        # The category lookup that follows a scan: how far it has got, and
        # the scan outcome to summarise once it's done.
        self._category_total = 0
        self._category_done = 0
        self._last_outcome = None

        # Sort keys as [(column, Gtk.SortType)], key 1 first. The list's
        # sorter is built from this after every header click; the column
        # view's own sorter only serves to notice those clicks.
        self._sort_keys = []
        self._clearing_view_sorter = False
        # CatalogEntry per pid in the store, for in-place category and
        # download-history updates.
        self._entries_by_pid = {}
        # pid -> record from get_iplayer's download history, for the
        # Downloaded column; re-read after every download.
        self.download_history = {}
        # Last error reading that file, so it's logged once, not per read.
        self._history_error = None

        # Words in the search box; a row must contain every one of them.
        self._search_terms = []
        # Type and category values unticked in the filter rows.
        self._hidden_types = set(self.settings["hidden_types"])
        self._hidden_categories = set(self.settings["hidden_categories"])
        # True while a Ctrl-click syncs a whole row of tick boxes, so the
        # boxes' own toggled handlers stand back.
        self._syncing_checks = False
        # Expiry range the "Leaving soon" filter accepts; measured afresh
        # whenever the list is filled or the box is ticked.
        self._leaving_soon_window = _leaving_soon_window()
        # Rows the context menu acts on, fixed when it opens.
        self._context_entries = []
        # pids selected in the list after the last selection change, so a
        # Ctrl-click (preview the added row) can be told apart from a
        # Shift-click range (keep the current preview).
        self._selected_pids = set()
        # pid whose details the preview pane shows or is loading.
        self._preview_pid = None

        self._install_css()
        self._build_actions()

        # --- Header: sidebar toggle, busy spinner, search, rescan, log
        # toggle and the primary menu ---
        self.header = Adw.HeaderBar()

        self.sidebar_button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic")
        self.sidebar_button.set_tooltip_text("Show the filters (F9)")
        self.header.pack_start(self.sidebar_button)

        # Busy spinner - shown while a background info-fetch or catalog
        # scan is running. A counter keeps it spinning until *all*
        # in-flight operations finish.
        self.spinner = _make_spinner()
        self.header.pack_start(self.spinner)

        # The search box filters the catalog as you type; nothing is sent
        # to get_iplayer.
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search programmes")
        self.search_entry.set_tooltip_text(
            "Every word must appear in the programme's name, episode, "
            "description, categories or pid (Ctrl+F)"
        )
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("stop-search", self._on_stop_search)
        search_clamp = Adw.Clamp(maximum_size=560, tightening_threshold=400)
        search_clamp.set_hexpand(True)
        search_clamp.set_child(self.search_entry)
        self.header.set_title_widget(search_clamp)

        menu = Gio.Menu()
        menu.append("Show Log", "win.show-log")
        menu.append("About GiPlayer", "win.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_button.set_tooltip_text("Main menu")
        menu_button.set_menu_model(menu)
        menu_button.set_primary(True)
        self.header.pack_end(menu_button)

        self.log_button = Gtk.ToggleButton(icon_name="utilities-terminal-symbolic")
        self.log_button.set_tooltip_text("Show the log (Ctrl+L)")
        self.log_button.set_action_name("win.show-log")
        self.header.pack_end(self.log_button)

        self.rescan_button = Gtk.Button(icon_name="view-refresh-symbolic")
        self.rescan_button.set_tooltip_text(
            "Scan the ticked channels again and compare with the catalog "
            "saved by the previous run (F5)"
        )
        self.rescan_button.connect("clicked", self._on_rescan_clicked)
        self.header.pack_end(self.rescan_button)

        # --- Layout: the header on top, the downloads bar along the
        # bottom, and between them the filter sidebar beside the catalog,
        # the preview and (when opened) the log ---
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(self.header)
        toolbar_view.add_bottom_bar(self._build_downloads_bar())
        toolbar_view.set_bottom_bar_style(Adw.ToolbarStyle.RAISED)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(toolbar_view)
        self.set_content(self.toast_overlay)

        # Prerequisite warning banner (hidden unless a tool is missing);
        # its button opens the log, where the details are.
        self.prereq_banner = Adw.Banner()
        self.prereq_banner.set_revealed(False)
        self.prereq_banner.set_button_label("Show Log")
        self.prereq_banner.connect(
            "button-clicked", lambda _banner: self._set_log_shown(True)
        )

        # The log opens beneath the catalog and preview, at its own height
        # until the divider is dragged.
        self.log_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.log_paned.set_vexpand(True)
        self.log_paned.set_resize_start_child(True)
        self.log_paned.set_resize_end_child(False)
        self.log_paned.set_shrink_end_child(False)
        self.log_paned.set_start_child(self._build_browse_pane())
        self.log_paned.set_end_child(self._build_log_pane())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.prereq_banner)
        content.append(self.log_paned)

        self.split_view = Adw.OverlaySplitView()
        self.split_view.set_sidebar(self._build_filter_sidebar())
        self.split_view.set_content(content)
        self.split_view.set_min_sidebar_width(200)
        self.split_view.set_max_sidebar_width(240)
        self.split_view.set_sidebar_width_fraction(0.17)
        self.split_view.set_show_sidebar(self.settings["show_sidebar"])
        self.split_view.bind_property(
            "show-sidebar", self.sidebar_button, "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        toolbar_view.set_content(self.split_view)

        # On a narrow window the sidebar folds away, and overlays the
        # catalog when opened; widening the window brings it back.
        breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(SIDEBAR_BREAKPOINT)
        )
        breakpoint.add_setter(self.split_view, "collapsed", True)
        breakpoint.add_setter(self.split_view, "show-sidebar", False)
        self.add_breakpoint(breakpoint)

        self.connect("close-request", self._on_close_request)
        self._build_catalog_menu()
        self._set_log_shown(self.settings["show_log"])

        if settings_error:
            self.add_to_log(
                f"Could not read settings ({settings_error}); using defaults.\n"
            )

        # Verify external tools are present before the user tries to use them.
        self._check_prerequisites()
        self._restore_persisted_state()
        self._update_match_count()
        self.search_entry.grab_focus()

        if self._prereqs_ok:
            if self.download_queue:
                self._process_queue()
            self._start_catalog_scan()

    # --- UI Construction ---

    def _install_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_string(APP_CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _build_actions(self):
        """Window actions behind the header buttons, the menu and the
        keyboard shortcuts."""
        self.show_log_action = Gio.SimpleAction.new_stateful(
            "show-log", None, GLib.Variant.new_boolean(False)
        )
        self.show_log_action.connect("change-state", self._on_show_log_state)
        self.add_action(self.show_log_action)

        for name, handler in (
            ("focus-search", self._on_focus_search),
            ("rescan", self._on_rescan_action),
            ("show-sidebar", self._on_toggle_sidebar),
            ("about", self._on_about),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        app = self.get_application()
        if app is not None:
            app.set_accels_for_action("win.focus-search", ["<Control>f"])
            app.set_accels_for_action("win.rescan", ["F5"])
            app.set_accels_for_action("win.show-sidebar", ["F9"])
            app.set_accels_for_action("win.show-log", ["<Control>l"])

    def _on_show_log_state(self, action, value):
        self._set_log_shown(value.get_boolean())

    def _set_log_shown(self, shown):
        self.show_log_action.set_state(GLib.Variant.new_boolean(shown))
        self.log_scroll.set_visible(shown)
        self.settings["show_log"] = shown

    def _on_focus_search(self, action, parameter):
        self.search_entry.grab_focus()

    def _on_rescan_action(self, action, parameter):
        if self.rescan_button.get_sensitive():
            self._start_catalog_scan()

    def _on_toggle_sidebar(self, action, parameter):
        self.split_view.set_show_sidebar(not self.split_view.get_show_sidebar())

    def _on_about(self, action, parameter):
        about = Adw.AboutDialog()
        about.set_application_name("GiPlayer")
        about.set_application_icon("tv-symbolic")
        about.set_comments(
            "A graphical front-end for get_iplayer: browse, search and "
            "download BBC programmes."
        )
        about.set_license("GiPlayer is released under the GNU General Public License.")
        about.add_link("get_iplayer", "https://github.com/get-iplayer/get_iplayer")
        about.present(self)

    def _build_browse_pane(self):
        """Catalog on the left, programme preview on the right."""
        self.browse_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.browse_paned.set_vexpand(True)
        self.browse_paned.set_resize_start_child(True)
        self.browse_paned.set_position(800)
        self.browse_paned.set_start_child(self._build_catalog_pane())
        self.browse_paned.set_end_child(self._build_preview_pane())
        # The preview keeps a readable width; the catalog scrolls sideways
        # instead when the window is narrow.
        self.browse_paned.set_shrink_end_child(False)
        return self.browse_paned

    def _build_filter_sidebar(self):
        """The filter sidebar: the new/leaving-soon filters, then one tick
        box per channel (the channels ticked are also the ones scanned),
        programme type and category."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        _section, show_box = self._add_filter_section(box, "Show")
        self.only_new_check = Gtk.CheckButton(label="Only new programmes")
        self.only_new_check.set_active(self.settings["show_only_new"])
        self.only_new_check.set_tooltip_text(
            "Show only programmes that weren't in the catalog last time"
        )
        self.only_new_check.connect("toggled", self._on_only_new_toggled)
        show_box.append(self.only_new_check)

        self.leaving_soon_check = Gtk.CheckButton(label="Leaving soon")
        self.leaving_soon_check.set_active(self.settings["show_leaving_soon"])
        self.leaving_soon_check.set_tooltip_text(
            "Show only programmes that expire within the next "
            f"{LEAVING_SOON_DAYS} days"
        )
        self.leaving_soon_check.connect("toggled", self._on_leaving_soon_toggled)
        show_box.append(self.leaving_soon_check)

        self.channel_checks = {}
        _section, channel_box = self._add_filter_section(box, "Channels")
        for channel in catalog.SCAN_CHANNELS:
            check = self._make_filter_check(
                channel, channel in self.settings["scan_channels"],
                "Untick to hide this channel and leave it out of the next "
                "scan. Ctrl-click to show, and scan, only this channel.",
                self._on_scan_channel_toggled,
            )
            self.channel_checks[channel] = check
            channel_box.append(check)

        self.type_checks = {}
        _section, type_box = self._add_filter_section(box, "Type")
        for kind in catalog.KINDS:
            check = self._make_filter_check(
                kind, kind not in self._hidden_types,
                f"Untick to hide {kind} programmes. "
                "Ctrl-click to show only them.",
                self._on_value_filter_toggled, "hidden_types",
            )
            self.type_checks[kind] = check
            type_box.append(check)

        # One box per category in the catalog, built by
        # _update_category_checks once there is one; hidden until then.
        self.category_checks = {}
        self.category_section, self.category_box = self._add_filter_section(
            box, "Category"
        )
        self.category_section.set_visible(False)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(box)
        return scroll

    def _add_filter_section(self, container, title):
        """A titled group of tick boxes appended to `container`:
        (section, box the tick boxes go in)."""
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        container.append(section)
        heading = Gtk.Label(label=title, xalign=0.0)
        heading.add_css_class("heading")
        section.append(heading)
        checks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        section.append(checks)
        return section, checks

    def _build_catalog_pane(self):
        """Scan status, the sortable catalog with new programmes
        highlighted (or a status page while nothing is listed), and the
        match count beside the queue button."""
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pane.set_hexpand(True)

        # Scan status on a single line; the full text is its tooltip.
        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_row.set_margin_top(6)
        status_row.set_margin_bottom(6)
        status_row.set_margin_start(12)
        status_row.set_margin_end(12)
        pane.append(status_row)

        self.catalog_spinner = _make_spinner()
        status_row.append(self.catalog_spinner)

        self.catalog_status_label = Gtk.Label(label="Not scanned yet.")
        self.catalog_status_label.add_css_class("dim-label")
        self.catalog_status_label.add_css_class("caption")
        self.catalog_status_label.set_xalign(0.0)
        self.catalog_status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.catalog_status_label.set_hexpand(True)
        status_row.append(self.catalog_status_label)

        # The catalog itself, or a status page while there is nothing to
        # list (see _update_match_count).
        self.catalog_stack = Gtk.Stack()
        self.catalog_stack.set_vexpand(True)
        self.catalog_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        pane.append(self.catalog_stack)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self._build_catalog_view())
        self.catalog_stack.add_named(scroll, "list")

        self.catalog_empty_page = Adw.StatusPage()
        self.catalog_empty_page.set_icon_name("system-search-symbolic")
        self.catalog_empty_page.add_css_class("compact")
        self.catalog_stack.add_named(self.catalog_empty_page, "empty")

        # Match count + queue button
        actions = Gtk.ActionBar()
        pane.append(actions)

        self.match_count_label = Gtk.Label()
        self.match_count_label.add_css_class("dim-label")
        self.match_count_label.set_xalign(0.0)
        self.match_count_label.set_ellipsize(Pango.EllipsizeMode.END)
        actions.pack_start(self.match_count_label)

        self.catalog_add_button = Gtk.Button(label="Add to Queue")
        self.catalog_add_button.add_css_class("suggested-action")
        self.catalog_add_button.set_sensitive(False)
        self.catalog_add_button.set_tooltip_text(
            "Queue the selected programmes for download. Ctrl-click or "
            "Shift-click to select several."
        )
        self.catalog_add_button.connect("clicked", self.on_catalog_add_clicked)
        actions.pack_end(self.catalog_add_button)

        return pane

    def _make_filter_check(self, title, active, tooltip, handler, *args):
        check = Gtk.CheckButton(label=title)
        check.set_active(active)  # before connecting: no toggled signal
        check.set_tooltip_text(tooltip)
        check.connect("toggled", handler, *args)
        return check

    def _update_category_checks(self):
        """One tick box per category present in the catalog, alphabetical,
        with the blank category last. Rebuilt only when the set changes."""
        present = {entry.category for entry in self._entries_by_pid.values()}
        ordered = sorted((c for c in present if c), key=str.casefold)
        if "" in present:
            ordered.append("")
        if ordered == list(self.category_checks):
            return

        _remove_children(self.category_box)
        self.category_checks = {}
        for category in ordered:
            if category:
                tooltip = (
                    f"Untick to hide {category} programmes. "
                    "Ctrl-click to show only them."
                )
            else:
                tooltip = (
                    "Programmes whose category hasn't been looked up yet, "
                    "or that the BBC has no record of. Untick to hide them; "
                    "Ctrl-click to show only them."
                )
            check = self._make_filter_check(
                category or UNCATEGORISED_TITLE,
                category not in self._hidden_categories, tooltip,
                self._on_value_filter_toggled, "hidden_categories",
            )
            self.category_checks[category] = check
            self.category_box.append(check)
        self.category_section.set_visible(bool(ordered))

    def _build_preview_pane(self):
        """Programme preview: thumbnail, title, chips, description and
        details on one scrolling page, or a status page while nothing is
        selected."""
        self.preview_stack = Gtk.Stack()
        self.preview_stack.set_hexpand(True)
        self.preview_stack.set_size_request(300, -1)

        empty = Adw.StatusPage()
        empty.set_icon_name("tv-symbolic")
        empty.set_title("No Programme Selected")
        empty.set_description(
            "Select a programme to see its details and thumbnail."
        )
        empty.add_css_class("compact")
        self.preview_stack.add_named(empty, "empty")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.preview_stack.add_named(scroll, "details")

        # The page scrolls at its natural height, so the thumbnail (whose
        # minimum height is nothing, as it may shrink) gets the height
        # its aspect ratio asks for rather than whatever is left over.
        viewport = Gtk.Viewport()
        viewport.set_vscroll_policy(Gtk.ScrollablePolicy.NATURAL)
        scroll.set_child(viewport)

        # Every label on the page wraps, so a long single-line field
        # (get_iplayer prints desclong as one line) can't inflate the
        # pane's minimum width; the clamp keeps the text readable when
        # the pane is dragged wide.
        clamp = Adw.Clamp(maximum_size=720, tightening_threshold=560)
        viewport.set_child(clamp)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(18)
        page.set_margin_bottom(18)
        page.set_margin_start(18)
        page.set_margin_end(18)
        clamp.set_child(page)

        # Thumbnail: the picture's height follows the page width at the
        # image's aspect ratio, and the widget clips to rounded corners.
        self.preview_image = Gtk.Picture()
        self.preview_image.set_visible(False)
        self.preview_image.set_can_shrink(True)
        self.preview_image.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.preview_image.set_overflow(Gtk.Overflow.HIDDEN)
        self.preview_image.add_css_class("preview-thumbnail")
        page.append(self.preview_image)

        # Title and episode, with the spinner beside them while the full
        # details are being fetched.
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        page.append(heading)

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        titles.set_hexpand(True)
        heading.append(titles)

        self.preview_title = Gtk.Label(xalign=0.0)
        self.preview_title.add_css_class("title-2")
        self.preview_title.set_wrap(True)
        self.preview_title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        titles.append(self.preview_title)

        self.preview_subtitle = Gtk.Label(xalign=0.0)
        self.preview_subtitle.add_css_class("dim-label")
        self.preview_subtitle.set_wrap(True)
        self.preview_subtitle.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        titles.append(self.preview_subtitle)

        self.preview_spinner = _make_spinner()
        self.preview_spinner.set_valign(Gtk.Align.START)
        heading.append(self.preview_spinner)

        # Channel, type, category and download state as chips.
        self.preview_chips = _make_wrap_box(6)
        page.append(self.preview_chips)

        self.preview_desc = Gtk.Label(xalign=0.0)
        self.preview_desc.set_wrap(True)
        self.preview_desc.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        page.append(self.preview_desc)

        # Metadata as key/value rows in a card. Rows of boxes rather than
        # a grid: a grid measures its height at its minimum width, where
        # a wrapping value is one character per line, and reports that
        # height as its minimum.
        self.preview_details_card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        self.preview_details_card.add_css_class("card")
        self.preview_details_card.add_css_class("preview-details")
        self.preview_details_card.set_visible(False)
        page.append(self.preview_details_card)

        return self.preview_stack

    def _build_catalog_view(self):
        """Sortable column view over the catalog."""
        self.catalog_store = Gio.ListStore.new(CatalogEntry)
        self.catalog_filter = Gtk.CustomFilter.new(self._catalog_filter_func)
        self.catalog_filtered = Gtk.FilterListModel.new(
            self.catalog_store, self.catalog_filter
        )
        self.catalog_filtered.connect("items-changed", self._on_catalog_items_changed)

        self.catalog_view = Gtk.ColumnView()
        # The sorter is set by _apply_sort_keys from the numbered keys. The
        # view's own sorter can't hold more than one column through public
        # API, so it isn't used for sorting, only to notice header clicks.
        self.catalog_sorted = Gtk.SortListModel.new(self.catalog_filtered, None)
        self.catalog_selection = Gtk.MultiSelection.new(self.catalog_sorted)
        self.catalog_selection.connect(
            "selection-changed", self._on_catalog_selection_changed
        )
        self.catalog_view.set_model(self.catalog_selection)
        self.catalog_view.set_enable_rubberband(True)
        self.catalog_view.add_css_class("numbered-sort")
        # Cells carry their own tooltips, so this one shows over the headers.
        self.catalog_view.set_tooltip_text(
            "Click a column header to sort by it, and again to reverse the "
            "order. Shift-click a header to add it as the next sort key; "
            "Shift-click a numbered key to reverse it, then to remove it."
        )

        # Right-click opens the context menu for the row under the pointer.
        # Capture phase, so the click is claimed before the row's own
        # gesture can turn it into a selection change.
        right_click = Gtk.GestureClick()
        right_click.set_button(Gdk.BUTTON_SECONDARY)
        right_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        right_click.connect("pressed", self._on_catalog_right_click)
        self.catalog_view.add_controller(right_click)

        def prop(name):
            return Gtk.PropertyExpression.new(CatalogEntry, None, name)

        def by_text(name):
            # Sorter factory: order -> sorter over the text property.
            def make(order):
                sorter = Gtk.StringSorter.new(prop(name))
                if order == Gtk.SortType.DESCENDING:
                    return _reversed_sorter(sorter)
                return sorter
            return make

        def by_number(name):
            def make(order):
                sorter = Gtk.NumericSorter.new(prop(name))
                sorter.set_sort_order(order)
                return sorter
            return make

        # Each column sorts by its own field only: ties fall through to the
        # next numbered key and finally to the store's name/episode order.
        # The SortType is the direction a fresh click sorts in.
        asc, desc = Gtk.SortType.ASCENDING, Gtk.SortType.DESCENDING
        self._column_titles = {}
        self._default_orders = {}
        self._sorter_factories = {}
        new_column = self._add_catalog_column(
            "New", "badge", by_number("new-rank"), asc, fixed_width=64
        )
        self._add_catalog_column(
            "Downloaded", "downloaded", by_number("downloaded-ts"), desc,
            fixed_width=112
        )
        self._add_catalog_column(
            "Channel", "channel", by_text("channel"), asc, fixed_width=108
        )
        self._add_catalog_column(
            "Type", "kind", by_text("kind"), asc, fixed_width=82
        )
        self._add_catalog_column(
            "Category", "category", by_text("category"), asc, fixed_width=124
        )
        self._add_catalog_column(
            "Name", "name", by_text("name"), asc, expand=True
        )
        self._add_catalog_column(
            "Episode", "episode", by_text("episode"), asc, expand=True
        )
        available_column = self._add_catalog_column(
            "Available", "available", by_number("available-ts"), desc,
            fixed_width=104
        )

        # New programmes first, most recently available next, by default.
        self._sort_keys = [(new_column, asc), (available_column, desc)]
        self._apply_sort_keys()
        self.catalog_view.get_sorter().connect(
            "changed", self._on_view_sorter_changed
        )
        return self.catalog_view

    def _add_catalog_column(self, title, attr, make_sorter, default_order,
                            expand=False, fixed_width=None):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_catalog_cell_setup, attr)
        factory.connect("bind", self._on_catalog_cell_bind, attr)
        factory.connect("unbind", self._on_catalog_cell_unbind)

        column = Gtk.ColumnViewColumn.new(title, factory)
        # A column needs a sorter for its header to react to clicks at all;
        # the list itself is sorted by the sorters _apply_sort_keys builds.
        column.set_sorter(make_sorter(Gtk.SortType.ASCENDING))
        column.set_expand(expand)
        column.set_resizable(True)
        if fixed_width:
            column.set_fixed_width(fixed_width)
        self.catalog_view.append_column(column)
        self._column_titles[column] = title
        self._default_orders[column] = default_order
        self._sorter_factories[column] = make_sorter
        return column

    def _on_catalog_cell_setup(self, factory, list_item, attr):
        if attr == "downloaded":
            # A tick that is hidden unless the programme was downloaded.
            # The box fills the cell whether or not the tick shows, so the
            # cell keeps answering tooltip queries.
            tick = Gtk.Image.new_from_icon_name("object-select-symbolic")
            tick.add_css_class("success")
            tick.set_hexpand(True)
            tick.set_halign(Gtk.Align.CENTER)
            child = Gtk.Box()
            child.append(tick)
        else:
            child = Gtk.Label()
            if attr == "badge":
                child.set_halign(Gtk.Align.CENTER)
            else:
                child.set_xalign(0.0)
                child.set_ellipsize(Pango.EllipsizeMode.END)
        # Tooltips are composed when shown (see CatalogEntry.tooltip), so
        # they stay right when a row's state changes after binding.
        child.set_has_tooltip(True)
        child.connect("query-tooltip", self._on_catalog_cell_query_tooltip)
        list_item.set_child(child)

    def _on_catalog_cell_bind(self, factory, list_item, attr):
        entry = list_item.get_item()
        child = list_item.get_child()
        if not isinstance(entry, CatalogEntry) or child is None:
            return
        # Lets a right-click find the row's entry from the widget under the
        # pointer (see _entry_at).
        child.catalog_entry = entry

        # Cells are recycled, so every visual state is set explicitly.
        # Property bindings, for the cells that follow a value that can
        # change while the row is shown, are undone in unbind.
        bindings = []
        sync = GObject.BindingFlags.SYNC_CREATE
        if attr == "downloaded":
            tick = child.get_first_child()
            if tick is not None:
                bindings.append(
                    entry.bind_property("downloaded", tick, "visible", sync)
                )
        elif isinstance(child, Gtk.Label):
            if attr == "badge":
                child.set_text("NEW" if entry.is_new else "")
                if entry.is_new:
                    child.add_css_class("new-badge")
                else:
                    child.remove_css_class("new-badge")
            elif attr == "category":
                # Categories arrive after the scan.
                bindings.append(
                    entry.bind_property("category", child, "label", sync)
                )
                child.set_attributes(BOLD_ATTRS if entry.is_new else None)
            else:
                child.set_text(getattr(entry, attr))
                child.set_attributes(BOLD_ATTRS if entry.is_new else None)
        list_item.bindings = bindings

    def _on_catalog_cell_unbind(self, factory, list_item):
        for binding in getattr(list_item, "bindings", ()):
            binding.unbind()
        list_item.bindings = []

    def _on_catalog_cell_query_tooltip(self, widget, x, y, keyboard, tooltip):
        entry = getattr(widget, "catalog_entry", None)
        if entry is None:
            return False
        tooltip.set_text(entry.tooltip)
        return True

    # --- Column sorting ---

    def _on_view_sorter_changed(self, sorter, change):
        """A header was clicked: the column view's sorter has just made that
        column primary. Treat the click as a change to the numbered keys."""
        if self._clearing_view_sorter:
            return
        column = sorter.get_primary_sort_column()
        if column is None or column not in self._column_titles:
            return
        self._handle_header_click(
            column, add=self._modifier_held(Gdk.ModifierType.SHIFT_MASK)
        )
        # Empty the view's sorter again (outside this emission) so no header
        # shows GTK's own arrow and the next click on the same column
        # registers as a fresh click rather than a flip.
        GLib.idle_add(self._clear_view_sorter)

    def _clear_view_sorter(self):
        self._clearing_view_sorter = True
        try:
            self.catalog_view.sort_by_column(None, Gtk.SortType.ASCENDING)
        finally:
            self._clearing_view_sorter = False
        return False

    def _modifier_held(self, mask):
        """Whether the keyboard modifier(s) in `mask` are held right now."""
        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display is not None else None
        keyboard = seat.get_keyboard() if seat is not None else None
        if keyboard is None:
            return False
        return bool(keyboard.get_modifier_state() & mask)

    def _handle_header_click(self, column, add):
        """
        Plain click: sort by this column alone, or reverse it if it's
        already key 1 (secondary keys stay). Shift-click (add): append the
        column as the next key; if it's a key already, reverse it, and on
        the next click remove it.
        """
        asc, desc = Gtk.SortType.ASCENDING, Gtk.SortType.DESCENDING
        reverse = {asc: desc, desc: asc}
        default = self._default_orders[column]
        keys = self._sort_keys
        position = next(
            (i for i, (key_column, _) in enumerate(keys) if key_column is column),
            None,
        )

        if not add:
            if position == 0:
                keys[0] = (column, reverse[keys[0][1]])
            else:
                self._sort_keys = [(column, default)]
        elif position is None:
            keys.append((column, default))
        elif keys[position][1] == default:
            keys[position] = (column, reverse[default])
        else:
            del keys[position]
        self._apply_sort_keys()

    def _apply_sort_keys(self):
        """Sort the list by the numbered keys, in order, and label the
        headers."""
        combined = Gtk.MultiSorter()
        for column, order in self._sort_keys:
            combined.append(self._sorter_factories[column](order))
        self.catalog_sorted.set_sorter(combined)

        for column, title in self._column_titles.items():
            column.set_title(self._numbered_title(column, title))

    def _is_sort_key(self, title):
        return any(self._column_titles[c] == title for c, _ in self._sort_keys)

    def _numbered_title(self, column, title):
        for rank, (key_column, order) in enumerate(self._sort_keys, start=1):
            if key_column is column:
                arrow = "▲" if order == Gtk.SortType.ASCENDING else "▼"
                return f"{title} {rank}{arrow}"
        return title

    # --- Context menu ---

    def _build_catalog_menu(self):
        """Right-click menu for catalog rows. Its actions queue the rows
        recorded in _context_entries when the menu opened."""
        for name, force in (("queue-selected", False), ("force-download", True)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", self._on_context_queue, force)
            self.add_action(action)

        menu = Gio.Menu()
        menu.append("Add to Queue", "win.queue-selected")
        menu.append("Force Re-download", "win.force-download")
        self.catalog_menu = Gtk.PopoverMenu.new_from_model(menu)
        self.catalog_menu.set_parent(self.catalog_view)
        self.catalog_menu.set_has_arrow(False)
        self.catalog_menu.set_halign(Gtk.Align.START)

    def _on_catalog_right_click(self, gesture, n_press, x, y):
        entry = self._entry_at(x, y)
        position = self._position_of(entry) if entry is not None else None
        if position is None:
            return  # a header or the empty space below the rows
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

        # As in a file manager: a right-click on a selected row acts on the
        # whole selection, anywhere else it selects just that row first.
        if not self.catalog_selection.is_selected(position):
            self.catalog_selection.select_item(position, True)
        self._context_entries = self._selected_entries()

        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.catalog_menu.set_pointing_to(rect)
        self.catalog_menu.popup()

    def _entry_at(self, x, y):
        """The CatalogEntry of the row at view coordinates (x, y), or None."""
        widget = self.catalog_view.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget is not self.catalog_view:
            if widget.get_css_name() == "row":
                # Every cell of a row is bound to the same entry, so the
                # first cell's label (see _on_catalog_cell_bind) will do.
                cell = widget.get_first_child()
                label = cell.get_first_child() if cell is not None else None
                return getattr(label, "catalog_entry", None)
            widget = widget.get_parent()
        return None

    def _position_of(self, entry):
        """Row of `entry` in the list as shown, or None if filtered out."""
        model = self.catalog_selection
        for position in range(model.get_n_items()):
            item = model.get_item(position)
            if isinstance(item, CatalogEntry) and item.pid == entry.pid:
                return position
        return None

    def _on_context_queue(self, action, parameter, force):
        self._queue_entries(self._context_entries, force)

    def _build_downloads_bar(self):
        """Bottom bar: the download folder, the progress of the current
        download, and the pending queue behind a button."""
        bar = Gtk.ActionBar()

        self.download_dir_content = Adw.ButtonContent()
        self.download_dir_content.set_icon_name("folder-symbolic")
        self.download_dir_content.set_label("Default location")
        self.download_dir_button = Gtk.Button()
        self.download_dir_button.set_child(self.download_dir_content)
        self.download_dir_button.set_tooltip_text("Choose where downloads are saved")
        self.download_dir_button.connect("clicked", self.on_select_folder_clicked)
        bar.pack_start(self.download_dir_button)

        # Progress of the current download, kept to a readable width.
        progress = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        progress.set_valign(Gtk.Align.CENTER)
        progress.set_hexpand(True)
        progress_clamp = Adw.Clamp(maximum_size=640, tightening_threshold=480)
        progress_clamp.set_hexpand(True)
        progress_clamp.set_margin_start(12)
        progress_clamp.set_margin_end(12)
        progress_clamp.set_child(progress)
        bar.set_center_widget(progress_clamp)

        self.progress_label = Gtk.Label(xalign=0.0)
        self.progress_label.add_css_class("caption")
        self.progress_label.add_css_class("dim-label")
        self.progress_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        progress.append(self.progress_label)

        self.progress_bar = Gtk.ProgressBar()
        progress.append(self.progress_bar)

        # Queue button with the pending count; opens the list of pending
        # downloads.
        queue_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        queue_content.append(Gtk.Image.new_from_icon_name("folder-download-symbolic"))
        queue_content.append(Gtk.Label(label="Queue"))
        self.queue_badge = Gtk.Label()
        self.queue_badge.add_css_class("queue-badge")
        self.queue_badge.set_visible(False)
        queue_content.append(self.queue_badge)

        self.queue_button = Gtk.MenuButton()
        self.queue_button.set_child(queue_content)
        self.queue_button.set_popover(self._build_queue_popover())
        bar.pack_end(self.queue_button)

        self._set_progress("Idle", 0.0, active=False)
        return bar

    def _build_queue_popover(self):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        popover.set_child(box)

        heading = Gtk.Label(label="Pending Downloads", xalign=0.0)
        heading.add_css_class("heading")
        heading.set_margin_start(6)
        heading.set_margin_top(4)
        box.append(heading)

        self.queue_list_box = Gtk.ListBox()
        self.queue_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.queue_list_box.add_css_class("navigation-sidebar")

        queue_placeholder = Gtk.Label(label="No pending downloads")
        queue_placeholder.add_css_class("dim-label")
        queue_placeholder.set_margin_top(12)
        queue_placeholder.set_margin_bottom(12)
        self.queue_list_box.set_placeholder(queue_placeholder)

        # Sized to its content up to a limit, then scrolls.
        self.queue_scroll = Gtk.ScrolledWindow()
        self.queue_scroll.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self.queue_scroll.set_size_request(380, -1)
        self.queue_scroll.set_max_content_height(320)
        self.queue_scroll.set_propagate_natural_height(True)
        self.queue_scroll.set_child(self.queue_list_box)
        box.append(self.queue_scroll)
        return popover

    def _build_log_pane(self):
        self.log_scroll = Gtk.ScrolledWindow()
        self.log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.log_scroll.set_min_content_height(LOG_PANE_HEIGHT)

        self.log_text_view = Gtk.TextView()
        self.log_text_view.set_editable(False)
        self.log_text_view.set_cursor_visible(False)
        self.log_text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_text_view.set_monospace(True)
        self.log_text_view.add_css_class("log-view")
        self.log_text_view.set_left_margin(12)
        self.log_text_view.set_right_margin(12)
        self.log_text_view.set_top_margin(8)
        self.log_text_view.set_bottom_margin(8)
        self.log_scroll.set_child(self.log_text_view)

        self.log_buffer = self.log_text_view.get_buffer()
        self.log_end_mark = self.log_buffer.create_mark(
            "end_mark", self.log_buffer.get_end_iter(), False
        )
        return self.log_scroll

    # --- UI Helpers ---

    def _check_prerequisites(self):
        """Probe for required/optional tools, report to the log, and surface a
        banner (disabling scans) if a critical dependency is missing."""
        report = workers.check_prerequisites()
        self.add_to_log("Checking prerequisites...\n")

        blocking = []   # critical tools that are missing -> app unusable
        warnings = []   # required-but-non-critical tools missing -> degraded
        for item in report:
            status = "OK     " if item["found"] else "MISSING"
            tag = "required" if item["required"] else "optional"
            location = f" -> {item['path']}" if item["found"] else ""
            self.add_to_log(
                f"  [{status}] {item['name']} ({tag}){location}\n"
            )
            if not item["found"]:
                self.add_to_log(f"            {item['detail']}\n")
                if item["critical"]:
                    blocking.append(item["name"])
                elif item["required"]:
                    warnings.append(item["name"])

        if blocking:
            self._prereqs_ok = False
            names = ", ".join(blocking)
            self.prereq_banner.set_title(
                f"Missing required tool: {names}. Scanning, programme details "
                "and downloads are disabled — see the log."
            )
            self.prereq_banner.set_revealed(True)
            # Nothing but browsing the saved catalog works without get_iplayer.
            self.rescan_button.set_sensitive(False)
            self._set_catalog_status(
                f"Cannot scan channels: {names} is not installed."
            )
        elif warnings:
            names = ", ".join(warnings)
            self.prereq_banner.set_title(
                f"{names} not found. Browsing and previews work, "
                "but downloads will likely fail."
            )
            self.prereq_banner.set_revealed(True)
            self.add_to_log(
                "All critical prerequisites satisfied (with warnings above).\n"
            )
        else:
            self.add_to_log("All prerequisites satisfied.\n")

    def _restore_persisted_state(self):
        """Apply saved settings that need widgets, restore the unfinished
        download queue and show the previous run's catalog."""
        path = self.settings.get("download_path")
        if path:
            if os.path.isdir(path):
                self._apply_download_path(path)
            else:
                self.add_to_log(
                    f"Saved download folder no longer exists: {path}\n"
                )
                self.settings["download_path"] = None

        queue = self.settings.get("download_queue") or []
        if queue:
            self.download_queue = [dict(item) for item in queue]
            self.update_queue_display()
            self.add_to_log(
                f"Restored {len(queue)} unfinished download(s) from the "
                "last session.\n"
            )

        try:
            self.catalog_baseline = storage.load_catalog()
        except storage.StorageError as e:
            self.add_to_log(
                f"Could not read the saved catalog ({e}); "
                "starting a fresh one.\n"
            )
            self.catalog_baseline = {"scanned_at": None, "items": {}}
        self.catalog_saved = dict(self.catalog_baseline["items"])

        self.download_history = self._load_download_history()
        if self.download_history:
            self.add_to_log(
                f"get_iplayer's download history lists "
                f"{len(self.download_history)} programmes.\n"
            )

        if self.catalog_baseline["items"]:
            when = _format_iso(self.catalog_baseline["scanned_at"])
            count = len(self.catalog_baseline["items"])
            self.add_to_log(
                f"Loaded catalog from {when} ({count} programmes).\n"
            )
            # Browsable straight away; the scan replaces it with fresh
            # listings and the new-programme highlights.
            self._populate_catalog(
                (item, False) for item in self.catalog_saved.values()
            )
            if self._prereqs_ok:
                self._set_catalog_status(
                    f"Showing the catalog from {when} ({count:,} programmes)."
                )

    def _save_settings(self):
        """Persist settings, including whatever is still left to download."""
        unfinished = list(self.download_queue)
        if self.current_download:
            unfinished.insert(0, self.current_download)
        self.settings["download_queue"] = [
            {
                "pid": item["pid"],
                "title": item["title"],
                "force": bool(item.get("force")),
            }
            for item in unfinished
        ]
        try:
            storage.save_settings(self.settings)
        except storage.StorageError as e:
            self.add_to_log(f"Could not save settings: {e}\n")

    def _set_busy(self, busy):
        """Show/hide the header spinner using a counter so overlapping
        background tasks don't stop it prematurely."""
        if busy:
            self._busy_count += 1
            _spin(self.spinner, True)
        else:
            self._busy_count = max(0, self._busy_count - 1)
            if self._busy_count == 0:
                _spin(self.spinner, False)
        return False

    def _toast(self, text):
        self.toast_overlay.add_toast(Adw.Toast.new(text))

    def add_to_log(self, text):
        self.log_buffer.insert(self.log_buffer.get_end_iter(), text)
        self.log_text_view.scroll_to_mark(self.log_end_mark, 0.0, True, 0.0, 1.0)
        return False

    def clear_preview_pane(self):
        self._reset_preview()
        self.preview_stack.set_visible_child_name("empty")

    def _reset_preview(self):
        """Empty every part of the details page without switching to it."""
        self._set_preview_heading("", "")
        _remove_children(self.preview_chips)
        self.preview_desc.set_text("")
        self._set_preview_details([])
        self.preview_image.set_paintable(None)
        self.preview_image.set_visible(False)
        _spin(self.preview_spinner, False)

    def _set_preview_heading(self, name, episode):
        self.preview_title.set_text(name)
        self.preview_subtitle.set_text(episode)
        self.preview_subtitle.set_visible(bool(episode))

    def _set_preview_chips(self, entry):
        """Chips under the title: new, channel, type, category, downloaded."""
        _remove_children(self.preview_chips)
        chips = [
            ("New", "accent") if entry.is_new else None,
            (entry.channel, None),
            (entry.kind, None),
            (entry.category, None),
            ("Downloaded", "success") if entry.downloaded else None,
        ]
        for chip in chips:
            if chip is None or not chip[0]:
                continue
            label = Gtk.Label(label=chip[0])
            label.add_css_class("chip")
            if chip[1]:
                label.add_css_class(chip[1])
            self.preview_chips.append(label)

    def _set_preview_details(self, rows):
        """Rebuild the details card from an iterable of (key, value) pairs."""
        card = self.preview_details_card
        _remove_children(card)

        rows = list(rows)
        card.set_visible(bool(rows))
        # The keys share a width so the values line up.
        key_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for key, value in rows:
            key_label = Gtk.Label(label=_detail_title(key))
            key_label.add_css_class("dim-label")
            key_label.set_xalign(1.0)
            key_label.set_valign(Gtk.Align.START)
            key_label.set_justify(Gtk.Justification.RIGHT)
            key_label.set_wrap(True)
            key_label.set_max_width_chars(12)
            key_group.add_widget(key_label)

            value_label = Gtk.Label(label=value)
            value_label.set_hexpand(True)
            value_label.set_xalign(0.0)
            value_label.set_wrap(True)
            value_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            value_label.set_selectable(True)

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            row.append(key_label)
            row.append(value_label)
            card.append(row)

    def update_queue_display(self):
        """Rebuild the pending list in the queue popover, and the count on
        its button, from download_queue."""
        while child := self.queue_list_box.get_row_at_index(0):
            self.queue_list_box.remove(child)

        for item in self.download_queue:
            title = Gtk.Label(label=item["title"], xalign=0.0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            detail = item["pid"]
            if item.get("force"):
                detail += "  ·  re-download"
            subtitle = Gtk.Label(label=detail, xalign=0.0)
            subtitle.add_css_class("caption")
            subtitle.add_css_class("dim-label")
            subtitle.set_ellipsize(Pango.EllipsizeMode.END)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.append(title)
            box.append(subtitle)
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_child(box)
            self.queue_list_box.append(row)

        count = len(self.download_queue)
        self.queue_badge.set_text(str(count))
        self.queue_badge.set_visible(count > 0)
        if count == 0:
            self.queue_button.set_tooltip_text("No pending downloads")
        else:
            self.queue_button.set_tooltip_text(
                f"{count} pending download{'s' if count != 1 else ''}"
            )

    def _set_progress(self, text, fraction=None, active=True):
        """Status text (and, if given, fill) of the download progress. The
        bar itself only shows while a download is active."""
        self.progress_label.set_text(text)
        self.progress_label.set_tooltip_text(text)
        if fraction is not None:
            self.progress_bar.set_fraction(fraction)
        self.progress_bar.set_visible(active)

    def _apply_download_path(self, path):
        self.download_path = path
        self.download_dir_content.set_label(GLib.path_get_basename(path))
        self.download_dir_button.set_tooltip_text(f"Downloads are saved to {path}")

    # --- Event Handlers ---

    def _on_close_request(self, window):
        # A popover is a child of its parent widget without being in its
        # layout; detach it so the column view doesn't dispose with a
        # child left over.
        self.catalog_menu.unparent()
        # The sidebar comes back as it was, unless it was folded away by
        # the window being narrow.
        if not self.split_view.get_collapsed():
            self.settings["show_sidebar"] = self.split_view.get_show_sidebar()
        # Pending and in-progress downloads come back next time.
        self._save_settings()
        # Categories looked up so far are worth keeping if the lookup is
        # still running; the rest are fetched on the next scan.
        if self._scanning and self._category_done:
            self._save_catalog()
        return False

    def on_select_folder_clicked(self, button):
        dialog = Gtk.FileDialog(title="Select Download Folder", modal=True)
        dialog.select_folder(self, None, self._on_folder_selected)

    def _on_folder_selected(self, dialog, result):
        try:
            gfile = dialog.select_folder_finish(result)
        except GLib.Error:
            # Dismissed or failed - keep the previous location.
            return

        path = gfile.get_path() if gfile is not None else None
        if path is None:
            return

        self._apply_download_path(path)
        self.settings["download_path"] = path
        self._save_settings()

    # --- Search (local filter over the catalog) ---

    def _on_search_changed(self, entry):
        terms = _search_terms(entry.get_text())
        old = self._search_terms
        if terms == old:
            return
        self._search_terms = terms

        # Telling the filter model how the match set changed lets it re-test
        # only the rows now shown (typing) or only the hidden ones
        # (deleting) instead of the whole catalog on every keystroke.
        if _narrows(old, terms):
            change = Gtk.FilterChange.MORE_STRICT
        elif _narrows(terms, old):
            change = Gtk.FilterChange.LESS_STRICT
        else:
            change = Gtk.FilterChange.DIFFERENT
        self.catalog_filter.changed(change)

    def _on_stop_search(self, entry):
        # Escape clears the search.
        entry.set_text("")

    def _catalog_filter_func(self, entry, *_args):
        if self.settings["show_only_new"] and not entry.is_new:
            return False
        if self.settings["show_leaving_soon"]:
            start, end = self._leaving_soon_window
            if not start <= entry.expires_ts <= end:
                return False
        if entry.channel not in self.settings["scan_channels"]:
            return False
        if entry.kind in self._hidden_types:
            return False
        if entry.category in self._hidden_categories:
            return False
        return all(term in entry.search_text for term in self._search_terms)

    def _on_catalog_items_changed(self, model, position, removed, added):
        self._update_match_count()
        # Rows hidden by a filter change or replaced by a scan drop out of
        # the selection without a selection-changed signal.
        self._update_catalog_add_button()

    def _update_match_count(self):
        """The match count under the list, and the status page that
        stands in for the list while it is empty."""
        shown = self.catalog_filtered.get_n_items()
        total = self.catalog_store.get_n_items()
        if total == 0:
            text = ""
            self.catalog_empty_page.set_title("No Catalog Yet")
            self.catalog_empty_page.set_description(
                "Programmes appear here once the ticked channels have "
                "been scanned."
            )
        elif shown == total:
            text = f"{total:,} programmes"
        elif shown == 0:
            text = f"No matches among {total:,} programmes"
            self.catalog_empty_page.set_title("No Matching Programmes")
            self.catalog_empty_page.set_description(
                "Try another search, or tick more boxes in the filters."
            )
        else:
            text = f"{shown:,} of {total:,} programmes"
        self.match_count_label.set_text(text)
        self.catalog_stack.set_visible_child_name("list" if shown else "empty")

    # --- Preview ---

    def _on_catalog_selection_changed(self, model, position, n_items):
        self._update_catalog_add_button()

        entries = self._selected_entries()
        previous = self._selected_pids
        self._selected_pids = {entry.pid for entry in entries}

        if not entries:
            self._preview_pid = None
            self._info_request += 1  # drop any fetch still in flight
            self.clear_preview_pane()
            return

        added = [entry for entry in entries if entry.pid not in previous]
        if len(entries) == 1:
            target = entries[0]
        elif len(added) == 1:
            target = added[0]  # Ctrl-click: preview the row just added
        else:
            return  # Shift-click range: keep the current preview

        if target.pid != self._preview_pid:
            self._load_preview(target)

    def _load_preview(self, entry):
        """Show what the catalog knows at once, then fetch the full details
        and thumbnail from get_iplayer in the background."""
        self._preview_pid = entry.pid
        self._reset_preview()

        self._set_preview_heading(entry.name, entry.episode)
        self._set_preview_chips(entry)
        self.preview_desc.set_text(entry.desc)
        details = [("pid", entry.pid), ("channel", entry.channel)]
        if entry.categories:
            details.append(("categories", entry.categories))
        details.append(("available", entry.available))
        if entry.expires:
            details.append(("expires", entry.expires))
        details.extend(self._downloaded_details(entry))
        self._set_preview_details(details)
        self.preview_stack.set_visible_child_name("details")

        if not self._prereqs_ok:
            return  # get_iplayer is missing; the catalog data is all there is

        _spin(self.preview_spinner, True)
        self._set_busy(True)

        # A second click can start a second fetch before the first returns.
        # Tag this request so stale callbacks from an earlier fetch can't
        # overwrite the newer selection's preview.
        self._info_request += 1
        request_id = self._info_request

        thread = threading.Thread(
            target=workers.fetch_info,
            args=(
                entry.pid,
                self.add_to_log,
                partial(self._on_update_preview, request_id),
                partial(self._on_image_ready, request_id),
                partial(self._on_info_finished, request_id)
            )
        )
        thread.daemon = True
        thread.start()

    def _on_info_finished(self, request_id):
        """Completion callback for an info fetch. Every request releases the
        busy counter; only the newest one also stops the preview spinner."""
        self._set_busy(False)
        if request_id == self._info_request:
            _spin(self.preview_spinner, False)
        return False

    def _on_update_preview(self, request_id, info_data):
        if request_id != self._info_request:
            return False  # a newer selection superseded this fetch

        _spin(self.preview_spinner, False)
        name = info_data.pop("name", "Unknown Title")
        episode = info_data.pop("episode", "")
        desc = info_data.pop("desc", "No description available.")
        pid = info_data.pop("pid", "Unknown PID")

        # Clean up
        info_data.pop("thumbnail", None)

        self._set_preview_heading(name, episode)
        self.preview_desc.set_text(desc)
        details = [("pid", pid)]
        details.extend(
            (key, _detail_value(key, value)) for key, value in info_data.items()
        )
        entry = self._entries_by_pid.get(self._preview_pid)
        if entry is not None:
            self._set_preview_chips(entry)
            details.extend(self._downloaded_details(entry))
        self._set_preview_details(details)
        return False

    def _downloaded_details(self, entry):
        """Preview rows describing the programme's last download, if any."""
        if not entry.downloaded:
            return []
        text = entry.downloaded_when or "before"
        if entry.downloaded_file:
            text += f" to {entry.downloaded_file}"
            if not os.path.exists(entry.downloaded_file):
                text += " (file no longer there)"
        return [("downloaded", text)]

    def _on_image_ready(self, request_id, pixbuf):
        if request_id != self._info_request:
            return False  # a newer selection superseded this fetch

        # The Picture lives in the preview Paned's top child, so it has a
        # bounded allocation; CONTAIN + can_shrink scale the full-resolution
        # pixbuf to fit that sub-pane (keeping aspect ratio) without overflowing.
        try:
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            self.preview_image.set_paintable(texture)
            self.preview_image.set_visible(True)
        except Exception as e:
            self.add_to_log(f"Failed to display thumbnail: {e}\n")

        return False

    # --- Catalog scan ---

    def _set_catalog_status(self, text):
        self.catalog_status_label.set_text(text)
        self.catalog_status_label.set_tooltip_text(text)

    def _on_scan_channel_toggled(self, check):
        if self._syncing_checks:
            return
        self._solo_on_ctrl_click(check, self.channel_checks)
        self.settings["scan_channels"] = [
            channel for channel, widget in self.channel_checks.items()
            if widget.get_active()
        ]
        # Unticked channels are hidden straight away; they drop out of the
        # scan itself on the next Rescan.
        self.catalog_filter.changed(Gtk.FilterChange.DIFFERENT)
        self._save_settings()

    def _on_value_filter_toggled(self, check, key):
        """A Type or Category box was toggled; `key` names the settings
        list of hidden values ('hidden_types' or 'hidden_categories')."""
        if self._syncing_checks:
            return
        if key == "hidden_types":
            checks, hidden = self.type_checks, self._hidden_types
        else:
            checks, hidden = self.category_checks, self._hidden_categories
        self._solo_on_ctrl_click(check, checks)

        before = set(hidden)
        # Values without a box right now (categories absent from this
        # catalog) keep their setting for when they turn up again.
        hidden.difference_update(checks)
        hidden.update(value for value, box in checks.items()
                      if not box.get_active())
        self.settings[key] = sorted(hidden)

        if hidden > before:
            change = Gtk.FilterChange.MORE_STRICT
        elif hidden < before:
            change = Gtk.FilterChange.LESS_STRICT
        else:
            change = Gtk.FilterChange.DIFFERENT
        self.catalog_filter.changed(change)
        self._save_settings()

    def _solo_on_ctrl_click(self, check, checks):
        """Ctrl-click on a filter box ticks only that box, or every box
        again if it was already the only one ticked. The row is synced
        with the toggled handlers muted, so the caller applies the result
        once, from the boxes' final state."""
        if not self._modifier_held(Gdk.ModifierType.CONTROL_MASK):
            return
        solo = any(box.get_active() for box in checks.values() if box is not check)
        self._syncing_checks = True
        try:
            for box in checks.values():
                box.set_active(box is check or not solo)
        finally:
            self._syncing_checks = False

    def _on_only_new_toggled(self, check):
        only_new = check.get_active()
        self.settings["show_only_new"] = only_new
        self.catalog_filter.changed(
            Gtk.FilterChange.MORE_STRICT if only_new
            else Gtk.FilterChange.LESS_STRICT
        )
        self._save_settings()

    def _on_leaving_soon_toggled(self, check):
        leaving_soon = check.get_active()
        self.settings["show_leaving_soon"] = leaving_soon
        # "Soon" is counted from now, not from when the list was filled.
        self._leaving_soon_window = _leaving_soon_window()
        self.catalog_filter.changed(
            Gtk.FilterChange.MORE_STRICT if leaving_soon
            else Gtk.FilterChange.LESS_STRICT
        )
        self._save_settings()

    def _on_rescan_clicked(self, button):
        self._start_catalog_scan()

    def _start_catalog_scan(self):
        """Scan the ticked channels in the background and diff them against
        the catalog saved by the previous run."""
        if self._scanning:
            return

        channels = [
            channel for channel in catalog.SCAN_CHANNELS
            if channel in self.settings["scan_channels"]
        ]
        if not channels:
            self._set_catalog_status(
                "No channels selected. Tick at least one channel and click Rescan."
            )
            return

        self._scanning = True
        self._scan_channels = channels
        self._scan_results = {}

        self.rescan_button.set_sensitive(False)
        _spin(self.catalog_spinner, True)
        self._set_catalog_status(
            f"Scanning {len(channels)} channel(s)… The first scan in a few "
            "hours also refreshes get_iplayer's cache, which can take a while."
        )
        self.add_to_log(
            "Scanning channels for the catalog: " + ", ".join(channels) + "\n"
        )
        self._set_busy(True)

        thread = threading.Thread(
            target=workers.scan_channels,
            args=(
                channels,
                self.add_to_log,
                self._on_channel_scanned,
                self._on_scan_finished,
            )
        )
        thread.daemon = True
        thread.start()

    def _on_channel_scanned(self, channel, items):
        self._scan_results[channel] = items
        done = len(self._scan_results)
        total = len(self._scan_channels)
        result = "failed" if items is None else f"{len(items)} programmes"
        self._set_catalog_status(
            f"Scanning… {done}/{total} channels done ({channel}: {result})."
        )
        return False

    def _on_scan_finished(self):
        outcome = catalog.apply_scan(
            self.catalog_baseline["items"], self.catalog_saved,
            self._scan_results
        )
        self._last_outcome = outcome

        # The list shows the whole merged catalog - unticked or failed
        # channels keep their previous entries - with the scanned channels'
        # new programmes flagged. Downloads made outside the app since the
        # last read show up too, as the rows are built afresh.
        self.download_history = self._load_download_history()
        new_pids = {item["pid"] for item, is_new in outcome.entries if is_new}
        self._populate_catalog(
            (item, item["pid"] in new_pids) for item in outcome.merged.values()
        )

        # Only overwrite the saved catalog when at least one channel listed
        # successfully; otherwise the previous run's data is worth keeping.
        if outcome.entries:
            self.catalog_saved = outcome.merged
            self._save_catalog()

        self.add_to_log(
            f"Scan complete: {len(outcome.entries)} programmes, "
            f"{outcome.new_count} new.\n"
        )
        self._start_category_lookup()
        return False

    def _save_catalog(self):
        try:
            scanned_at = storage.save_catalog(self.catalog_saved)
            self.add_to_log(
                f"Catalog saved ({len(self.catalog_saved)} programmes) "
                f"at {_format_iso(scanned_at)}.\n"
            )
        except storage.StorageError as e:
            self.add_to_log(f"Could not save the catalog: {e}\n")

    def _start_category_lookup(self):
        """Second phase of a scan: fetch the category of every programme
        that hasn't had one looked up yet, then wrap the scan up."""
        pids = catalog.pids_without_category(self.catalog_saved)
        if not pids:
            self._finish_scan()
            return

        self._category_total = len(pids)
        self._category_done = 0
        self._set_catalog_status(
            f"Looking up categories for {len(pids):,} programmes…"
        )
        self.add_to_log(
            f"Looking up categories for {len(pids)} programmes on bbc.co.uk.\n"
        )
        thread = threading.Thread(
            target=workers.fetch_categories,
            args=(
                pids,
                self.add_to_log,
                self._on_category_batch,
                self._on_categories_finished,
            )
        )
        thread.daemon = True
        thread.start()

    def _on_category_batch(self, results):
        for pid, info in results.items():
            item = self.catalog_saved.get(pid)
            if item is not None:
                item.update(info)
            entry = self._entries_by_pid.get(pid)
            if entry is not None:
                entry.set_category(info["category"], info["categories"])
        self._category_done += len(results)
        self._set_catalog_status(
            f"Looking up categories… {self._category_done:,}/"
            f"{self._category_total:,} programmes."
        )
        return False

    def _on_categories_finished(self, failed):
        if failed:
            self.add_to_log(
                f"{failed} category request(s) failed; those programmes "
                "will be tried again on the next scan.\n"
            )
        if self._category_done:
            self._save_catalog()
            self._update_category_checks()
            # A row whose category just arrived may now fall under an
            # unticked box; otherwise the only effect on the filter is that
            # a search typed meanwhile can match on category text. A sort
            # by Category needs the values it didn't have.
            if self._hidden_categories:
                self.catalog_filter.changed(Gtk.FilterChange.DIFFERENT)
            elif self._search_terms:
                self.catalog_filter.changed(Gtk.FilterChange.LESS_STRICT)
            if self._is_sort_key("Category"):
                self._apply_sort_keys()
        self._finish_scan()
        return False

    def _finish_scan(self):
        self._scanning = False
        _spin(self.catalog_spinner, False)
        if self._last_outcome is not None:
            self._set_catalog_status(self._describe_scan(self._last_outcome))
        self._set_busy(False)
        self.rescan_button.set_sensitive(True)

    def _describe_scan(self, outcome):
        """Human-readable summary for the status line."""
        scanned = [c for c in self._scan_channels if c not in outcome.failed]
        total = len(outcome.entries)
        parts = []

        if not scanned:
            parts.append("The scan failed for every channel; see the log.")
        elif not self.catalog_baseline["items"]:
            parts.append(
                f"Catalog created: {total:,} programmes on "
                f"{len(scanned)} channel(s). New programmes will be "
                "highlighted from the next run."
            )
        else:
            since = _format_iso(self.catalog_baseline["scanned_at"])
            parts.append(
                f"{total:,} programmes on {len(scanned)} channel(s); "
                f"{outcome.new_count:,} new since {since}."
            )
            if outcome.unseen:
                parts.append(
                    f"First scan of {', '.join(outcome.unseen)}: its "
                    "programmes will be highlighted from the next run."
                )

        if outcome.failed:
            parts.append(
                f"Could not scan {', '.join(outcome.failed)} (see log)."
            )
        return " ".join(parts)

    def _populate_catalog(self, entries):
        """Replace the list with (item, is_new) pairs."""
        new_entries = [
            CatalogEntry(item, is_new, self.download_history.get(item["pid"]))
            for item, is_new in entries
        ]
        # The store's own order is the final tie-breaker for every sort.
        new_entries.sort(
            key=lambda entry: (entry.name.casefold(), entry.episode.casefold())
        )
        self._entries_by_pid = {entry.pid: entry for entry in new_entries}
        self._update_category_checks()
        # The rows are filtered afresh, so "soon" starts from now.
        self._leaving_soon_window = _leaving_soon_window()
        # The old rows leave the selection silently; forget them so the
        # next click is judged against an empty selection.
        self._selected_pids = set()
        # One splice keeps the filter/sort models to a single update.
        self.catalog_store.splice(
            0, self.catalog_store.get_n_items(), new_entries
        )
        self._update_catalog_add_button()

    # --- Download history ---

    def _load_download_history(self):
        """get_iplayer's download history as pid -> record; {} if it can't
        be read, with the reason logged the first time it occurs."""
        try:
            records = history.load_history()
        except OSError as e:
            if str(e) != self._history_error:
                self._history_error = str(e)
                self.add_to_log(
                    f"Could not read get_iplayer's download history: {e}\n"
                )
            return {}
        self._history_error = None
        return records

    def _refresh_download_history(self):
        """Re-read the history and update the rows whose state changed."""
        self.download_history = self._load_download_history()
        changed = False
        for pid, entry in self._entries_by_pid.items():
            record = self.download_history.get(pid)
            if record != entry.download_record:
                entry.set_downloaded(record)
                changed = True
        # The sort model doesn't notice property changes by itself.
        if changed and self._is_sort_key("Downloaded"):
            self._apply_sort_keys()
        # The preview's chips follow the programme it shows.
        entry = self._entries_by_pid.get(self._preview_pid)
        if changed and entry is not None:
            self._set_preview_chips(entry)

    # --- Queue Logic ---

    def _selected_entries(self):
        """The CatalogEntry objects currently selected, in list order."""
        selection = self.catalog_selection.get_selection()
        entries = []
        for i in range(selection.get_size()):
            entry = self.catalog_selection.get_item(selection.get_nth(i))
            if isinstance(entry, CatalogEntry):
                entries.append(entry)
        return entries

    def _update_catalog_add_button(self):
        selected = self.catalog_selection.get_selection().get_size()
        self.catalog_add_button.set_sensitive(selected > 0)

    def on_catalog_add_clicked(self, button):
        self._queue_entries(self._selected_entries(), force=False)

    def _queue_entries(self, entries, force):
        """
        Queue the given rows, skipping any already queued or downloading.

        With `force` a programme is downloaded again even though get_iplayer
        has it in its history or the file still exists; a plain queue entry
        for the same programme is upgraded to a re-download.
        """
        if not entries:
            return
        queued = {item["pid"]: item for item in self.download_queue}
        current = self.current_download

        added = 0
        upgraded = 0
        for entry in entries:
            if current and entry.pid == current["pid"]:
                self.add_to_log(f"{entry.pid} is downloading right now.\n")
                continue
            item = queued.get(entry.pid)
            if item is not None:
                if force and not item.get("force"):
                    item["force"] = True
                    upgraded += 1
                    self.add_to_log(
                        f"{entry.pid} is already queued; it will be "
                        "downloaded again.\n"
                    )
                else:
                    self.add_to_log(f"{entry.pid} is already queued.\n")
                continue
            item = {"pid": entry.pid, "title": entry.title_text, "force": force}
            queued[entry.pid] = item
            self.download_queue.append(item)
            added += 1
            what = "for re-download" if force else "to queue"
            self.add_to_log(f"Added {entry.pid} {what}.\n")

        if added or upgraded:
            self.update_queue_display()
            self._save_settings()
            self._process_queue()
        self._toast(self._describe_queued(entries, added, upgraded, force))

    @staticmethod
    def _describe_queued(entries, added, upgraded, force):
        """Toast text summarising a queue request."""
        def plural(count):
            return f"{count} programme{'s' if count != 1 else ''}"

        if added and force:
            text = f"Queued {plural(added)} for re-download"
        elif added:
            text = f"Added {plural(added)} to the queue"
        elif upgraded:
            text = f"Marked {plural(upgraded)} for re-download"
        elif len(entries) == 1:
            return "Already queued or downloading"
        else:
            return "All of those are already queued or downloading"
        skipped = len(entries) - added
        if skipped and added:
            text += f"; {skipped} already queued"
        return text

    def _process_queue(self):
        """
        Checks if a download is running. If not, starts the next one in queue.
        """
        if self.is_downloading:
            return

        if not self.download_queue:
            self._set_progress("All downloads finished", 0.0, active=False)
            return

        if not self._prereqs_ok:
            # Keep the queue intact rather than burning through it with
            # downloads that can't start.
            self.add_to_log(
                "Downloads are on hold until get_iplayer is installed.\n"
            )
            return

        # Pop the next item
        item = self.download_queue.pop(0)
        self.current_download = item
        self.update_queue_display()
        self._save_settings()

        pid = item['pid']
        title = item['title']
        force = bool(item.get("force"))

        self.is_downloading = True
        self._set_progress(f"Downloading {title}", 0.0)

        if force:
            self.add_to_log(
                f"Starting re-download for PID: {pid} (ignoring get_iplayer's "
                "history, overwriting any existing file)...\n"
            )
        else:
            self.add_to_log(f"Starting download for PID: {pid}...\n")
        if self.download_path:
            self.add_to_log(f"Download location: {self.download_path}\n")

        thread = threading.Thread(
            target=workers.download,
            args=(
                pid,
                self.download_path,
                self.add_to_log,
                self._on_download_progress,
                self._on_download_finished
            ),
            kwargs={"force": force},
        )
        thread.daemon = True
        thread.start()

    def _on_download_progress(self, percent, text):
        """Updates progress bar fraction and text."""
        # Use the full text status from get_iplayer (e.g., "54% - 12.3MB/s")
        self._set_progress(text, percent / 100.0)
        return False

    def _on_download_finished(self):
        """Called when a single download finishes."""
        item = self.current_download
        self.is_downloading = False
        self.current_download = None
        self._save_settings()
        # get_iplayer has added the programme to its history by now (if the
        # download succeeded), so the row can show its tick.
        before = self.download_history.get(item["pid"]) if item else None
        self._refresh_download_history()
        if item is not None:
            after = self.download_history.get(item["pid"])
            if after is not None and after != before:
                self._toast(f"Downloaded {item['title']}")
            else:
                self._toast(
                    f"get_iplayer finished without downloading "
                    f"{item['title']}; see the log"
                )
        # Process next item
        self._process_queue()
        return False
