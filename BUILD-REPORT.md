# Pop-fe GUI — AppImage + Windows package build report (2026-11-20)

Built and acceptance-tested on recomp-node2 under Xvfb, driving the
packaged AppImage itself through a scripted smoke hook
(`POPFE_GUI_SMOKE`, inert in normal runs).

## Deliverables

| Item | Location | Size | sha256 |
|---|---|---|---|
| Linux AppImage | node2 `~/popfe-build/PopFE-GUI-x86_64.AppImage` | 129,680,576 | `eed008b9d904f54ec21ccc8f74de7c53ad103c5ba6cad56dbdda347442b15049` |
| Windows package | VM `~/workspace/pop-fe-appimage/PopFEGUI-Windows.zip`, copy on node2 `~/popfe-build/` | 29,568 | `457f607fc75ab9bcd0074b3a688f3da2910dfafeb92eea2627bd6b7919c15f7b` |
| PS3 test pkg (K9.5 5) | node2 `~/popfe-build/deliverables/ps3/K9.5 5 - The Howlywood Premiere (LSP907103) [SCUS80087].pkg` | 716,883,920 | `64b26e63095a295ffad57650c2146fe6b32d062bddc51409a701d7bf60a8f34f` |
| PS3 test pkg (SF Collection, 2-disc set) | node2 `~/popfe-build/deliverables/ps3/STREET FIGHTER COLLECTION [SLUS00423].pkg` | 1,400,176,272 | `4f9941bbc3229be6b6c207c50b31f02894d95a8812375d424d9d870a0fb89030` |
| PSP test package (K9.5 5) | node2 `~/popfe-build/deliverables/psp/SCUS80087/` (EBOOT.PBP 602,967,256, sha256 `9e40479257c6759196a0a728276fbf954ff70e3fc6fdc1db3b320f4c2e8593af`, + DOCUMENT.DAT 2,816,583) | | |
| PSP test package (SF Collection set) | node2 `~/popfe-build/deliverables/psp/SLUS00423/` (EBOOT.PBP 376,072,728, sha256 `7672bfb9a9e2991ad299c459553395748219bbe6c74809a6660ad79faf634680`, + DOCUMENT.DAT 11,360,973) | | |

Windows package = the owner's established PopFEGUI.zip format (source
tree + `pop-fe-gui.bat` first-run venv launcher + `.sh` twin + README).
It carries the identical updated source as the AppImage. It was NOT
executed on Windows (fleet is Linux-only) — the .bat path is verified
only by being the owner's own shipped launcher, unchanged.

## What changed in the app (source: `src/PopFEGUI/`)

- **Status bar (req 6)** — stage line + progress bar in the main
  window: setup stages, scan state, "Converting k of n: title —
  stage", batch summary. No silent phases.
- **Add Single Disc / Add Multi-Disc (req 3b)** — explicit buttons
  beside Add Folder. Multi-Disc offers "pick the set's disc files"
  or "pick the set's folder" and force-groups them into ONE entry
  (`engine.build_set_entry`); titles/IDs still read by pop-fe's own
  scan.
- **No SND0 (req 8)** — every conversion argv carries pop-fe's own
  `--snd0 no` off switch; it short-circuits the theme download,
  local `.snd0` files, and the gamedb download. In-game CD audio
  (CDDA→ATRAC3 via atracdenc) is untouched.
- **First-run download robustness (req 7)** — the engine installer
  env now carries three verified workarounds pop-fe's helper builds
  need on modern toolchains: `CMAKE_POLICY_VERSION_MINIMUM=3.5`
  (atracdenc vs CMake 4), `CXXFLAGS=-include cstdint` (atracdenc vs
  current GCC), and `CC/LDSHARED=gcc` when clang is absent (the
  clang-built bundled Python makes distutils demand clang for the
  PS3 pkgcrypt extension). Plus `_repair_lcp`: pop-fe's installer
  extracts the libcrypt patcher with `unzip -f` (freshen), which
  silently skips a not-yet-existing file, so lcp never lands on a
  fresh install; the app now pulls it out of the zip itself.
- **Packaging paths** — `POPFE_GUI_DATA_DIR` env override for
  settings/cache; AppRun sets engine + data dirs to
  `~/.local/share/popfe-gui/`, exports the bundled certifi CA via
  `SSL_CERT_FILE`, and re-points the engine venv's python links at
  the current AppImage mount on every launch (mount paths change;
  without this the venv dangles and the app would re-run setup).

