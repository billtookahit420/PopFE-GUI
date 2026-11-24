"""Network share access for the Pop-fe GUI.

Duane's whole disc library lives on a home server (SMB shares),
so every picker in this app must be able to reach a share, not
just local disks. This module turns "server + share name" into
a REAL filesystem path the rest of the app (pathlib, pop-fe
subprocesses, file dialogs) can use unchanged:

* Windows -- a UNC path (\\\\SERVER\\share). Windows' own
  redirector does the work; the path is handed back as-is and
  the native file dialog / OS handle any credential prompt.
* Linux -- the share is mounted with GVFS (`gio mount
  smb://server/share`), which needs no root and works inside
  the AppImage because `gio` is a host binary found on PATH.
  The mount appears under $XDG_RUNTIME_DIR/gvfs/ as a real
  directory (FUSE), and that directory is what we hand back.

Shares the user connects to are remembered by the CALLER in
settings (server/share/username only -- passwords are never
stored anywhere).
"""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote


class NetShareError(Exception):
    """A plain-language network share failure."""


def is_windows():
    return os.name == "nt"


# ---------------------------------------------------------------------
# Windows

def unc_root(server, share):
    return "\\\\%s\\%s" % (server.strip("\\/ "),
                            share.strip("\\/ "))


def connect_windows(server, share, log):
    """Return the UNC root for the share. Reachability is
    probed, but a failed probe does NOT hard-fail: a share that
    needs credentials answers the probe with access-denied while
    the native file dialog (opened at this path next) would have
    prompted for the password and worked. So hand the path back
    either way and say what the probe saw."""
    root = unc_root(server, share)
    try:
        reachable = Path(root).is_dir()
    except OSError:
        reachable = False
    if reachable:
        log("Network share %s is reachable." % root)
    else:
        log("Network share %s did not answer a direct probe "
            "(it may need credentials) -- opening the picker "
            "there anyway; Windows will ask for a password if "
            "one is needed." % root)
    return root


# ---------------------------------------------------------------------
# Linux (GVFS)

def smb_url(server, share, username=""):
    server = server.strip().strip("/")
    share = share.strip().strip("/")
    auth = ""
    if username:
        auth = "%s@" % username.strip()
    return "smb://%s%s/%s" % (auth, server, share)


def gvfs_dir():
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        base = "/run/user/%d" % os.getuid()
    return Path(base) / "gvfs"


def find_mount(server, share):
    """Local path of an already-mounted GVFS SMB share, or
    None. GVFS names mounts 'smb-share:server=X,share=Y' (with
    extra ,user=/,port= parts when applicable), so match on the
    server and share parts, case-insensitively."""
    root = gvfs_dir()
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    host = server.strip().lower().split(":")[0]
    want_server = "server=%s" % host
    want_share = "share=%s" % share.strip().lower()
    for entry in entries:
        # GVFS may percent-encode values in the mount name
        # (e.g. a space in the share name) -- compare decoded.
        name = unquote(entry.name).lower()
        if not name.startswith("smb-share:"):
            continue
        parts = name[len("smb-share:"):].split(",")
        if want_server in parts and want_share in parts:
            if entry.is_dir():
                return str(entry)
    return None


