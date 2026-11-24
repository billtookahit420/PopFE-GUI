"""Pop-fe GUI -- a batch front-end for sahlberg/pop-fe.

Pick what you are converting FOR, add disc images (files or whole
folders), and the queue builder reads every disc's own identity
with pop-fe's code and groups the discs of one game into a single
entry -- only discs whose pop-fe database title matches are
grouped, so a folder full of different games never collapses into
one monster conversion. Convert All then runs pop-fe once per
entry, in order, and reports every success and every failure.

pop-fe by sahlberg -- https://github.com/sahlberg/pop-fe
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import netshare  # noqa: E402
import popfe_engine as engine  # noqa: E402

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QIcon, QPixmap, QTextCursor  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QButtonGroup, QComboBox, QDialog, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)


_LOG_DIVIDER = ("----- previous session's log restored above; "
                "this session continues below -----")


class NetworkShareDialog(QDialog):
    """Server + share (+ optional sign-in) for one network
    share, with the remembered shares one click away. The
    dialog only collects the fields; the caller does the
    actual connect (which can take seconds) in a worker.
    Passwords are never stored -- only server/share/username
    are remembered."""

    def __init__(self, parent, shares):
        super().__init__(parent)
        self.setWindowTitle("Connect to a network share")
        self.shares = [dict(s) for s in shares]
        lay = QVBoxLayout(self)
        intro = QLabel(
            "The disc library lives on the server, so connect "
            "to one of its SMB shares here. On Linux the share "
            "is mounted for you (no root needed); on Windows "
            "its \\\\SERVER\\share path is used directly.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        if self.shares:
            lay.addWidget(QLabel("Remembered shares:"))
            self.list = QListWidget()
            for s in self.shares:
                self.list.addItem("%s / %s"
                                  % (s.get("server", ""),
                                     s.get("share", "")))
            self.list.currentRowChanged.connect(self._fill)
            self.list.itemDoubleClicked.connect(
                lambda _i: self.accept())
            lay.addWidget(self.list)
            forget = QPushButton("Forget selected share")
            forget.clicked.connect(self._forget)
            lay.addWidget(forget)
        else:
            self.list = None
        form = QFormLayout()
        self.server_edit = QLineEdit()
        self.server_edit.setPlaceholderText("192.168.0.102")
        form.addRow("Server:", self.server_edit)
        self.share_edit = QLineEdit()
        self.share_edit.setPlaceholderText("PS1 ARCHIVE")
        form.addRow("Share:", self.share_edit)
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText(
            "optional -- blank for a guest share")
        form.addRow("Username:", self.user_edit)
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.pass_edit.setPlaceholderText(
            "optional -- never saved")
        form.addRow("Password:", self.pass_edit)
        lay.addLayout(form)
        brow = QHBoxLayout()
        brow.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        brow.addWidget(cancel)
        connect = QPushButton("Connect")
        connect.setDefault(True)
        connect.clicked.connect(self._connect)
        brow.addWidget(connect)
        lay.addLayout(brow)

    def _fill(self, row):
        if 0 <= row < len(self.shares):
            s = self.shares[row]
            self.server_edit.setText(s.get("server", ""))
            self.share_edit.setText(s.get("share", ""))
            self.user_edit.setText(s.get("username", ""))

    def _forget(self):
        if self.list is None:
            return
        row = self.list.currentRow()
        if 0 <= row < len(self.shares):
            del self.shares[row]
            self.list.takeItem(row)

    def _connect(self):
        if not self.server_edit.text().strip() \
                or not self.share_edit.text().strip():
            QMessageBox.warning(self, "Missing details",
                                "Enter both a server and a "
                                "share name.")
            return
        self.accept()

    def values(self):
        return {
            "server": self.server_edit.text().strip(),
            "share": self.share_edit.text().strip(),
            "username": self.user_edit.text().strip(),
            "password": self.pass_edit.text(),
        }


class Worker(QThread):
    """Runs one engine callable in the background. Workers are
    held in a set on the window until they finish -- letting a
    worker reference be overwritten while it runs is what aborted
    sibling apps, so it is designed out here."""
    log = Signal(str)
    status = Signal(int, str)
    done = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            result = self._fn(
                lambda s: self.log.emit(str(s)),
                lambda i, s: self.status.emit(i, str(s)))
        except Exception as e:  # noqa: BLE001
            self.done.emit({"error": "%s: %s" % (type(e).__name__, e)})
            return
        self.done.emit(result)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pop-fe GUI -- PSX disc converter")
        self.resize(980, 720)
        self.entries = []
        self._restoring = False
        self._restored_log = None
        self._workers = set()
        self._runner = None
        self._converting = False
        self.settings = self._load_settings()

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)

        header = QLabel("<b>Pop-fe GUI</b> -- convert PSX disc "
                        "images for PSP/Vita, PS2, PS3, PSIO, "
                        "PS Classic and RetroArch, in batches.")
        header.setWordWrap(True)
        lay.addWidget(header)
        credit = QLabel("pop-fe by sahlberg -- "
                        "https://github.com/sahlberg/pop-fe")
        credit.setWordWrap(True)
        lay.addWidget(credit)

        # target selector
        trow = QHBoxLayout()
        trow.addWidget(QLabel("<b>Convert for:</b>"))
        self.target_group = QButtonGroup(self)
        self.target_buttons = {}
        for key, label in engine.TARGETS:
            rb = QRadioButton(label)
            self.target_group.addButton(rb)
            self.target_buttons[key] = rb
            trow.addWidget(rb)
            rb.toggled.connect(self._target_changed)
        trow.addWidget(QLabel("RetroArch layout:"))
        self.ra_layout = QComboBox()
        for key, label in engine.RA_LAYOUTS:
            self.ra_layout.addItem(label, key)
        trow.addWidget(self.ra_layout)
        trow.addStretch(1)
        lay.addLayout(trow)

        # output folder
        orow = QHBoxLayout()
        orow.addWidget(QLabel("<b>Output folder:</b>"))
        self.output_edit = QLineEdit()
        orow.addWidget(self.output_edit, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(lambda: self._browse_output())
        orow.addWidget(browse)
        browse_net = QPushButton("Network...")
        browse_net.clicked.connect(lambda: self._net_add("output"))
        orow.addWidget(browse_net)
        lay.addLayout(orow)
        note = QLabel("For PS3 the package file name is generated "
                      "automatically from the scanned title and "
                      "game ID -- you never type a file name.")
        note.setWordWrap(True)
        lay.addWidget(note)

        # queue buttons -- the three add paths are explicit:
        # one single disc, one deliberate multi-disc set, or a
        # whole folder scanned and grouped automatically.
        brow = QHBoxLayout()
        self.add_single_btn = QPushButton("Add Single Disc...")
        self.add_single_btn.clicked.connect(
            lambda: self._add_single())
        brow.addWidget(self.add_single_btn)
        self.add_set_btn = QToolButton()
        self.add_set_btn.setText("Add Multi-Disc...")
        self.add_set_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        set_menu = QMenu(self.add_set_btn)
        act = set_menu.addAction("Pick the set's disc files...")
        act.triggered.connect(self._add_set_files)
        act = set_menu.addAction("Pick the set's folder...")
        act.triggered.connect(self._add_set_folder)
        self.add_set_btn.setMenu(set_menu)
        brow.addWidget(self.add_set_btn)
        self.add_folder_btn = QPushButton("Add Folder...")
        self.add_folder_btn.clicked.connect(lambda: self._add_folder())
        brow.addWidget(self.add_folder_btn)
        # network shares: the same four add paths, but starting
        # from a server share (mounted via GVFS on Linux, a UNC
        # path on Windows) instead of a local folder.
        self.net_btn = QToolButton()
        self.net_btn.setText("Network Share...")
        self.net_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        net_menu = QMenu(self.net_btn)
        for label, what in (
                ("Add Folder from a share...", "folder"),
                ("Add Single Disc from a share...", "single"),
                ("Add Multi-Disc (pick the discs) from a "
                 "share...", "set_files"),
                ("Add Multi-Disc (pick the folder) from a "
                 "share...", "set_folder")):
            act = net_menu.addAction(label)
            act.triggered.connect(
                lambda _checked=False, w=what: self._net_add(w))
        self.net_btn.setMenu(net_menu)
        brow.addWidget(self.net_btn)
        self.remove_btn = QPushButton("Remove Selected")
        self.remove_btn.clicked.connect(self._remove_selected)
        brow.addWidget(self.remove_btn)
        brow.addStretch(1)
        self.convert_btn = QPushButton("Convert All")
        self.convert_btn.clicked.connect(self._convert_all)
        brow.addWidget(self.convert_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        brow.addWidget(self.cancel_btn)
        lay.addLayout(brow)

        # queue table
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Cover", "Title", "Game ID(s)", "Discs", "Target",
             "Status"])
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.table.setIconSize(QSize(56, 80))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        lay.addWidget(self.table, 1)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        lay.addWidget(self.log_view, 1)

        # status bar: a stage line saying what is happening right
        # now plus a progress bar -- busy (animated) while a scan
        # or the first-run setup runs, entry-count based during a
        # batch, and a plain-language summary when the batch ends.
        # No phase of the app is silent.
        self.stage_label = QLabel("Starting...")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setMinimumWidth(200)
        self.progress_bar.setMaximumWidth(300)
        status_bar = self.statusBar()
        status_bar.addWidget(self.stage_label, 1)
        status_bar.addPermanentWidget(self.progress_bar)

        self._smoke_cfg = None
        self._apply_settings()
        self._restore_state()
        if engine.engine_ready():
            self._set_stage("Ready -- pop-fe engine found.")
            self.log("pop-fe engine found at %s" % engine.ENGINE_DIR)
        else:
            self._set_stage("First-run setup: starting...")
            self._set_progress_busy()
            self.log("pop-fe is not installed yet in %s -- "
                     "starting first-run setup (clone + pop-fe's "
                     "own installer). Watch this log; setup can "
                     "take several minutes."
                     % engine.ENGINE_DIR)
            self._start_setup()
        self._smoke_maybe_start()

    # ---------------------------------------------------------- util
    def log(self, msg):
        self.log_view.appendPlainText(str(msg))
        try:
            with open(engine.SESSION_LOG_PATH, "a") as fh:
                fh.write(str(msg) + "\n")
        except OSError:
            pass
        sink = getattr(self, "_smoke_log_sink", None)
        if sink is not None:
            try:
                with open(sink, "a") as fh:
                    fh.write(str(msg) + "\n")
            except OSError:
                pass

    # -------------------------------------------------- status bar
    def _set_stage(self, text):
        self.stage_label.setText(text)

    def _set_progress_busy(self):
        self.progress_bar.setRange(0, 0)

    def _set_progress(self, done, total):
        if total and total > 0:
            self.progress_bar.setRange(0, int(total))
            self.progress_bar.setValue(max(0, min(int(done),
                                                  int(total))))
        else:
            self._set_progress_busy()

    def _reset_progress(self):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

    @staticmethod
    def _entry_finished(entry):
        status = entry.get("status", "")
        return status in ("done", "cancelled") \
            or status.startswith("failed")

    def _start_worker(self, worker):
        self._workers.add(worker)
        worker.log.connect(self.log)
        worker.finished.connect(
            lambda w=worker: self._workers.discard(w))
        worker.start()
        return worker

    def _load_settings(self):
        try:
            return json.loads(engine.SETTINGS_PATH.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def _save_settings(self):
        data = {
            "target": self._selected_target(),
            "ra_layout": self.ra_layout.currentData(),
            "output_dir": self.output_edit.text().strip(),
            "last_add_dir": self.settings.get("last_add_dir", ""),
            "network_shares": self.settings.get(
                "network_shares", []),
        }
        self.settings.update(data)
        try:
            engine.SETTINGS_PATH.write_text(json.dumps(data, indent=2))
        except OSError:
            pass

    # --------------------------------------------- persistence
    # The queue and this log used to live in memory only, so a
    # power cut (or a crash) mid-batch lost both. They are now
    # mirrored into the data dir on every change: state.json
    # holds the queue, session-log.txt every log line. Startup
    # restores both; an entry that was mid-conversion when the
    # app died comes back marked interrupted, not done.
    def _save_state(self):
        if self._restoring:
            return
        try:
            tmp = engine.STATE_PATH.with_name(
                engine.STATE_PATH.name + ".tmp")
            tmp.write_text(json.dumps({"entries": self.entries},
                                      indent=2))
            os.replace(tmp, engine.STATE_PATH)
        except Exception:  # noqa: BLE001 -- persistence must
            pass            # never take the window down with it

    def _restore_session_log(self):
        try:
            path = engine.SESSION_LOG_PATH
            if path.stat().st_size > 5 * 1024 * 1024:
                # Cap the history: keep the newest lines, the
                # ones that say where the last session stopped.
                tail = path.read_text(
                    errors="replace").splitlines()[-3000:]
                path.write_text("\n".join(tail) + "\n")
            text = path.read_text(errors="replace")
        except OSError:
            return
        if not text.strip():
            return
        # The restored text is NOT put into the log view here.
        # Filling the view before the window is shown leaves
        # this Qt build crashing in its exit cleanup (SIGBUS
        # in the binding teardown, proven on the build node),
        # so the text is stashed and _show_restored_log puts
        # it at the top of the view once the event loop is
        # turning. The divider goes into the log FILE now, so
        # the file keeps true session order.
        self._restored_log = text.rstrip("\n")
        try:
            with open(engine.SESSION_LOG_PATH, "a") as fh:
                fh.write(_LOG_DIVIDER + "\n")
        except OSError:
            pass

    def _restore_state(self):
        self._restoring = True
        try:
            self._restore_session_log()
            try:
                saved = json.loads(engine.STATE_PATH.read_text())
            except Exception:  # noqa: BLE001
                saved = None
            entries = (saved or {}).get("entries") or []
            restored = 0
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("status") in ("converting",
                                           "fetching cover"):
                    entry["status"] = "interrupted (app closed)"
                self.add_entry_row(entry)
                restored += 1
            if restored:
                self.log("Restored %d queue entr%s from the "
                         "previous session."
                         % (restored,
                            "y" if restored == 1 else "ies"))
        finally:
            self._restoring = False
        if self._restored_log:
            QTimer.singleShot(0, self._show_restored_log)

    def _show_restored_log(self):
        """Put the restored previous-session log at the top of
        the log view, above this session's own lines, with the
        divider under it. Runs on the event loop (see
        _restore_session_log for why it cannot run in __init__)."""
        text = self._restored_log
        self._restored_log = None
        if not text:
            return
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.Start)
        cursor.insertText(text + "\n" + _LOG_DIVIDER + "\n")

    def _apply_settings(self):
        target = self.settings.get("target", "psp")
        btn = self.target_buttons.get(target)
        if btn:
            btn.setChecked(True)
        idx = self.ra_layout.findData(
            self.settings.get("ra_layout", "cue"))
        if idx >= 0:
            self.ra_layout.setCurrentIndex(idx)
        self.output_edit.setText(self.settings.get("output_dir", ""))
        self._target_changed()

    def _selected_target(self):
        for key, btn in self.target_buttons.items():
            if btn.isChecked():
                return key
        return "psp"

    def _target_changed(self):
        self.ra_layout.setEnabled(
            self._selected_target() == "retroarch")
        target = self._selected_target()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 4)
            if item:
                item.setText(engine.TARGET_LABEL.get(target, target))

    def _browse_output(self, start=None):
        if start is None:
            start = self.output_edit.text().strip() \
                or self.settings.get("last_add_dir", "")
        folder = QFileDialog.getExistingDirectory(
            self, "Choose the output folder", start)
        if folder:
            self.output_edit.setText(folder)

    # -------------------------------------------------- network shares
    def _remember_share(self, vals):
        shares = [s for s in self.settings.get("network_shares",
                                               [])
                  if not (s.get("server", "").lower()
                          == vals["server"].lower()
                          and s.get("share", "").lower()
                          == vals["share"].lower())]
        shares.append({"server": vals["server"],
                       "share": vals["share"],
                       "username": vals["username"]})
        self.settings["network_shares"] = shares[-12:]
        self._save_settings()

    def _net_add(self, what):
        """Connect to a network share, then run the matching
        picker starting INSIDE the share: 'output', 'folder',
        'single', 'set_files' or 'set_folder'. The connect runs
        in a worker (mounting can take seconds); the picker
        itself opens on this thread once the share is ready."""
        dlg = NetworkShareDialog(
            self, self.settings.get("network_shares", []))
        if dlg.exec() != QDialog.Accepted:
            # the dialog's Forget button may have edited the
            # remembered list even on Cancel -- keep the edit.
            self.settings["network_shares"] = dlg.shares
            self._save_settings()
            return
        self.settings["network_shares"] = dlg.shares
        vals = dlg.values()
        self._set_stage("Connecting to \\\\%s\\%s ..."
                        % (vals["server"], vals["share"]))
        self._set_progress_busy()

        def fn(log, status):
            root = netshare.connect(
                vals["server"], vals["share"], vals["username"],
                vals["password"], log)
            return {"root": root}

        worker = Worker(fn)
        worker.done.connect(
            lambda result: self._net_connected(result, vals,
                                               what))
        self._start_worker(worker)

    def _net_connected(self, result, vals, what):
        self._reset_progress()
        if not isinstance(result, dict) or result.get("error") \
                or not result.get("root"):
            msg = (result.get("error") if isinstance(result, dict)
                   else None) or "the share did not answer"
            # netshare raises with a plain message; the Worker
            # wraps it as 'NetShareError: ...' -- strip the
            # class name for the dialog.
            msg = msg.split(": ", 1)[-1] if msg.startswith(
                "NetShareError") else msg
            self._set_stage("Network share failed -- see the "
                            "log.")
            self.log("Network share \\\\%s\\%s failed: %s"
                     % (vals["server"], vals["share"], msg))
            QMessageBox.warning(self, "Network share", msg)
            return
        root = result["root"]
        self._remember_share(vals)
        self.log("Network share \\\\%s\\%s ready at %s"
                 % (vals["server"], vals["share"], root))
        self._set_stage("Share ready -- pick from %s" % root)
        if what == "output":
            self._browse_output(start=root)
        elif what == "folder":
            self._add_folder(start=root)
        elif what == "single":
            self._add_single(start=root)
        elif what == "set_files":
            self._add_set_files(start=root)
        elif what == "set_folder":
            self._add_set_folder(start=root)

    # ---------------------------------------------------------- setup
    def _start_setup(self):
        self._set_stage("First-run setup: checking system "
                        "packages...")
        self._set_progress_busy()

        def fn(log, status):
            status(-1, "First-run setup: checking system "
                       "packages...")
            ok_sys = engine.ensure_system_deps(log)
            if not ok_sys:
                log("Some system packages are missing (see "
                    "above). pop-fe setup continues; targets "
                    "needing a missing package will fail in the "
                    "open, with the error in this log.")
            status(-1, "First-run setup: downloading the pop-fe "
                       "engine and building its helper tools -- "
                       "this can take several minutes...")
            ok, _helpers = engine.ensure_popfe(log)
            return {"ok": ok}
        worker = Worker(fn)
        worker.status.connect(self._setup_status)
        worker.done.connect(self._setup_done)
        self._start_worker(worker)

    def _setup_status(self, idx, text):
        if idx == -1:
            self._set_stage(text)

    def _setup_done(self, result):
        self._reset_progress()
        if isinstance(result, dict) and result.get("ok"):
            self._set_stage("Ready -- pop-fe engine installed.")
            self.log("pop-fe setup finished -- engine ready.")
        elif isinstance(result, dict) and result.get("error"):
            self._set_stage("Setup hit an error -- see the log.")
            self.log("pop-fe setup hit an error: %s"
                     % result["error"])
        else:
            self._set_stage("Setup did not complete cleanly -- "
                            "see the log.")
            self.log("pop-fe setup did not complete cleanly; "
                     "see the log above.")

    # ---------------------------------------------------------- queue
    _DISC_FILTER = ("Disc images and archives (*.cue *.ccd "
                    "*.img *.bin *.chd *.zip *.7z);;All files (*)")

    def _add_single(self, start=None):
        """Add Single Disc: one disc image, one queue entry."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Add one single disc image",
            self.settings.get("last_add_dir", "") if start is None
            else start, self._DISC_FILTER)
        if path:
            self.settings["last_add_dir"] = str(Path(path).parent)
            self._import_paths([path], "scan")

    def _add_set_files(self, start=None):
        """Add Multi-Disc, files: pick every disc of one set;
        they are queued as ONE grouped set entry."""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Pick all the discs of one multi-disc set",
            self.settings.get("last_add_dir", "") if start is None
            else start, self._DISC_FILTER)
        if files:
            self.settings["last_add_dir"] = str(
                Path(files[0]).parent)
            self._import_paths(files, "set")

    def _add_set_folder(self, start=None):
        """Add Multi-Disc, folder: pick the folder holding one
        set's discs; they are queued as ONE grouped set entry."""
        folder = QFileDialog.getExistingDirectory(
            self, "Pick the folder holding one set's discs",
            self.settings.get("last_add_dir", "") if start is None
            else start)
        if folder:
            self.settings["last_add_dir"] = folder
            self._import_paths([folder], "set")

    def _add_folder(self, start=None):
        """Add Folder: scan a whole folder tree; each game found
        (single or multi-disc) becomes its own queue entry."""
        folder = QFileDialog.getExistingDirectory(
            self, "Add a folder of disc images",
            self.settings.get("last_add_dir", "") if start is None
            else start)
        if folder:
            self.settings["last_add_dir"] = folder
            self._import_paths([folder], "scan")

    def _import_paths(self, paths, mode="scan"):
        for btn in (self.add_single_btn, self.add_set_btn,
                    self.add_folder_btn, self.net_btn):
            btn.setEnabled(False)
        self._set_stage("Scanning and identifying discs...")
        self._set_progress_busy()

        def fn(log, status):
            status(-1, "Scanning and identifying discs...")
            if mode == "set":
                entries = engine.build_set_entry(paths, log)
            else:
                entries = engine.build_entries(paths, log)
            for entry in entries:
                try:
                    engine.resolve_cover(entry, log)
                except Exception as e:  # noqa: BLE001
                    log("Cover lookup failed for %s (%s)"
                        % (entry["title"], e))
            return entries

        worker = Worker(fn)
        worker.status.connect(self._setup_status)

        def _done(result):
            for btn in (self.add_single_btn, self.add_set_btn,
                        self.add_folder_btn, self.net_btn):
                btn.setEnabled(True)
            self._reset_progress()
            if isinstance(result, dict) and result.get("error"):
                self._set_stage("Adding failed -- see the log.")
                self.log("Adding failed: %s" % result["error"])
                return
            for entry in result:
                self.add_entry_row(entry)
            if not result:
                self._set_stage("No disc images found there.")
                self.log("No disc images found there.")
            else:
                discs = sum(len(e["discs"]) for e in result)
                self._set_stage(
                    "Queued %d entr%s (%d disc%s). Ready."
                    % (len(result),
                       "y" if len(result) == 1 else "ies",
                       discs, "s" if discs != 1 else ""))

        worker.done.connect(_done)
        self._start_worker(worker)

    def add_entry_row(self, entry):
        """Append one scanned entry to the queue table. Also used
        by the smoke test to populate the queue directly."""
        self.entries.append(entry)
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, 84)
        cover_item = QTableWidgetItem()
        if entry.get("cover_path"):
            pix = QPixmap(entry["cover_path"])
            if not pix.isNull():
                cover_item.setIcon(QIcon(pix.scaled(
                    60, 84, Qt.KeepAspectRatio,
                    Qt.SmoothTransformation)))
        self.table.setItem(row, 0, cover_item)
        title_item = QTableWidgetItem(entry["title"])
        if not entry.get("known", True):
            title_item.setText(entry["title"]
                               + "  (identity unknown)")
        self.table.setItem(row, 1, title_item)
        self.table.setItem(row, 2, QTableWidgetItem(
            ", ".join(entry["ids"])))
        self.table.setItem(row, 3, QTableWidgetItem(
            str(len(entry["discs"]))))
        self.table.setItem(row, 4, QTableWidgetItem(
            engine.TARGET_LABEL.get(self._selected_target(), "")))
        self.table.setItem(row, 5, QTableWidgetItem(
            entry.get("status", "queued")))
        self.table.setColumnWidth(0, 64)
        self._save_state()

    def _remove_selected(self):
        if self._converting:
            return
        rows = sorted({i.row() for i in
                       self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)
            del self.entries[row]
        if rows:
            self._save_state()

    # ---------------------------------------------------------- convert
    def _convert_all(self):
        if self._converting:
            return
        if not self.entries:
            QMessageBox.information(self, "Nothing queued",
                                    "Add some disc images first.")
            return
        outdir = self.output_edit.text().strip()
        if not outdir:
            QMessageBox.warning(
                self, "No output folder",
                "Choose an output folder first -- that is where "
                "the converted packages are written.")
            return
        if not engine.engine_ready():
            self.log("pop-fe engine is not ready yet -- running "
                     "setup first. Press Convert All again when "
                     "the log says the engine is ready.")
            self._start_setup()
            return
        self._save_settings()
        target = self._selected_target()
        ra_layout = self.ra_layout.currentData()
        entries = list(self.entries)
        runner = engine.QueueRunner()
        self._runner = runner
        self._converting = True
        self.convert_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._set_progress(0, len(entries))
        self._set_stage("Converting 1 of %d: %s"
                        % (len(entries), entries[0]["title"]))

        def fn(log, status):
            return runner.run(entries, target, outdir, ra_layout,
                              log, status)

        worker = Worker(fn)
        worker.status.connect(self._entry_status)
        worker.done.connect(self._convert_done)
        self._start_worker(worker)

    def _entry_status(self, idx, text):
        if 0 <= idx < self.table.rowCount():
            item = self.table.item(idx, 5)
            if item:
                item.setText(text)
        total = len(self.entries)
        if 0 <= idx < total:
            self._set_stage("Converting %d of %d: %s -- %s"
                            % (idx + 1, total,
                               self.entries[idx]["title"], text))
        finished = sum(1 for e in self.entries
                       if self._entry_finished(e))
        self._set_progress(finished, total)
        self._save_state()

    def _convert_done(self, result):
        self._converting = False
        self._runner = None
        self.convert_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self._save_state()
        for i, entry in enumerate(self.entries):
            if 0 <= i < self.table.rowCount():
                item = self.table.item(i, 5)
                if item:
                    item.setText(entry.get("status", ""))
        total = len(self.entries)
        if isinstance(result, dict) and result.get("error"):
            self._set_stage("Conversion error -- see the log.")
            self._set_progress(0, total)
            if self._smoke_cfg is None:
                QMessageBox.critical(self, "Conversion error",
                                     result["error"])
            else:
                self.log("Conversion error: %s" % result["error"])
            return
        if not isinstance(result, dict):
            self._set_stage("Batch finished.")
            return
        ok = result.get("ok", [])
        failed = result.get("failed", [])
        cancelled = result.get("cancelled", [])
        lines = ["Converted: %d" % len(ok)]
        if failed:
            lines.append("FAILED: %d" % len(failed))
            for title, reason in failed:
                lines.append("  - %s: %s" % (title, reason))
        if cancelled:
            lines.append("Cancelled: %d" % len(cancelled))
        text = "\n".join(lines)
        self._set_stage("Batch finished: %d converted, %d failed, "
                        "%d cancelled."
                        % (len(ok), len(failed), len(cancelled)))
        self._set_progress(total, total)
        if self._smoke_cfg is not None:
            self.log("Batch summary: " + text.replace("\n", "; "))
            return
        if failed:
            QMessageBox.warning(self, "Batch finished with "
                                      "failures", text)
        else:
            QMessageBox.information(self, "Batch finished", text)

    def _cancel(self):
        if self._runner is not None:
            self.log("Cancelling -- the current conversion is "
                     "stopped and the rest of the queue will be "
                     "reported as cancelled.")
            self._runner.cancel()
        self.cancel_btn.setEnabled(False)

    def closeEvent(self, event):
        self._save_settings()
        self._save_state()
        if self._runner is not None:
            self._runner.cancel()
        super().closeEvent(event)

    # ---------------------------------------------------------- smoke
    # Scripted end-to-end run, ONLY active when the environment
    # variable POPFE_GUI_SMOKE points at a scenario JSON. It
    # drives the same methods the buttons drive (never the file
    # dialogs), records the queue after every add, screenshots
    # the window (including mid-conversion, for the status bar),
    # and writes a state JSON at the end. Normal runs never see
    # any of this.
    def _smoke_maybe_start(self):
        cfg_path = os.environ.get("POPFE_GUI_SMOKE")
        if not cfg_path:
            return
        try:
            cfg = json.loads(Path(cfg_path).read_text())
        except Exception:  # noqa: BLE001
            return
        self._smoke_cfg = cfg
        if cfg.get("log_out"):
            self._smoke_log_sink = cfg["log_out"]
        self._smoke_state = {"adds": []}
        self._smoke_phase = "setup"
        self._smoke_add_idx = 0
        self._smoke_add_started = False
        self._smoke_convert_started = False
        self._smoke_convert_t0 = None
        self._smoke_mid_shot = False
        self._smoke_t0 = time.time()
        self._smoke_timer = QTimer(self)
        self._smoke_timer.timeout.connect(self._smoke_tick)
        self._smoke_timer.start(300)
        self.log("SMOKE: scripted run started.")

    def _smoke_shot(self, name):
        shots = (self._smoke_cfg or {}).get("shots_dir")
        if not shots:
            return
        try:
            Path(shots).mkdir(parents=True, exist_ok=True)
            self.grab().save(str(Path(shots) / (name + ".png")))
        except Exception:  # noqa: BLE001
            pass

    def _smoke_queue_snapshot(self):
        return [{"title": e["title"], "ids": list(e["ids"]),
                 "discs": len(e["discs"]),
                 "disc_paths": [d["path"] for d in e["discs"]],
                 "status": e.get("status", ""),
                 "cover_path": e.get("cover_path"),
                 "cover_mode": e.get("cover_mode"),
                 "outputs": list(e.get("outputs", []))}
                for e in self.entries]

    def _smoke_finish(self):
        self._smoke_state["stage_final"] = self.stage_label.text()
        self._smoke_state["progress_final"] = {
            "value": self.progress_bar.value(),
            "maximum": self.progress_bar.maximum()}
        self._smoke_state["queue_final"] = \
            self._smoke_queue_snapshot()
        out = (self._smoke_cfg or {}).get("state_out")
        if out:
            try:
                Path(out).write_text(
                    json.dumps(self._smoke_state, indent=2))
            except OSError:
                pass
        self.log("SMOKE: done.")
        QApplication.quit()

    def _smoke_tick(self):
        cfg = self._smoke_cfg
        if cfg is None:
            return
        if time.time() - self._smoke_t0 > cfg.get("total_timeout_s",
                                                 5400):
            self._smoke_state["timeout"] = True
            self._smoke_state["phase_at_timeout"] = self._smoke_phase
            self._smoke_finish()
            return
        busy = bool(self._workers) or self._converting
        if self._smoke_phase == "setup":
            if engine.engine_ready() and not self._workers:
                self._smoke_state["setup_seconds"] = round(
                    time.time() - self._smoke_t0, 1)
                self._smoke_state["stage_after_setup"] = \
                    self.stage_label.text()
                self._smoke_shot("01-after-setup")
                self._smoke_phase = "adds"
            return
        if self._smoke_phase == "adds":
            adds = cfg.get("adds", [])
            if self._smoke_add_idx >= len(adds):
                self._smoke_phase = ("convert" if cfg.get("convert")
                                     else "finish")
                return
            step = adds[self._smoke_add_idx]
            if not self._smoke_add_started:
                kind = step.get("kind")
                if kind in ("folder", "single"):
                    self._import_paths([step["path"]], "scan")
                elif kind == "set_files":
                    self._import_paths(list(step["files"]), "set")
                elif kind == "set_folder":
                    self._import_paths([step["path"]], "set")
                elif kind in ("net_folder", "net_set_folder",
                              "net_output", "net_archive"):
                    # Drive the SAME connect routine the
                    # Network Share dialog uses, then the same
                    # import path the picker would have fed.
                    # 'net_archive': subdir names an archive
                    # FILE on the share; the engine extracts
                    # it to local scratch and groups its discs.
                    try:
                        root = netshare.connect(
                            step["server"], step["share"],
                            step.get("username", ""),
                            step.get("password", ""), self.log)
                        self._smoke_state["net_root"] = root
                        sub = step.get("subdir", "")
                        path = str(Path(root) / sub) if sub \
                            else root
                        if kind == "net_output":
                            self.output_edit.setText(path)
                        elif kind == "net_folder":
                            self._import_paths([path], "scan")
                        elif kind == "net_archive":
                            self._import_paths([path], "scan")
                        else:
                            self._import_paths([path], "set")
                    except Exception as e:  # noqa: BLE001
                        self._smoke_state.setdefault(
                            "net_errors", []).append(
                                "%s: %s" % (type(e).__name__, e))
                        self.log("SMOKE net step failed: %s" % e)
                self._smoke_add_started = True
                return
            if not busy:
                self._smoke_state["adds"].append({
                    "step": step,
                    "queue": self._smoke_queue_snapshot(),
                    "output_dir": self.output_edit.text(),
                    "stage": self.stage_label.text()})
                self._smoke_shot("02-queue-after-add-%d"
                                 % self._smoke_add_idx)
                self._smoke_add_idx += 1
                self._smoke_add_started = False
            return
        if self._smoke_phase == "convert":
            if not self._smoke_convert_started:
                if cfg.get("output_dir"):
                    self.output_edit.setText(cfg["output_dir"])
                btn = self.target_buttons.get(cfg.get("target"))
                if btn is not None:
                    btn.setChecked(True)
                self._smoke_convert_t0 = time.time()
                self._convert_all()
                self._smoke_convert_started = True
                return
            if self._converting and not self._smoke_mid_shot \
                    and time.time() - self._smoke_convert_t0 > 5:
                self._smoke_mid_shot = True
                self._smoke_state["mid_run_stage"] = \
                    self.stage_label.text()
                self._smoke_state["mid_run_progress"] = {
                    "value": self.progress_bar.value(),
                    "maximum": self.progress_bar.maximum()}
                self._smoke_shot("03-convert-mid-run")
            if not busy:
                self._smoke_shot("04-after-convert")
                self._smoke_phase = "finish"
            return
        if self._smoke_phase == "finish":
            self._smoke_finish()


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    code = app.exec()
    # Orderly teardown: close and destroy the window while
    # the application object is still alive. Leaving every
    # widget for the interpreter's exit sweep to destroy
    # crashes this PySide build (SIGBUS in the binding
    # teardown) once a session has restored state to destroy.
    win.close()
    win.deleteLater()
    app.processEvents()
    del win
    del app
    return code


if __name__ == "__main__":
    raise SystemExit(main())