## Acceptance evidence (node2 `~/popfe-build/`)

- **First run** (clean HOME): setup completed in ~64 s (PS3 run) /
  ~56 s (PSP run, after the lcp repair landed — all 5 helpers OK);
  status bar showed setup stages; screenshot `shots-*/01-after-setup.png`.
- **Later run skip**: same HOME, repacked AppImage → engine found in
  0.3 s, no download (`smoke-skip-state.json`).
- **Add Folder** (PS3 run): test library scanned → K9.5 5 (1 disc)
  + Street Fighter Collection as ONE entry, 2 discs
  [SLUS00423, SLUS00584]; stage "Queued 2 entries (3 discs). Ready."
- **Add Single / Add Multi-Disc** (PSP run): single → exactly one
  1-disc entry; set_files → SF as ONE grouped 2-disc entry.
- **Conversions**: both runs "Batch finished: 2 converted, 0 failed,
  0 cancelled." Mid-run status captured ("Converting 1 of 2: ... —
  converting", progress 0/2 → 2/2; `shots-*/03-convert-mid-run.png`).
- **Covers**: libretro covers fetched over the real network from
  inside the packaged app and placed beside disc 1
  (`*_cover.png`, 461 KB / 482 KB), then used by pop-fe.
- **Req 8 in the packages**: PS3 PKGs listed with ps3py — ICON0.PNG
  (68 KB / 78 KB) + PIC1.PNG present, **no SND0.AT3**. PSP EBOOT
  section tables parsed — ICON0/PIC0/PIC1 non-zero, **SND0.AT3
  section size 0** in both. Conversion logs show `Disable SND0`
  once per conversion.
- **Req 9 (game configs)**: the first-run-downloaded engine carries
  pop-fe's config database (85 `ps3configs` + 774 `pspconfigs`
  title sets). PSP run log: `Found an external config for
  SLUS00423` — Street Fighter Collection's pspconfig was consulted
  and embedded. Neither test title has a `ps3configs` entry, so the
  PS3 packages carry pop-fe's default 60-byte USRDIR/CONFIG (stated
  plainly; the apply path is create_ps3's external-config load).

## Node-2 test accommodations (not app behaviour)

- Node 2 has no libfuse2, so the AppImage was run with
  `--appimage-extract-and-run`. On a desktop with libfuse2 it runs
  directly; the README says so.
- Node 2 has no passwordless sudo and no system libsndfile headers;
  the test launches exported `CMAKE_PREFIX_PATH`/`PKG_CONFIG_PATH`
  pointing at a locally extracted libsndfile1-dev prefix so
  atracdenc could build. On a normal desktop the app's own pkexec
  step installs `libsndfile1-dev` instead. chdman (mame-tools) is
  likewise absent on node 2 — only .chd inputs need it; the app
  reports it missing in the open, as designed.

## Addendum — 2026-11-22/23: network-share + archive-input retrofit

Two features added at Duane's word (his library lives on the
home server; pickers that only see local disks force
copy-first workarounds):

### Network shares (new module app/netshare.py)
- Every picker gained a network route: the output row has a
  "Network..." button; the queue row has a "Network Share..."
  menu (Add Folder / Add Single / Add Multi-Disc files /
  Add Multi-Disc folder from a share). A dialog collects
  server + share + username + password (password NEVER
  stored; remembered shares keep server/share/username only,
  capped at 12, persisted in settings).
- Windows: the UNC root \\SERVER\share is handed to the
  standard pickers as-is; Windows' redirector and native
  credential prompts do the rest. A failed reachability
  probe does not hard-fail (credential shares answer
  access-denied to probes). Code-verified only — the fleet
  is Linux-only, same honesty as the original report.
- Linux: `gio mount smb://[user@]server/share` (GVFS)
  returns the real FUSE path under $XDG_RUNTIME_DIR/gvfs,
  and the normal pickers open there, so subfolder/file
  browsing is the ordinary dialog on a real path. Requires
  the host's gvfs + gvfs-backends packages (the error
  message says so when gio is missing).
- gio prompt handling (bug found by the VB retrofit against
  a live authenticated share, fixed here the same way):
  gio is driven through a PTY and only the PENDING prompt
  (text after the last newline) is answered — Domain
  first, then Password, then Username. Keyword-searching
  the whole output buffer is wrong: the header line itself
  contains the word "password", so a whole-buffer search
  types the password into the Domain field and never
  answers the real Password prompt. Answers are capped
  at 8; the buffer is cleared after each answer. The PTY
  also makes gio's password prompt turn echo off.
  This fix landed AFTER the first retrofit rebuild, so the
  packages below are a second rebuild that includes it.

