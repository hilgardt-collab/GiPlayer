#!/usr/bin/env python3
import sys
import gi

# Specify versions *before* importing Gtk/Adw
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Adw  # noqa: E402
from window import GiPlayerWindow  # noqa: E402

class GiPlayerApp(Adw.Application):
    """The main GTK Application class."""

    def __init__(self, **kwargs):
        super().__init__(application_id="com.example.giplayer", **kwargs)
        self.window = None

    def do_activate(self):
        """Called when the application is activated."""
        if not self.window:
            self.window = GiPlayerWindow(application=self)
        self.window.present()

if __name__ == "__main__":
    app = GiPlayerApp()
    exit_status = app.run(sys.argv)
    sys.exit(exit_status)
