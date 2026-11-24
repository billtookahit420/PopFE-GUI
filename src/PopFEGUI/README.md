# Pop-fe GUI

A batch front-end for **pop-fe by sahlberg**
(https://github.com/sahlberg/pop-fe) -- the tool that converts
PlayStation 1 disc images into packages for other Sony hardware.
This GUI wraps it in the converter-family style: pick a target,
queue up games, convert them all, and get an honest report.

pop-fe itself does all the converting. This app adds the queue,
the smart multi-disc grouping, a libretro cover source, and
automatic PS3 package naming.

## What it does

- **Target selector** -- one target per batch run: PSP/Vita
  (EBOOT), PS2 (VCD), PS3 (PKG), PSIO, PS Classic (AutoBleem
  layout), or RetroArch (bin/cue with .m3u, or PBP).
- **Three ways to add** -- *Add Single Disc* (one disc image,
  one queue entry), *Add Multi-Disc* (pick the discs of one
  set, or the folder holding them; they are queued as ONE
  grouped set entry), and *Add Folder* (scan a whole folder
  tree; every game found becomes its own entry, multi-disc
  sets grouped automatically).
- **Archive input** -- Add Single and Add Folder also accept
  `.zip` and `.7z` archives (picked directly, or found inside
  an added folder). The archive is extracted to a local
  scratch folder inside the app's cache -- the source archive
  is never modified and extraction never happens back onto
  a network share -- and the discs inside (nested folders
  included) are scanned and grouped exactly as if the loose
  files had been picked, so a multi-disc set inside one
  archive becomes one grouped entry. Password-protected
  archives are not supported. Extracted scratch is kept for
  the session (queue entries point at it) and can be deleted
  from the cache folder whenever the app is closed.
- **Network shares** -- the disc library usually lives on a
  server, so every picker can start from a server share: the
  *Network Share...* button (next to the Add buttons) offers
  Add Folder / Add Single Disc / Add Multi-Disc from a share,
  and the output folder row has its own *Network...* button.
  You enter a server and share name once; the app remembers
  up to the last 12 shares (server/share/username only --
  passwords are never saved). On Linux the share is mounted
  for you with GVFS (`gio mount`, no root needed) and used
  through its real mount folder; on Windows the
  `\\SERVER\share` UNC path is used directly and Windows
  handles any password prompt. See "Network shares" below.
- **Status bar** -- a stage line plus progress bar along the
  bottom says what the app is doing at all times: first-run
  setup, scanning, which queue entry is converting and how
  far through the batch it is, and the final summary. No
  phase is silent.
- **No XMB theme song** -- packages NEVER include the SND0
  theme song that plays when a game is highlighted on the
  XMB. pop-fe would download one for many games; this app
  passes pop-fe's own `--snd0 no` off switch on every
  conversion. Cover art is still fetched as below, and the
  game's own music (its CD audio) is untouched.
- **Batch convert** -- a queue of games converted one after
  another, each with its own status. Cancel stops the current
  conversion and reports the rest as cancelled. The final
  summary names every failure and its reason -- nothing is
  silently swallowed.
- **Smart multi-disc grouping** -- when you add files or a
  folder, every disc is identified by pop-fe's OWN code: the
  MD5 fingerprint / SYSTEM.CNF extraction pop-fe itself uses,
  giving each disc's real game ID and its title from pop-fe's
  database. Discs are grouped into ONE queue entry only when
  their title matches (multi-disc sets legitimately carry a
  different serial per disc -- Evil Dead is SLUS01072 +
  SLUS01326, one title, one entry). Disc order comes from the
  disc number in the file name, then the serial. Unrelated
  games in the same folder always stay separate entries, and a
  disc whose identity cannot be read is queued on its own,
  flagged "identity unknown".
- **Covers** -- before conversion the GUI tries
  libretro-thumbnails (serial -> Redump name -> Named_Boxarts,
  the Recomp Covers fetch policy). A found cover is saved as
  `<disc name>_cover.png` beside disc 1 -- the exact file
  pop-fe looks for first -- when that folder is writable.
  An existing local cover is never overwritten; if the folder
  is not writable the cover is cached in the app and passed to
  pop-fe with `--cover` instead. If libretro has nothing,
  pop-fe's own built-in scraper (psxdatacenter / pop-fe assets)
  fetches the art during conversion, as usual.
- **Automatic PS3 package names** -- for the PS3 target you
  pick only an output folder. The package is named
  `<Title> [<FirstDiscID>].pkg` from the scanned data, one name
  per multi-disc set, and an existing package is never
  overwritten (` (2)`, ` (3)`, ... are appended).
- **Free-space check** -- before each conversion the app
  estimates the temp + output space pop-fe needs and fails
  that entry with a plain "not enough free space" message
  instead of letting pop-fe die mid-write.

## Network shares

Every path in the app -- the three Add paths and the output
folder -- can live on an SMB share (a home server, a NAS).
Use the **Network Share...** button for adding and the
**Network...** button beside the output folder; both open
the same connect dialog: server (name or IP), share name,
and an optional username/password for shares that are not
guest-accessible. After connecting, the normal file picker
opens *inside* the share, so you browse its subfolders and
files exactly like a local disk, and conversions read from
and write to the share directly.

- **Linux**: the share is mounted with GVFS --
  `gio mount smb://server/share` -- which needs no root and
  works from the AppImage. That needs the `gvfs` and
  `gvfs-backends` packages on the system (most desktop
  installs already have them; the app says so plainly if
  `gio` is missing). The mount appears under
  `/run/user/<you>/gvfs/` and stays mounted after the app
  closes (unmount from your file manager, or
  `gio mount -u smb://server/share`). Reading a big library
  over Wi-Fi is slower than local disk; a wired connection
  is kinder for batch conversions.
- **Windows**: the `\\SERVER\share` UNC path is used as-is;
  File Explorer's credential prompt applies if the share
  needs a password. Mapping a drive letter first also works
  -- a mapped drive is just a local path to the pickers.

## Packages

- **Linux**: the AppImage bundles the Python/Qt runtime.
  The pop-fe engine, settings and cover cache live in
  `~/.local/share/popfe-gui/` (created on first run), so the
  AppImage file itself stays read-only and portable. If your
  system cannot mount AppImages (no libfuse2), run it with
  `--appimage-extract-and-run`.
- **Windows / source**: this folder, launched with
  `pop-fe-gui.bat` (Windows) or `pop-fe-gui.sh` (Linux). The
  launcher creates a `.venv` beside the app on first run and
  installs the GUI's Python deps into it; engine, settings
  and cache then live inside this folder.

## First run

The app needs pop-fe itself. On first run it downloads and
sets it up automatically (the status bar shows the setup
progress; everything also streams to the log):

1. Missing Linux system packages are installed via `pkexec`
   (one password prompt): ffmpeg, mame-tools (chdman, for .chd
   files), cmake, libsndfile1-dev.
2. pop-fe is cloned from GitHub into the app's engine folder
   (`engine/` beside the app, or `~/.local/share/popfe-gui/
   engine/` for the AppImage).
3. A venv is created in the engine folder, setuptools is
   installed first (pop-fe's PS3 helper build needs it on
   Python 3.12+), then pop-fe's own `pop-fe.py --install`
   runs inside the venv.
4. pop-fe's installer ignores helper build failures, so the
   app verifies afterwards that the helpers really exist
   (atracdenc, lcp, the PS3 pkgcrypt module, psx-undither,
   binmerge) and prints a warning naming anything missing --
   conversions needing a missing helper will fail in the open,
   with pop-fe's error in the log.

You need: Python 3.10+ (with venv support), git, and an
internet connection for setup and cover downloads.

## Good to know

- pop-fe works in its own folder (`engine/`) and creates its
  `pop-fe-work/` temp files there; conversions of big discs
  need several GB of free space across the engine folder and
  your output folder.
- During conversion, pop-fe itself may write small sidecar
  files next to your disc images (GAME_ID / GAME_TITLE /
  `*_cover.png` and friends). That is upstream pop-fe
  behaviour, not something this GUI adds.
- Grouping follows the title rule exactly. Two discs pop-fe's
  database gives the *same* title to will be grouped as one
  set -- that is how pop-fe treats them too (it converts
  whatever set it is handed in one pass). Check the queue
  before converting if you keep variant discs (for example
  the two Resident Evil 2 Dual Shock discs, which share one
  database title) in the same folder.
- Windows: the GUI and launcher run, and pop-fe setup is
  attempted the same way, but this build is developed and
  verified on Linux; pop-fe's own Windows behaviour is
  upstream's domain. Treat Windows as untested.
- No game data is included. You supply your own disc images
  (.cue/.bin, .ccd/.img, .chd, .zip).

## Credits

- pop-fe -- sahlberg, https://github.com/sahlberg/pop-fe
  (LGPL-2.1; cloned unmodified into `engine/` at setup time,
  never redistributed inside this package)
- Cover art -- libretro-thumbnails
  (https://github.com/libretro-thumbnails) and the Redump DAT
  from libretro-database; fetch policy shared with the
  Recomp Covers app.
