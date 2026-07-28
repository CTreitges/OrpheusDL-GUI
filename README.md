# OrpheusDL GUI

<img src="https://raw.githubusercontent.com/bascurtiz/OrpheusDL-GUI/main/animated.gif" alt="GUI overview">

> **This fork adds a Hi-Res Suite** on top of upstream `v2.0.3`: a persistent
> **download queue**, a **TIDAL playlist browser**, and **Spotify → TIDAL**
> playlist conversion that downloads your Spotify playlists from TIDAL in
> hi-res (24 bit FLAC). See **[HIRES.md](HIRES.md)**.
>
> Upstream project: [bascurtiz/OrpheusDL-GUI](https://github.com/bascurtiz/OrpheusDL-GUI)

## Trailer
Watch trailer: https://youtu.be/RAXsW67SjGU

## How to install:

1. Download the compiled installer from the [Releases page](https://github.com/bascurtiz/OrpheusDL-GUI/releases).
2. Double-click the exe/app
   - Windows - Demo how to install: https://youtu.be/tMG7nhIdy34 *(1m4s)*
   - macOS - Demo how to install: https://youtu.be/VdDNldZc-mI *(1m54s)*
   - macOS - For older macOS / if Homebrew isn't supported: https://youtu.be/x_5IZyUFTbM *(1m30s)*
   - Linux - Demo to install: https://youtu.be/MwGeAFFH-Sg *(1m8s)*

### If you prefer running from source:
0. Make sure [OrpheusDL](https://github.com/bascurtiz/OrpheusDL) is installed
1. Clone this repository (`git clone https://github.com/bascurtiz/OrpheusDL-GUI`) or download the ZIP file.
2. Ensure all files from this repository are placed in the same folder where your `orpheus.py` is located.
3. Update your package list: `sudo apt update`
4. Install the Python virtual environment and Tkinter packages: `sudo apt install python3-venv python3-tk`
5. Create a virtual environment: `python3 -m venv venv`
6. Activate the virtual environment: `source venv/bin/activate`
7. Install the required dependencies: `pip3 install -r requirements-gui.txt`
8. Ensure **FFmpeg** is installed (usually includes `ffplay`).
   - Ubuntu/Debian: `sudo apt install ffmpeg`
   - Fedora: `sudo dnf install ffmpeg`
   - Arch: `sudo pacman -S ffmpeg`
9. Run the GUI: `python3 gui.py`<br>
<br>

## Compatibility

### Operating Systems

| OS            | Tested |
|---------------|--------|
| Windows 10    | ✅     |
| Windows 11    | ✅     |
| macOS 11.4+   | ✅     |
| Linux Ubuntu 24 | ✅     |

### Platforms

| Platform     | Tested | Platform     | Tested | Platform     | Tested | Platform     | Tested |
|--------------|--------|--------------|--------|--------------|--------|--------------|--------|
| Apple Music  | ✅     | Beatport     | ✅     | Beatsource   | ✅     | Bugs         | \*     |
| Deezer       | ✅     | Genius       | \*     | Idagio       | \*     | ~~JioSaavn~~ | ❌     |
| KKBOX        | \*     | Musixmatch   | \*     | Napster      | \*     | Nugs.net     | \*     |
| Qobuz        | ✅     | SoundCloud   | ✅     | Spotify      | ✅     | Tidal        | ✅     |
| YouTube      | ✅     |              |        |              |        |              |        |

\* *If this platform isn't working properly and you have a valid subscription you can share, please open an issue or contact me. I'm willing to debug!* 

<br>

<img src="badge.svg" width="189" alt="Badge">
