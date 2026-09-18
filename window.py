# window.py
import threading
from functools import partial

from gi.repository import Gtk, Adw, GLib, Pango, Gdk

import workers


class ResultLabel(Gtk.Label):
    """A Gtk.Label that also carries the search-result metadata for a row."""

    def __init__(self, pid, raw_line, title_text, **kwargs):
        super().__init__(**kwargs)
        self.pid = pid
        self.raw_line = raw_line
        self.title_text = title_text


class GiPlayerWindow(Adw.ApplicationWindow):
    """
    The main application window for GiPlayer.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.set_default_size(900, 700)

        # --- Header ---
        self.header = Adw.HeaderBar()
        self.header.set_show_end_title_buttons(True)
        self.title_widget = Adw.WindowTitle(title="GiPlayer")
        self.header.set_title_widget(self.title_widget)

        # Busy spinner in the header — shown while a background search or
        # info-fetch is running. A counter keeps it spinning until *all*
        # in-flight operations finish.
        self._busy_count = 0
        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        self.header.pack_start(self.spinner)

        self.main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.main_vbox.append(self.header)
        self.set_content(self.main_vbox)

        # Prerequisite warning banner (hidden unless a tool is missing).
        self.prereq_banner = Adw.Banner()
        self.prereq_banner.set_revealed(False)
        self.main_vbox.append(self.prereq_banner)

        self._prereqs_ok = True

        # --- State Management ---
        self.download_path = None
        self.download_queue = []  # List of dicts: {'pid': str, 'title': str}
        self.is_downloading = False
        # Incremented on every selection; callbacks from an older info fetch
        # compare against it and drop their results if superseded.
        self._info_request = 0

        # --- Layout Panes ---
        self.main_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.main_paned.set_vexpand(True)
        self.main_paned.set_position(460)
        self.main_vbox.append(self.main_paned)

        self.top_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.top_paned.set_resize_start_child(True)
        self.top_paned.set_wide_handle(True)
        self.top_paned.set_position(440)
        self.main_paned.set_start_child(self.top_paned)

        # --- Left Side (Search) ---
        self.left_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.left_vbox.set_margin_top(6)
        self.left_vbox.set_margin_bottom(6)
        self.left_vbox.set_margin_start(6)
        self.left_vbox.set_margin_end(6)
        self.left_vbox.set_hexpand(True)
        self.top_paned.set_start_child(self.left_vbox)

        # Search Grid
        self.search_grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        self.left_vbox.append(self.search_grid)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search for a program...")
        # --- Fix: Set default search term to '*' ---
        self.search_entry.set_text("*")
        # --- End Fix ---
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("activate", self.on_search_button_clicked)
        self.search_grid.attach(self.search_entry, 0, 0, 2, 1)

        self.search_button = Gtk.Button(label="Search")
        self.search_button.connect("clicked", self.on_search_button_clicked)
        self.search_grid.attach(self.search_button, 2, 0, 1, 1)

        # Filters
        self.channel_label = Gtk.Label(label="Channel:")
        self.channel_label.set_halign(Gtk.Align.END)
        self.search_grid.attach(self.channel_label, 0, 1, 1, 1)
        self.channel_entry = Gtk.Entry()
        self.channel_entry.set_hexpand(True)
        self.search_grid.attach(self.channel_entry, 1, 1, 2, 1)

        self.category_label = Gtk.Label(label="Category:")
        self.category_label.set_halign(Gtk.Align.END)
        self.search_grid.attach(self.category_label, 0, 2, 1, 1)
        self.category_entry = Gtk.Entry()
        self.category_entry.set_hexpand(True)
        self.search_grid.attach(self.category_entry, 1, 2, 2, 1)

        # Download Path UI
        self.download_dir_label = Gtk.Label(label="Download to:")
        self.download_dir_label.set_halign(Gtk.Align.END)
        self.search_grid.attach(self.download_dir_label, 0, 3, 1, 1)

        self.download_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.download_hbox.set_hexpand(True)
        self.search_grid.attach(self.download_hbox, 1, 3, 2, 1)

        self.download_dir_button = Gtk.Button(label="Select…")
        self.download_dir_button.connect("clicked", self.on_select_folder_clicked)
        self.download_hbox.append(self.download_dir_button)

        self.download_dir_display_label = Gtk.Label(label="Default location")
        self.download_dir_display_label.add_css_class("dim-label")
        self.download_dir_display_label.set_halign(Gtk.Align.START)
        self.download_dir_display_label.set_ellipsize(Pango.EllipsizeMode.START)
        self.download_hbox.append(self.download_dir_display_label)

        # Results List
        self.results_scroll = Gtk.ScrolledWindow()
        self.results_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.results_scroll.set_vexpand(True)
        self.left_vbox.append(self.results_scroll)

        self.results_list_box = Gtk.ListBox()
        self.results_list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.results_list_box.connect("row-selected", self.on_result_selected)
        self.results_scroll.set_child(self.results_list_box)

        # --- Queue & Progress Section ---
        self.queue_frame = Gtk.Frame(label="Download Queue")
        self.queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.queue_frame.set_child(self.queue_box)
        self.left_vbox.append(self.queue_frame)

        # Queue List (Using an Expander to save space)
        self.queue_expander = Gtk.Expander(label="Pending Items (0)")
        self.queue_list_box = Gtk.ListBox()
        self.queue_list_box.set_selection_mode(Gtk.SelectionMode.NONE)

        # Placeholder so an *empty* queue still has real content to lay out
        # when the expander is opened (an empty scroll area could otherwise
        # leave the expander with nothing to size against).
        queue_placeholder = Gtk.Label(label="No pending downloads")
        queue_placeholder.add_css_class("dim-label")
        queue_placeholder.set_margin_top(12)
        queue_placeholder.set_margin_bottom(12)
        self.queue_list_box.set_placeholder(queue_placeholder)

        # Scroll for queue — size to its natural content (bounded), rather than
        # forcing a fixed height the surrounding panes have to fight over.
        self.queue_scroll = Gtk.ScrolledWindow()
        self.queue_scroll.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self.queue_scroll.set_min_content_height(48)
        self.queue_scroll.set_max_content_height(200)
        self.queue_scroll.set_propagate_natural_height(True)
        self.queue_scroll.set_child(self.queue_list_box)

        self.queue_expander.set_child(self.queue_scroll)
        self.queue_box.append(self.queue_expander)

        # Progress Bar
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_text("Idle")
        self.queue_box.append(self.progress_bar)

        # Add to Queue Button
        self.add_queue_button = Gtk.Button(label="Add Selected to Queue")
        self.add_queue_button.set_sensitive(False)
        self.add_queue_button.connect("clicked", self.on_add_queue_clicked)
        self.queue_box.append(self.add_queue_button)

        # --- Right Side (Preview) ---
        # The preview is split vertically: the title sits at the top, then a
        # Paned divides the image (top) from the scrolling text (bottom). Giving
        # the image its own Paned child means it gets a *bounded* height (the
        # divider position) instead of doing height-for-width off the full pane
        # width — so Gtk.Picture scales the thumbnail to fit that sub-pane and
        # never balloons off-screen. The divider is user-draggable.
        self.preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.preview_box.set_margin_top(10)
        self.preview_box.set_margin_bottom(10)
        self.preview_box.set_margin_start(10)
        self.preview_box.set_margin_end(10)
        self.preview_box.set_hexpand(True)
        self.top_paned.set_end_child(self.preview_box)

        self.preview_title = Gtk.Label()
        self.preview_title.set_markup("<b>Select a program to see details</b>")
        self.preview_title.set_halign(Gtk.Align.START)
        self.preview_title.set_wrap(True)
        self.preview_box.append(self.preview_title)

        # In-pane spinner shown while a program's details are being fetched.
        self.preview_spinner = Gtk.Spinner()
        self.preview_spinner.set_halign(Gtk.Align.CENTER)
        self.preview_spinner.set_size_request(32, 32)
        self.preview_spinner.set_visible(False)
        self.preview_box.append(self.preview_spinner)

        # Vertical split: image on top, text below.
        self.preview_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.preview_paned.set_vexpand(True)
        self.preview_paned.set_position(240)
        self.preview_box.append(self.preview_paned)

        # Thumbnail. Filling its Paned child (CONTAIN + can_shrink) makes the
        # Picture scale the image to fit the bounded space, keeping aspect ratio
        # and re-scaling as the divider/pane moves.
        self.preview_image = Gtk.Picture()
        self.preview_image.set_visible(False)
        self.preview_image.set_can_shrink(True)
        self.preview_image.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.preview_image.set_hexpand(True)
        self.preview_paned.set_start_child(self.preview_image)
        self.preview_paned.set_resize_start_child(True)
        self.preview_paned.set_shrink_start_child(True)

        # Description + details live in a scroller so long metadata never pushes
        # the thumbnail off-screen.
        self.preview_text_scroll = Gtk.ScrolledWindow()
        self.preview_text_scroll.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self.preview_text_scroll.set_min_content_height(100)
        self.preview_paned.set_end_child(self.preview_text_scroll)
        self.preview_paned.set_resize_end_child(True)
        self.preview_paned.set_shrink_end_child(True)

        self.preview_text_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        self.preview_text_scroll.set_child(self.preview_text_box)

        self.preview_desc = Gtk.Label()
        self.preview_desc.set_halign(Gtk.Align.START)
        self.preview_desc.set_wrap(True)
        self.preview_text_box.append(self.preview_desc)

        # Metadata as a two-column key/value grid. Every value label wraps, so
        # a long single-line field (get_iplayer prints desclong as one line)
        # can't inflate the pane's minimum width. A non-wrapping label here
        # made the preview box wider than the window, which pushed the
        # thumbnail off-screen to the right.
        self.preview_details_grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        self.preview_details_grid.set_hexpand(True)
        self.preview_text_box.append(self.preview_details_grid)

        # --- Bottom Panel (Log) ---
        self.log_scroll = Gtk.ScrolledWindow()
        self.log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.log_scroll.set_vexpand(True)
        self.main_paned.set_end_child(self.log_scroll)

        self.log_text_view = Gtk.TextView()
        self.log_text_view.set_editable(False)
        self.log_text_view.set_cursor_visible(False)
        self.log_text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_text_view.set_monospace(True)
        self.log_scroll.set_child(self.log_text_view)

        self.log_buffer = self.log_text_view.get_buffer()
        self.log_end_mark = self.log_buffer.create_mark(
            "end_mark", self.log_buffer.get_end_iter(), False
        )

        # Verify external tools are present before the user tries to use them.
        self._check_prerequisites()

    # --- UI Helpers ---

    def _check_prerequisites(self):
        """Probe for required/optional tools, report to the log, and surface a
        banner (disabling search) if a critical dependency is missing."""
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
                f"Missing required tool: {names}. "
                "Searching and downloading are disabled — see the log below."
            )
            self.prereq_banner.set_revealed(True)
            # Disable the search controls; nothing will work without get_iplayer.
            self.search_entry.set_sensitive(False)
            self.search_button.set_sensitive(False)
            self.channel_entry.set_sensitive(False)
            self.category_entry.set_sensitive(False)
        elif warnings:
            names = ", ".join(warnings)
            self.prereq_banner.set_title(
                f"{names} not found. Searching and previews work, "
                "but downloads will likely fail."
            )
            self.prereq_banner.set_revealed(True)
            self.add_to_log(
                "All critical prerequisites satisfied (with warnings above).\n"
            )
        else:
            self.add_to_log("All prerequisites satisfied.\n")

    def _set_busy(self, busy):
        """Show/hide the header spinner using a counter so overlapping
        background tasks don't stop it prematurely."""
        if busy:
            self._busy_count += 1
            self.spinner.set_visible(True)
            self.spinner.start()
        else:
            self._busy_count = max(0, self._busy_count - 1)
            if self._busy_count == 0:
                self.spinner.stop()
                self.spinner.set_visible(False)
        return False

    def _on_worker_finished(self):
        """Shared completion callback for search / info worker threads."""
        self.set_controls_sensitive(True)
        self._set_busy(False)
        return False

    def add_to_log(self, text):
        self.log_buffer.insert(self.log_buffer.get_end_iter(), text)
        self.log_text_view.scroll_to_mark(self.log_end_mark, 0.0, True, 0.0, 1.0)
        return False

    def set_controls_sensitive(self, sensitive):
        # Never re-enable search controls if a critical prerequisite is missing.
        search_sensitive = sensitive and self._prereqs_ok
        self.search_entry.set_sensitive(search_sensitive)
        self.search_button.set_sensitive(search_sensitive)
        self.channel_entry.set_sensitive(search_sensitive)
        self.category_entry.set_sensitive(search_sensitive)
        self.results_list_box.set_sensitive(sensitive)
        self.download_dir_button.set_sensitive(sensitive)

        # Download button logic is now handled by queue status
        selected = self.results_list_box.get_selected_row() is not None
        self.add_queue_button.set_sensitive(sensitive and selected)

        self.preview_box.set_sensitive(sensitive)
        return False

    def clear_search_results(self):
        while child := self.results_list_box.get_row_at_index(0):
            self.results_list_box.remove(child)

    def clear_preview_pane(self):
        self.preview_title.set_markup("<b>Select a program to see details</b>")
        self.preview_desc.set_text("")
        self._set_preview_details([])
        self.preview_image.set_paintable(None)
        self.preview_image.set_visible(False)
        self.preview_spinner.stop()
        self.preview_spinner.set_visible(False)

    def _set_preview_details(self, rows):
        """Rebuild the details grid from an iterable of (key, value) pairs."""
        grid = self.preview_details_grid
        while child := grid.get_first_child():
            grid.remove(child)

        for i, (key, value) in enumerate(rows):
            key_label = Gtk.Label(label=key)
            key_label.add_css_class("dim-label")
            key_label.set_halign(Gtk.Align.END)
            key_label.set_valign(Gtk.Align.START)
            grid.attach(key_label, 0, i, 1, 1)

            value_label = Gtk.Label(label=value)
            value_label.set_hexpand(True)
            value_label.set_xalign(0.0)
            value_label.set_wrap(True)
            value_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            value_label.set_selectable(True)
            grid.attach(value_label, 1, i, 1, 1)

    def update_queue_display(self):
        """Updates the listbox in the expander to match the download_queue list."""
        # Clear current list
        while child := self.queue_list_box.get_row_at_index(0):
            self.queue_list_box.remove(child)

        for item in self.download_queue:
            label = Gtk.Label(label=f"{item['pid']} - {item['title']}")
            label.set_halign(Gtk.Align.START)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            row = Gtk.ListBoxRow()
            row.set_child(label)
            self.queue_list_box.append(row)

        count = len(self.download_queue)
        self.queue_expander.set_label(f"Pending Items ({count})")

    # --- Event Handlers ---

    def on_select_folder_clicked(self, button):
        dialog = Gtk.FileDialog(title="Select Download Folder", modal=True)
        dialog.select_folder(self, None, self._on_folder_selected)

    def _on_folder_selected(self, dialog, result):
        try:
            gfile = dialog.select_folder_finish(result)
        except GLib.Error:
            # Dismissed or failed — keep the previous location.
            return

        path = gfile.get_path() if gfile is not None else None
        if path is None:
            return

        self.download_path = path
        self.download_dir_display_label.remove_css_class("dim-label")
        self.download_dir_display_label.set_text(GLib.path_get_basename(path))
        self.download_dir_display_label.set_tooltip_text(path)

    def on_search_button_clicked(self, widget):
        search_term = self.search_entry.get_text().strip()
        if not search_term:
            self.add_to_log("Please enter a search term.\n")
            return

        channel = self.channel_entry.get_text().strip()
        category = self.category_entry.get_text().strip()

        self.add_to_log(f"Searching for '{search_term}'...\n")
        self.clear_search_results()
        self.clear_preview_pane()
        self.set_controls_sensitive(False)
        self._set_busy(True)

        # Start Worker
        thread = threading.Thread(
            target=workers.search,
            args=(
                search_term,
                channel,
                category,
                self.add_to_log,
                self._on_search_result_added,
                self._on_worker_finished
            )
        )
        thread.daemon = True
        thread.start()

    def _on_search_result_added(self, line, pid, raw_line):
        # Hacky way to get the title: assume it's the text after the PID
        # line format: <pid>: <name> -- <episode> ...
        parts = line.split(':', 1)
        title_text = parts[1].strip() if len(parts) > 1 else pid

        label = ResultLabel(pid=pid, raw_line=raw_line, title_text=title_text,
                            label=line)
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)
        label.set_wrap(False)
        label.set_ellipsize(Pango.EllipsizeMode.END)

        font_desc = Pango.FontDescription.from_string("monospace")
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_font_desc_new(font_desc))
        label.set_attributes(attrs)

        row = Gtk.ListBoxRow()
        row.set_child(label)
        self.results_list_box.append(row)
        return False

    def on_result_selected(self, list_box, row):
        if not row:
            self.add_queue_button.set_sensitive(False)
            return

        self.add_queue_button.set_sensitive(True)
        label = row.get_child()
        if not isinstance(label, ResultLabel):
            return
        pid = label.pid

        self.clear_preview_pane()
        safe_pid = GLib.markup_escape_text(pid)
        self.preview_title.set_markup(f"<b>Loading info for {safe_pid}…</b>")
        self.preview_spinner.set_visible(True)
        self.preview_spinner.start()

        # Only disable search/filter controls, leave queue active
        self.search_entry.set_sensitive(False)
        self.search_button.set_sensitive(False)
        self._set_busy(True)

        # The results list stays clickable during a fetch, so a second click
        # can start a second thread. Tag this request so stale callbacks from
        # an earlier fetch can't overwrite the newer selection's preview.
        self._info_request += 1
        request_id = self._info_request

        thread = threading.Thread(
            target=workers.fetch_info,
            args=(
                pid,
                self.add_to_log,
                partial(self._on_update_preview, request_id),
                partial(self._on_image_ready, request_id),
                partial(self._on_info_finished, request_id)
            )
        )
        thread.daemon = True
        thread.start()

    def _on_info_finished(self, request_id):
        """Completion callback for an info fetch. A superseded request only
        releases the busy counter; the newest one also re-enables controls."""
        self._set_busy(False)
        if request_id == self._info_request:
            self.set_controls_sensitive(True)
        return False

    def _on_update_preview(self, request_id, info_data):
        if request_id != self._info_request:
            return False  # a newer selection superseded this fetch

        self.preview_spinner.stop()
        self.preview_spinner.set_visible(False)
        name = info_data.pop("name", "Unknown Title")
        episode = info_data.pop("episode", "")
        desc = info_data.pop("desc", "No description available.")
        pid = info_data.pop("pid", "Unknown PID")

        # Clean up
        info_data.pop("thumbnail", None)

        title = f"<b>{GLib.markup_escape_text(name)}</b>"
        if episode:
            title += f"\n<i>{GLib.markup_escape_text(episode)}</i>"

        self.preview_title.set_markup(title)
        self.preview_desc.set_text(desc)
        self._set_preview_details([("pid", pid), *info_data.items()])
        return False

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

    # --- Queue Logic ---

    def on_add_queue_clicked(self, widget):
        selected_row = self.results_list_box.get_selected_row()
        if not selected_row:
            return

        label = selected_row.get_child()
        if not isinstance(label, ResultLabel):
            return
        pid = label.pid
        title = label.title_text

        self.download_queue.append({'pid': pid, 'title': title})
        self.update_queue_display()
        self.add_to_log(f"Added {pid} to queue.\n")

        # Trigger queue processing if idle
        self._process_queue()

    def _process_queue(self):
        """
        Checks if a download is running. If not, starts the next one in queue.
        """
        if self.is_downloading:
            return

        if not self.download_queue:
            self.progress_bar.set_text("Queue Finished")
            self.progress_bar.set_fraction(0.0)
            return

        # Pop the next item
        item = self.download_queue.pop(0)
        self.update_queue_display()

        pid = item['pid']
        title = item['title']

        self.is_downloading = True
        self.progress_bar.set_text(f"Downloading: {title}")
        self.progress_bar.set_fraction(0.0)

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
            )
        )
        thread.daemon = True
        thread.start()

    def _on_download_progress(self, percent, text):
        """Updates progress bar fraction and text."""
        fraction = percent / 100.0
        self.progress_bar.set_fraction(fraction)
        # Use the full text status from get_iplayer (e.g., "54% - 12.3MB/s")
        self.progress_bar.set_text(text)
        return False

    def _on_download_finished(self):
        """Called when a single download finishes."""
        self.is_downloading = False
        # Process next item
        self._process_queue()
        return False
