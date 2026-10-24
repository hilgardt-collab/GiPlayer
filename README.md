# **GiPlayer**

**GiPlayer** is a modern, lightweight graphical user interface (GUI) for the command-line tool get\_iplayer. Built with Python, GTK4, and Libadwaita, it provides a user-friendly way to browse, search, and download BBC television programmes.

## **Features**

* **Modern UI:** Built using GTK4 and Libadwaita for a native, responsive Linux look and feel.  
* **Catalog Browser:** The programmes on the ticked BBC channels are listed in a table (New, Downloaded, Channel, Type, Category, Name, Episode, Available). Searching filters that list as you type, so no get_iplayer query runs per search.  
* **Filters:** Rows of tick boxes above the list narrow it by channel, by type (Series or One-off) and by category; the category row lists every category present in the catalog. Ctrl-click a box to show only that one, and Ctrl-click it again to show all of them.  
* **Multi-column Sorting:** Column headers are numbered: the list is sorted by key 1, ties by key 2, and so on. Click a header to sort by it, Shift-click to add further keys.  
* **Visual Previews:** View program details, episode descriptions, and thumbnail images before downloading.  
* **Download Queue:** Select one or more programmes and add them to a queue that downloads them sequentially. A right-click menu offers the same, plus a forced re-download of a programme get_iplayer has already fetched.  
* **Downloaded Before:** Programmes in get_iplayer's download history, whether fetched by GiPlayer or from the command line, carry a tick in the Downloaded column, and the preview shows when and to which file. The column sorts by download date, most recent first.  
* **Real-time Progress:** Monitor download progress with a visual progress bar and detailed status text.  
* **Custom Download Location:** Choose exactly where you want your files to be saved.  
* **What's New:** On every launch the ticked channels (BBC One, BBC Two, BBC Three, BBC Four, BBC Scotland, BBC Alba) are scanned into a catalog and compared with the catalog from the previous run. Programmes that weren't there last time are marked NEW and listed first; a tick box narrows the list to just those.  
* **Leaving Soon:** Another tick box narrows the list to programmes that expire from iPlayer within the next 3 days, so they can be grabbed before they go.  
* **Remembered Settings:** The download folder, the channel, type and category tick boxes, the "new" and "leaving soon" filters and any unfinished downloads are saved and restored on the next run.  
* **Non-Blocking:** Scans, previews and downloads run in background threads, keeping the interface responsive at all times.

## **Prerequisites**

To run GiPlayer, you need the following installed on your system:

1. **Python 3**  
2. **get\_iplayer**: The core command-line tool must be installed and accessible in your system's PATH.  
   * *Ubuntu/Debian:* sudo apt install get-iplayer  
   * *Fedora:* sudo dnf install get\_iplayer  
3. **GTK4 and Libadwaita (Python Bindings)**:  
   * *Ubuntu/Debian:* sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1  
   * *Fedora:* sudo dnf install python3-gobject gtk4 adwaita-icon-theme libadwaita  
   * libadwaita 1.7 or newer packs the filter tick boxes tightly; on an older version they are laid out in uniform columns and take a few more lines.

## **Installation & Setup**