def _gio_mount(url, username, password, timeout=90):
    """Run `gio mount <url>` through a pty, answering its
    credential prompts. Returns (rc, output text); rc is
    124 when the mount had to be killed at the deadline.

    Prompt handling (the subtle part): on current glib,
    gio asks Domain FIRST, then Password, under a header
    line that itself contains the word "password" ("Enter
    password for share ..."). So the output buffer must
    NOT be keyword-searched as a whole -- only the PENDING
    prompt (the text after the last newline; prompts are
    printed without one) says what is actually being
    asked. Searching the whole buffer types the password
    into the Domain field and then never answers the real
    Password prompt, and the mount hangs. Answers are
    capped (a refused credential would otherwise re-prompt
    forever), and the buffer is cleared after each answer
    so a prompt is never answered twice. The pty also
    gives gio a real terminal, so its password prompt
    turns echo off and the password never lands in the
    captured output."""
    import pty

    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["gio", "mount", url], stdin=slave, stdout=slave,
        stderr=slave, close_fds=True)
    os.close(slave)
    buf = b""
    answered = 0
    deadline = time.time() + timeout
    os.set_blocking(master, False)
    while True:
        if proc.poll() is not None:
            # drain whatever is left
            try:
                while True:
                    chunk = os.read(master, 4096)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                pass
            break
        if time.time() > deadline:
            proc.kill()
            try:
                os.close(master)
            except OSError:
                pass
            return 124, (buf.decode("utf-8", "replace")
                         + "\n(mount timed out)")
        try:
            chunk = os.read(master, 4096)
            if chunk:
                buf += chunk
        except BlockingIOError:
            time.sleep(0.05)
            continue
        except OSError:
            break
        text = buf.decode("utf-8", "replace")
        pending = text.replace("\r", "\n").rsplit("\n", 1)[-1]
        answer = None
        if "omain" in pending:
            answer = "WORKGROUP"
        elif "assword" in pending:
            answer = password or ""
        elif "sername" in pending or "ser [" in pending \
                or pending.rstrip().endswith("ser:"):
            answer = username or ""
        if answer is not None and answered < 8:
            os.write(master, (answer + "\n").encode())
            answered += 1
            buf = b""
    rc = proc.wait()
    try:
        os.close(master)
    except OSError:
        pass
    return rc, buf.decode("utf-8", "replace").strip()


def connect_linux(server, share, username="", password="",
                  log=lambda _m: None):
    """Mount the share via GVFS (if not already mounted) and
    return its local path. Raises NetShareError with a plain
    message on failure."""
    existing = find_mount(server, share)
    if existing:
        log("Network share already mounted at %s" % existing)
        return existing
    if not shutil.which("gio"):
        raise NetShareError(
            "The 'gio' tool (part of glib / GVFS) is not "
            "installed, so network shares cannot be mounted. "
            "On Debian/Ubuntu install: gvfs and gvfs-backends "
            "(sudo apt-get install -y gvfs gvfs-backends), "
            "then try again.")
    url = smb_url(server, share, username)
    log("Mounting %s ..." % url)
    rc, out = _gio_mount(url, username, password)
    if out and "already" not in out.lower():
        for line in out.splitlines():
            log("gio: " + line)
    # The mount can take a moment to appear in the gvfs dir.
    for _ in range(20):
        found = find_mount(server, share)
        if found:
            log("Network share mounted at %s" % found)
            return found
        time.sleep(0.5)
    if rc != 0:
        hint = ""
        if re.search(r"permission|auth|password|credential",
                     out or "", re.IGNORECASE):
            hint = (" The server refused the sign-in -- check "
                    "the username/password (leave both blank "
                    "for a guest share).")
        elif re.search(r"host|name|network unreachable|"
                       r"no route|timed out", out or "",
                       re.IGNORECASE):
            hint = (" The server could not be reached -- "
                    "check the server name/address and that "
                    "the server is on.")
        raise NetShareError(
            "Could not mount %s (gio said: %s).%s"
            % (url, out or "no message", hint))
    raise NetShareError(
        "gio reported success mounting %s but the mount did "
        "not appear under %s -- the GVFS daemon may not be "
        "running in this session." % (url, gvfs_dir()))


# ---------------------------------------------------------------------
# shared entry point

def connect(server, share, username="", password="",
            log=lambda _m: None):
    """Connect to an SMB share and return a real local path
    (a GVFS mount directory on Linux, a UNC path on Windows).
    Raises NetShareError with a plain message on failure."""
    server = (server or "").strip()
    share = (share or "").strip()
    if not server or not share:
        raise NetShareError("Enter both a server and a share "
                            "name.")
    if is_windows():
        return connect_windows(server, share, log)
    return connect_linux(server, share, username, password, log)