### Archive input (.zip / .7z)
- Add Single / Add Folder / Add Multi-Disc accept .zip and
  .7z files directly and inside added folders. The engine
  extracts them to LOCAL scratch (cache/archive-scratch
  under the app's data dir — never back onto a share, the
  source archive is only read), then scans/groups the
  extracted tree exactly like loose files; a multi-disc
  set inside one archive becomes one grouped entry.
  .zip via stdlib zipfile with zip-slip protection; .7z
  via py7zr (bundled into the AppImage's Python, and in
  requirements.txt for source/Windows installs).
  Password-protected archives are reported as unsupported.

### Verification (packaged AppImage, localhost fixtures only —
no real game images were pulled from Duane's server)
- Fixture: a local smbd share with a synthetic 2-disc set
  (serials SLUS00423/SLUS00584, gamedb title STREET
  FIGHTER COLLECTION) loose, plus the same set inside a
  .zip and a .7z; authenticated (user + password, no guest).
- Packaged-AppImage smoke: net Add Folder grouped the
  loose set as ONE entry / 2 discs; net .zip add -> ONE
  entry / 2 discs; net .7z add -> ONE entry / 2 discs;
  batch converted 3/3 with the output folder set to the
  share, and the RetroArch outputs (PSISO.m3u +
  PSISO0/1 .CD/.bin/.sbi) were verified present on the
  server side of the share afterwards.
- Output-on-share and the ':' in GVFS paths: the GVFS
  mount path contains ':' (smb-share:server=...,share=...)
  and all 3 conversions wrote straight to it. PopFE is
  immune to the GNU Make ':' failure the NES tool hit —
  its conversion path is pop-fe's Python end to end (no
  make anywhere in a conversion; paths travel as argv /
  Path objects). No staging workaround needed.
- Regression (real test library, PSP target): Add Single
  (K9.5 5) + Add Multi-Disc set (Street Fighter
  Collection, 2 discs) converted 2/2, game configs
  applied ("Found an external config for SLUS00423"),
  SND0.AT3 section size 0 in both EBOOT.PBPs, status bar
  narration captured mid-run — all preserved behaviors
  intact on the final build.
