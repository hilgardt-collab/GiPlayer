# window.py
import threading
import gi
from gi.repository import Gtk, Adw, GLib, Pango, Gdk, GdkPixbuf

import workers

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

        self.main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.main_vbox.append(self.header)
        self.set_content(self.main_vbox)

        # --- State Management ---
        self.download_path = None
        self.download_queue = []  # List of dicts: {'pid': str, 'title': str}
        self.is_downloading = False

        # --- File Chooser ---
        self.file_chooser = Gtk.FileChooserNative(
            title="Select Download Folder",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            transient_for=self,
            modal=True
        )
        self.file_chooser.connect("response", self.on_file_chooser_response)

        # --- Layout Panes ---
        self.main_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.main_paned.set_vexpand(True)
        self.main_vbox.append(self.main_paned)

        self.top_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.top_paned.set_resize_start_child(True)
        self.top_paned.set_wide_handle(True)
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

        self.download_dir_button = Gtk.Button(label="Select...")
        self.download_dir_button.connect("clicked", lambda x: self.file_chooser.show())
        self.download_hbox.append(self.download_dir_button)

        self.download_dir_display_label = Gtk.Label(label="<Default Location>")
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
        
        # Scroll for queue
        self.queue_scroll = Gtk.ScrolledWindow()
        self.queue_scroll.set_min_content_height(100)
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
        self.preview_scroll = Gtk.ScrolledWindow()
        self.preview_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.preview_scroll.set_hexpand(True)
        self.top_paned.set_end_child(self.preview_scroll)

        self.preview_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.preview_vbox.set_margin_top(10)
        self.preview_vbox.set_margin_bottom(10)
        self.preview_vbox.set_margin_start(10)
        self.preview_vbox.set_margin_end(10)
        self.preview_scroll.set_child(self.preview_vbox)

        self.preview_title = Gtk.Label()
        self.preview_title.set_markup("<b>Select a program to see details</b>")
        self.preview_title.set_halign(Gtk.Align.START)
        self.preview_title.set_wrap(True)
        self.preview_vbox.append(self.preview_title)

        self.preview_image = Gtk.Picture()
        self.preview_image.set_visible(False)
        self.preview_image.set_halign(Gtk.Align.CENTER)
        self.preview_vbox.append(self.preview_image)

        self.preview_desc = Gtk.Label()
        self.preview_desc.set_halign(Gtk.Align.START)
        self.preview_desc.set_wrap(True)
        self.preview_vbox.append(self.preview_desc)

        self.preview_details_label = Gtk.Label()
        self.preview_details_label.set_halign(Gtk.Align.START)
        self.preview_details_label.set_selectable(True)
        
        font_desc_details = Pango.FontDescription.from_string("monospace")
        attrs_details = Pango.AttrList()
        attrs_details.insert(Pango.attr_font_desc_new(font_desc_details))
        self.preview_details_label.set_attributes(attrs_details)
        self.preview_vbox.append(self.preview_details_label)

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

    # --- UI Helpers ---

    def add_to_log(self, text):
        self.log_buffer.insert(self.log_buffer.get_end_iter(), text)
        self.log_text_view.scroll_to_mark(self.log_end_mark, 0.0, True, 0.0, 1.0)
        return False

    def set_controls_sensitive(self, sensitive):
        self.search_entry.set_sensitive(sensitive)
        self.search_button.set_sensitive(sensitive)
        self.channel_entry.set_sensitive(sensitive)
        self.category_entry.set_sensitive(sensitive)
        self.results_list_box.set_sensitive(sensitive)
        self.download_dir_button.set_sensitive(sensitive)
        
        # Download button logic is now handled by queue status
        selected = self.results_list_box.get_selected_row() is not None
        self.add_queue_button.set_sensitive(sensitive and selected)
        
        self.preview_scroll.set_sensitive(sensitive)
        return False

    def clear_search_results(self):
        while child := self.results_list_box.get_row_at_index(0):
            self.results_list_box.remove(child)

    def clear_preview_pane(self):
        self.preview_title.set_markup("<b>Select a program to see details</b>")
        self.preview_desc.set_text("")
        self.preview_details_label.set_text("")
        self.preview_image.set_visible(False)

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

    def on_file_chooser_response(self, dialog, response_id):
        if response_id == Gtk.ResponseType.ACCEPT:
            gfile = self.file_chooser.get_file()
            self.download_path = gfile.get_path()
            display_name = GLib.path_get_basename(self.download_path)
            self.download_dir_display_label.set_text(display_name)
            self.download_dir_display_label.set_tooltip_text(self.download_path)
        dialog.hide()

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

        # Start Worker
        thread = threading.Thread(
            target=workers.search,
            args=(
                search_term, 
                channel, 
                category,
                self.add_to_log,             
                self._on_search_result_added, 
                lambda: self.set_controls_sensitive(True) 
            )
        )
        thread.daemon = True
        thread.start()

    def _on_search_result_added(self, line, pid, raw_line):
        label = Gtk.Label(label=line)
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)
        label.set_wrap(False)
        label.set_ellipsize(Pango.EllipsizeMode.END)

        font_desc = Pango.FontDescription.from_string("monospace")
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_font_desc_new(font_desc))
        label.set_attributes(attrs)

        label.pid = pid
        label.raw_line = raw_line
        # Hacky way to get the title: assume it's the text after the PID
        # line format: <pid>: <name> -- <episode> ...
        parts = line.split(':', 1)
        label.title_text = parts[1].strip() if len(parts) > 1 else pid

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
        pid = label.pid

        self.clear_preview_pane()
        self.preview_title.set_markup(f"<b>Loading info for {pid}...</b>")
        
        # Only disable search/filter controls, leave queue active
        self.search_entry.set_sensitive(False)
        self.search_button.set_sensitive(False)

        thread = threading.Thread(
            target=workers.fetch_info,
            args=(
                pid,
                self.add_to_log,
                self._on_update_preview,
                self._on_image_ready,
                lambda: self.set_controls_sensitive(True)
            )
        )
        thread.daemon = True
        thread.start()

    def _on_update_preview(self, info_data):
        name = info_data.pop("name", "Unknown Title")
        episode = info_data.pop("episode", "")
        desc = info_data.pop("desc", "No description available.")
        
        # Clean up
        info_data.pop("thumbnail", None)
        pid_v1 = info_data.pop("pid_v1", "Unknown PID")
        info_data.pop("pid", None)

        title = f"<b>{name}</b>"
        if episode:
            title += f"\n<i>{episode}</i>"

        details = f"pid:       {pid_v1}\n"
        for key, value in info_data.items():
            details += f"{key.ljust(9)}: {value}\n"

        self.preview_title.set_markup(title)
        self.preview_desc.set_text(desc)
        self.preview_details_label.set_text(details.strip())
        return False

    def _on_image_ready(self, pixbuf):
        try:
            container_width = self.preview_scroll.get_allocated_width()
            container_width -= 20  
            if container_width <= 0:
                container_width = 250

            img_width = pixbuf.get_width()
            img_height = pixbuf.get_height()
            scale = container_width / img_width
            new_height = int(img_height * scale)

            scaled_pixbuf = pixbuf.scale_simple(
                container_width, 
                new_height, 
                GdkPixbuf.InterpType.BILINEAR
            )
            
            texture = Gdk.Texture.new_for_pixbuf(scaled_pixbuf)
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