1. **Clone or Download** the repository containing the source files:  
   * main.py (Entry point)  
   * window.py (User Interface logic)  
   * workers.py (Backend/Threading logic)  
   * catalog.py (Channel catalog parsing and diff logic)  
   * storage.py (Settings and catalog persistence)  
   * history.py (Reads get_iplayer's download history)  
2. **Make the entry point executable** (optional):  
   chmod \+x main.py

## **Usage**

1. Run the application from the terminal:  
   python3 main.py

   *(Or ./main.py if you made it executable)*  
2. **Browse:** The catalog from the previous run appears straight away, and the ticked channels are scanned in the background; the status line reports progress. Programmes not present in the previous run's catalog are marked NEW, shown in bold and sorted to the top.  
   * **Type** is derived from the listing: "Series" for anything with an episode title or series/episode numbering, "One-off" for standalone programmes such as films and single documentaries.  
   * **Category** is the same headline genre get_iplayer prints for `--info` (Drama, Comedy, Factual, News, …), and the preview lists the full set. get_iplayer's cache carries none, so after each scan the app reads the programme's metadata from bbc.co.uk for every programme that hasn't been looked up yet (a few requests at a time) and remembers the answer in the catalog. The first lookup covers the whole catalog and takes about a minute; later scans only look up new programmes. Programmes the BBC has no record of are left blank.  
   * **Sorting:** Headers are numbered in sort order, e.g. "New 1▲", "Available 2▼": rows are ordered by key 1, ties by key 2, and so on; remaining ties are alphabetical by name. Click a header to sort by that column alone (click again to reverse). Shift-click a header to add it as the next key; Shift-click a numbered key to reverse it, and once more to remove it.  
   * The very first scan only creates the catalog; highlighting starts from the second run. The same applies to a channel the first time it is ticked.  
   * **Downloaded** shows a tick for every programme in get_iplayer's download history, including downloads made outside GiPlayer. Those are the programmes get_iplayer refuses to download again unless they are queued with Force Re-download. Hover a row to see the download date. The tick appears as soon as a download finishes; a download made from the command line while the app is open shows up after the next Rescan.  
3. **Search:** Type in the search box to filter the list. Every word must appear in the programme's name, episode, description, categories or PID; matching is case-insensitive. Press Escape to clear the search.  
4. **Channels, Type and Category:** Untick a channel to hide its programmes and leave it out of the next scan; tick it again to bring them back. Click "Rescan" to scan the ticked channels again. The Type row hides or shows Series and One-off programmes, and the Category row has a box for every category in the catalog, plus "Uncategorised" for programmes whose category hasn't been looked up yet or that the BBC has no record of; categories that appear after a later scan start out ticked. Ctrl-click any box in the three rows to show only that channel, type or category (for a channel, the next scan is narrowed the same way), and Ctrl-click it again to show all of them. Tick "Only new programmes" to hide everything that was already in the catalog, and "Leaving soon" to show only programmes that expire within the next 3 days (programmes that have already expired are not shown). All of these combine with each other and with a search.  
5. **Preview:** Click a programme to see its details, including its expiry date, and thumbnail in the pane on the right. A programme downloaded before also shows the date of its last download and the file it went to, with a note if that file is no longer there. The catalog's own data shows immediately and the full details follow from get_iplayer.  
6. **Queue:** Select one or more programmes (Ctrl-click or Shift-click for several) and click "Add Selected to Queue". Downloads start immediately if the queue is idle.  
   * **Right-click** a programme for a context menu with "Add to Queue" and "Force Re-download". Right-clicking inside a selection acts on the whole selection; anywhere else it acts on that programme alone.  
   * **Force Re-download:** get_iplayer skips programmes it has downloaded before (those with a tick in the Downloaded column, which are in its download history) and files that already exist. Force Re-download runs it with `--force --overwrite`, so the programme is fetched again and an existing file in the download folder is replaced. Re-downloads are marked in the pending list, and a programme already waiting in the queue is upgraded to one.  
7. **Download Location:** Click "Select…" in the Downloads area to choose a folder. The choice is remembered between runs.  
8. **Unfinished Downloads:** Anything still queued or in progress when the app closes is restored to the queue, and resumed, on the next launch.

## **File Structure**

* main.py: Bootstrap script that initializes the GTK Application.  
* window.py: Handles the window layout, widgets, event listeners, and updates the UI.  
* workers.py: Contains the background threads that interact with get\_iplayer (channel scans, programme details, downloads via pseudo-terminals (PTY)) and parse their output.  
* catalog.py: The get\_iplayer listing format used for channel scans, parsing, programme classification, and the diff against the previous catalog.  
* storage.py: Reads and writes the settings and catalog files.  
* history.py: Reads get_iplayer's download history for the Downloaded column and the preview.

## **Data Files**

* Settings (download folder, scan channels, unticked types and categories, filter tick boxes, unfinished downloads): ~/.config/giplayer/settings.json  
* Catalog from the last scan, including the looked-up categories: ~/.local/share/giplayer/catalog.json  
* get_iplayer's download history, read for the Downloaded column and never written: ~/.get_iplayer/download_history (or download_history in the directory named by GETIPLAYER_PROFILE / GETIPLAYERUSERPREFS, as get_iplayer itself does)  

Delete catalog.json to start the new-programme comparison afresh (the categories are then looked up again).

## **Troubleshooting**

* **"get\_iplayer command not found"**: Ensure get\_iplayer is installed and you can run it from a standard terminal window.  
* **No thumbnails**: Ensure your internet connection is active. The app downloads thumbnails directly from the BBC URLs provided by get\_iplayer.  
* **Progress bar not moving**: The app uses a pseudo-terminal (PTY) to force get\_iplayer to output progress. This feature is primarily designed for Linux/macOS environments.

## **License**

This project is released under the **GNU General Public License (GPL)**.  
This project is a simple front-end wrapper and is not affiliated with the BBC or the get\_iplayer project developers.