- Environment note: the build node's GVFS came from a
  user-space prefix (the node has no system gvfs); the
  session needed GVFS_MOUNTABLE_DIR + GIO_MODULE_DIR +
  GSETTINGS_SCHEMA_DIR to make the extracted stack work.
  On a normal desktop (Duane's Kubuntu) gvfs is installed
  properly and the app needs none of that.

### Packages (this retrofit)
- PopFE-GUI-x86_64.AppImage — 135,488,704 B,
  sha256 fc899307eb4eb8424d761c29cf1989ce9989660bcc279aeeb34d2f7491bbb1c9
  (previous build kept alongside as .orig-2026-11-20)
- PopFEGUI-Windows.zip — 40 KB,
  sha256 856a080f52f429ca5ead4c47b20312569cb1cd742fc761b7037dc2ba1a770f66

### Still owed (unchanged by this retrofit)
The queue/results PERSISTENCE fix (queue + results log live
in memory only; a power cut loses them — Duane's 2026-11-21
batch) is a separate known debt and was NOT part of this
retrofit. **DONE 2026-11-23 — see the persistence addendum
at the end of this report.** PS3-form hardware verdicts and
a Windows first run also remain outstanding, as in the
original report.
NOT published — releases are Duane's word alone.

## Addendum — 2026-11-23: queue + log persistence

The owed fix from Duane's 2026-11-21 power-cut batch: the
queue and the results log lived in memory only. Both are
now mirrored into the app's data dir and restored at
startup. No new UI, no settings changes.

### What changed (source: `src/PopFEGUI/`)
- **Engine** (`app/popfe_engine.py`): two new paths beside
  `SETTINGS_PATH` — `STATE_PATH` (`state.json`) and
  `SESSION_LOG_PATH` (`session-log.txt`), both under
  `DATA_DIR` (the per-user folder AppRun already creates).
- **Queue** (`app/main.py`): `_save_state()` writes
  `{"entries": self.entries}` to `STATE_PATH` atomically
  (temp file + `os.replace`) whenever the queue changes —
  in `add_entry_row`, `_remove_selected`, `_entry_status`
  (every status transition during a batch), `_convert_done`
  and `closeEvent`. A `self._restoring` flag keeps restores
  from re-saving. Save failures are swallowed by design:
  persistence must never take the window down.
- **Log**: `log()` also appends every line to
  `SESSION_LOG_PATH` (open-append per line, OSError-guarded
  — the same pattern the smoke sink already used), so the
  file is current to the last line even if the process dies
  mid-batch.
- **Restore** (in `__init__`, after `_apply_settings`):
  the session log is read back (trimmed first to its last
  ~3000 lines if it has grown past ~5 MB) and the queue is
  re-added entry by entry. An entry saved as `converting`
  or `fetching cover` comes back as
  `interrupted (app closed)` — an honest mid-batch death,
  not a fake success; every other status is kept as saved.
  Corrupt or missing files fall back to an empty start,
  exactly like `_load_settings`. A divider line
  ("----- previous session's log restored above; ...")
  separates sessions in both the file and the view.
- **Two exit-crash fixes the proof run forced** (below):
  the restored log text is inserted at the top of the log
  view from a zero-delay timer once the event loop is
  turning (`_show_restored_log`), NOT during `__init__` —
  filling the view before the window is shown leaves this
  PySide build crashing in its binding teardown (SIGBUS in
  `QObject::~QObject` via Shiboken's exit sweep, core-
  dump-verified on node 2). The divider is written to the
  log file at restore time so the file keeps true session
  order. And `main()` now tears down in order — close and
  destroy the window while the `QApplication` is still
  alive — instead of leaving every widget to the
  interpreter's exit sweep, which crashed the same way
  whenever a session had restored state. Both behaviours
  were reproduced against the pre-fix build (old build:
  clean; intermediate build: crash on every restored
  launch) and are gone in the final build.

### Proof (packaged build, node 2)
Two-phase offscreen harness (`proof/persist_harness.py`)
driving `MainWindow` from the EXTRACTED final AppImage
tree, engine seeded from the existing test-home install:
- **Phase 1 — power cut.** Queue populated through the
  app's own code paths (K9.5 5, 1 disc; Street Fighter
  Collection, 2 discs), first entry run to `done`, batch
  tally logged, second entry left at `converting` — then
  the process died via `os._exit` with NO `closeEvent`.
  On disk afterwards: `state.json` held both entries with
  their raw statuses (`done` / `converting`), and
  `session-log.txt` held every line including
  `Batch summary: Converted: 1` and the last line written
  before the cut.
- **Phase 2 — relaunch, same data dir.** Queue restored:
  K9.5 5 back as `done`; Street Fighter Collection back
  as `interrupted (app closed)` (both in the entry data
  and the visible table cells). Log view showed the whole
  previous session at the top — engine line, both
  `Queued:` lines, `Batch summary: Converted: 1`, the
  pre-cut last line — then the divider, then
  `Restored 2 queue entries from the previous session.`
  A clean close re-saved `state.json` with the mapped
  `interrupted (app closed)` status persisted.
- **Regression — no prior state.** Fresh data dir: empty
  queue, stage `Ready -- pop-fe engine found.`, log view
  holding only the engine-found line, no `state.json` at
  startup — byte-for-byte the pre-fix first-launch
  behaviour.
- **Smoke hook.** The packaged AppImage run the real way
  (`--appimage-extract-and-run`, AppRun, Xvfb, test-home)
  with the existing skip scenario completed normally
  (`SMOKE: done.`, engine found in 0.3 s) and exited 0 —
  the run that exited 127 on the intermediate build
  before the teardown fix. Restored-launch exit codes on
  the final build: 8/8 clean across log-restore,
  state-only, empty-log and brand-new data dirs.

### Packages (this fix)
- PopFE-GUI-x86_64.AppImage — 135,484,608 B,
  sha256 31c2827061fb5ced131eb8944bad3ef327c1110e5af46d8bdaba0a16d5d64fe2
  (previous retrofit build kept alongside as
  .prev-retrofit; the 2026-11-20 build as .orig-2026-11-20)
- PopFEGUI-Windows.zip — 38,755 B,
  sha256 bd20773930548fab9a15fe9260fce320563c87f31ed983034178724e085c72e2
  (same source + launchers layout as before; not executed
  on Windows, same honesty as the original report)
NOT published — releases are Duane's word alone.
