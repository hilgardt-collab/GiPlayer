# **GiPlayer**

**GiPlayer** is a modern, lightweight graphical user interface (GUI) for the command-line tool get\_iplayer. Built with Python, GTK4, and Libadwaita, it provides a user-friendly way to search, browse, and download BBC television and radio programs.

## **Features**

* **Modern UI:** Built using GTK4 and Libadwaita for a native, responsive Linux look and feel.  
* **Search & Filter:** Easily search for programs and filter results by Channel (e.g., 'BBC One') or Category (e.g., 'Drama').  
* **Visual Previews:** View program details, episode descriptions, and thumbnail images before downloading.  
* **Download Queue:** Add multiple programs to a queue to download them sequentially.  
* **Real-time Progress:** Monitor download progress with a visual progress bar and detailed status text.  
* **Custom Download Location:** Choose exactly where you want your files to be saved.  
* **Non-Blocking:** Searches and downloads run in background threads, keeping the interface responsive at all times.

## **Prerequisites**

To run GiPlayer, you need the following installed on your system:

1. **Python 3**  
2. **get\_iplayer**: The core command-line tool must be installed and accessible in your system's PATH.  
   * *Ubuntu/Debian:* sudo apt install get-iplayer  
   * *Fedora:* sudo dnf install get\_iplayer  
3. **GTK4 and Libadwaita (Python Bindings)**:  
   * *Ubuntu/Debian:* sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1  
   * *Fedora:* sudo dnf install python3-gobject gtk4 adwaita-icon-theme libadwaita

## **Installation & Setup**

1. **Clone or Download** the repository containing the three source files:  
   * main.py (Entry point)  
   * window.py (User Interface logic)  
   * workers.py (Backend/Threading logic)  
2. **Make the entry point executable** (optional):  
   chmod \+x main.py

## **Usage**

1. Run the application from the terminal:  
   python3 main.py

   *(Or ./main.py if you made it executable)*  
2. **Search:** Enter a keyword (e.g., "Doctor Who") in the search bar and press Enter or click "Search". Defaults to \* to list everything available.  
3. **Preview:** Click on any result in the list to load its metadata and thumbnail image.  
4. **Queue:** Click "Add Selected to Queue". The download will start immediately if the queue is idle.  
5. **Download Location:** Click "Select..." to choose a folder. This setting persists for the session.

## **File Structure**

* main.py: Bootstrap script that initializes the GTK Application.  
* window.py: Handles the window layout, widgets, event listeners, and updates the UI.  
* workers.py: Contains the background threads that interact with get\_iplayer, parse output, and manage downloads via pseudo-terminals (PTY).

## **Troubleshooting**

* **"get\_iplayer command not found"**: Ensure get\_iplayer is installed and you can run it from a standard terminal window.  
* **No thumbnails**: Ensure your internet connection is active. The app downloads thumbnails directly from the BBC URLs provided by get\_iplayer.  
* **Progress bar not moving**: The app uses a pseudo-terminal (PTY) to force get\_iplayer to output progress. This feature is primarily designed for Linux/macOS environments.

## **License**

This project is released under the **GNU General Public License (GPL)**.  
This project is a simple front-end wrapper and is not affiliated with the BBC or the get\_iplayer project developers.