"""Pop-fe GUI engine.

Everything that is not the window lives here:

* first-run setup of sahlberg/pop-fe itself (clone, venv, its own
  --install, helper verification, Linux system packages via pkexec),
* disc scanning and SMART GROUPING: each disc's identity (game id +
  title) is read from the disc by pop-fe's OWN extraction code
  (get_disc_id / get_title_from_game, imported from the cloned
  pop-fe), and discs are grouped into one queue entry only when
  their pop-fe database TITLE matches -- per-disc serials differ
  legitimately on multi-disc sets (verified: Evil Dead disc 1 is
  SLUS01072, disc 2 is SLUS01326, same title),
* cover resolution: libretro-thumbnails first (the family fetch
  policy in covers_fetch.py), placed where pop-fe looks first --
  as <cue>_cover.png beside disc 1's cue when that folder is
  writable and has no cover already (an existing local cover is
  NEVER overwritten), otherwise passed to pop-fe with --cover.
  When libretro has nothing, pop-fe's own psxdatacenter/assets
  scraper is the fallback (we simply pass no --cover),
* batch conversion: one pop-fe subprocess per queue entry with
  ALL of the entry's cues in disc order, output streamed to the
  log, a free-space check before each entry, and an honest
  per-entry result list -- failures are reported, never hidden.

pop-fe must run with its own directory as CWD (it resolves its
helpers relative to it and needs a writable pop-fe-work/ there),
so every subprocess is launched that way with the engine venv's
python. The caller's environment is inherited untouched (the
family proxy variables must survive or all network access
fake-fails).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
POPFE_REPO_URL = "https://github.com/sahlberg/pop-fe"
ENGINE_DIR = Path(os.environ.get("POPFE_GUI_ENGINE_DIR",
                                 str(APP_DIR / "engine")))
# When packaged (AppImage) the app folder itself is read-only, so
# AppRun points POPFE_GUI_DATA_DIR at a per-user folder; unpackaged
# runs keep the historical behaviour of living beside the app.
DATA_DIR = Path(os.environ.get("POPFE_GUI_DATA_DIR", str(APP_DIR)))
CACHE_DIR = DATA_DIR / "cache"
SETTINGS_PATH = DATA_DIR / "settings.json"
STATE_PATH = DATA_DIR / "state.json"
SESSION_LOG_PATH = DATA_DIR / "session-log.txt"

TARGETS = [
    ("psp", "PSP / Vita"),
    ("ps2", "PS2"),
    ("ps3", "PS3"),
    ("psio", "PSIO"),
    ("psc", "PS Classic"),
    ("retroarch", "RetroArch"),
]
TARGET_LABEL = dict(TARGETS)
RA_LAYOUTS = [("cue", "bin/cue (.m3u + cue/bin)"), ("pbp", "PBP (EBOOT)")]

DISC_EXTS = {".cue", ".ccd", ".img", ".bin", ".chd", ".zip"}
UNKNOWN_ID = "UNKN00000"


def _noop(_msg):
    pass


# ---------------------------------------------------------------------
# paths inside the pop-fe engine tree

def popfe_script():
    return ENGINE_DIR / "pop-fe.py"


def venv_dir():
    return ENGINE_DIR / ".venv"


def venv_python():
    if os.name == "nt":
        return venv_dir() / "Scripts" / "python.exe"
    return venv_dir() / "bin" / "python"


def engine_ready():
    return popfe_script().is_file() and venv_python().is_file()


def _run_stream(cmd, cwd, log, env=None, timeout=None):
    """Run a command, streaming merged output to log(); return rc."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=full_env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            bufsize=1)
    try:
        for line in proc.stdout:
            log(line.rstrip("\n"))
        return proc.wait(timeout=timeout)
    except Exception:
        proc.kill()
        raise


# ---------------------------------------------------------------------
# first-run setup

def check_system_deps():
    """[(name, ok, apt package)] for the Linux system pieces pop-fe
    needs. chdman is only needed for .chd inputs, the sndfile
    headers only for building atracdenc -- both reported honestly
    either way."""
    checks = [
        ("git", shutil.which("git") is not None, "git"),
        ("ffmpeg", shutil.which("ffmpeg") is not None, "ffmpeg"),
        ("chdman (.chd support)", shutil.which("chdman") is not None,
         "mame-tools"),
        ("cmake", shutil.which("cmake") is not None, "cmake"),
        ("libsndfile headers (atracdenc build)",
         Path("/usr/include/sndfile.h").exists()
         or subprocess.run(["pkg-config", "--exists", "sndfile"],
                           capture_output=True).returncode == 0,
         "libsndfile1-dev"),
    ]
    return checks


def ensure_system_deps(log=_noop):
    """Install missing Linux system packages via pkexec (one GUI
    password prompt). Returns True when nothing is missing after
    the attempt."""
    if os.name == "nt":
        return True
    missing = [(n, p) for n, ok, p in check_system_deps() if not ok]
    if not missing:
        log("System packages: all present "
            "(git, ffmpeg, chdman, cmake, libsndfile headers).")
        return True
    pkgs = sorted({p for _n, p in missing})
    log("Missing system packages: "
        + ", ".join(n for n, _p in missing))
    if not shutil.which("pkexec"):
        log("pkexec is not available, so I cannot install them "
            "for you. Install manually: sudo apt-get install -y "
            + " ".join(pkgs))
        return False
    log("Asking for your password once (pkexec) to install: "
        + " ".join(pkgs))
    rc = _run_stream(
        ["pkexec", "sh", "-c",
         "apt-get update -qq && apt-get install -y " + " ".join(pkgs)],
        Path("/"), log)
    still = [n for n, ok, _p in check_system_deps() if not ok]
    if still:
        log("Still missing after install attempt: " + ", ".join(still))
        return False
    log("System packages installed.")
    return True


_SUBMODULE_DIRS = ("Cue2cu2", "binmerge", "atracdenc",
                   "PSL1GHT", "psx-undither")


def _clone_complete():
    return popfe_script().is_file() and all(
        (ENGINE_DIR / d).is_dir() for d in _SUBMODULE_DIRS)


def _clone_popfe(log):
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    if _clone_complete():
        log("pop-fe source already present.")
        return True
    if popfe_script().is_file():
        # A previous clone was interrupted during the submodule
        # step -- finish it rather than starting over.
        log("pop-fe is present but its submodules are "
            "incomplete; finishing the submodule checkout ...")
        rc = _run_stream(
            ["git", "submodule", "update", "--init", "--recursive"],
            ENGINE_DIR, log)
        if rc == 0 and _clone_complete():
            log("pop-fe submodules completed.")
            return True
        log("Submodule checkout still failing (rc=%s) -- this "
            "is usually a network/GitHub error; run setup again "
            "when the connection cooperates." % rc)
        return False
    if any(ENGINE_DIR.iterdir()):
        log("Engine folder exists but has no pop-fe.py; "
            "cloning into a subfolder is not safe -- please clear "
            + str(ENGINE_DIR) + " and retry.")
        return False
    log("Cloning pop-fe (sahlberg/pop-fe) ...")
    rc = _run_stream(["git", "clone", "--recursive", POPFE_REPO_URL,
                      str(ENGINE_DIR)], APP_DIR, log)
    if rc != 0 or not _clone_complete():
        # One repair attempt: the main tree may have landed
        # while a submodule fetch failed transiently.
        if popfe_script().is_file():
            log("Clone reported errors; trying to finish the "
                "submodule checkout ...")
            _run_stream(["git", "submodule", "update", "--init",
                         "--recursive"], ENGINE_DIR, log)
        if not _clone_complete():
            log("git clone did not complete (rc=%s) -- this is "
                "usually a network/GitHub error; run setup "
                "again when the connection cooperates." % rc)
            return False
    log("pop-fe cloned.")
    return True


def _create_venv(log):
    if venv_python().is_file():
        log("pop-fe venv already present.")
        return True
    log("Creating pop-fe venv ...")
    rc = _run_stream([sys.executable, "-m", "venv", str(venv_dir())],
                     ENGINE_DIR, log)
    if rc != 0 or not venv_python().is_file():
        log("venv creation failed -- on Debian/Ubuntu install "
            "python3-venv, then run setup again.")
        return False
    return True


def _venv_env():
    vbin = str(venv_python().parent)
    return {"PATH": vbin + os.pathsep + os.environ.get("PATH", ""),
            "VIRTUAL_ENV": str(venv_dir())}


def _install_env():
    """Environment for the setup runs. Starts from the venv env
    and adds the workarounds pop-fe's helper builds need on a
    modern toolchain (all verified against pop-fe's installer):
    * CMAKE_POLICY_VERSION_MINIMUM=3.5 -- atracdenc's CMakeLists
      predates CMake 4, which refuses old minimums outright.
    * CXXFLAGS -include cstdint -- atracdenc's headers use the
      fixed-width integer types without including <cstdint>;
      current GCC no longer pulls it in transitively.
    * CC/LDSHARED=gcc when clang is absent -- the venv python's
      sysconfig names clang as its build compiler (the packaged
      Python is clang-built), and distutils believes it; without
      this the PS3 pkgcrypt extension cannot build on a machine
      that has gcc but no clang."""
    env = _venv_env()
    env.setdefault("CMAKE_POLICY_VERSION_MINIMUM", "3.5")
    cxxflags = env.get("CXXFLAGS", os.environ.get("CXXFLAGS", ""))
    if "cstdint" not in cxxflags:
        env["CXXFLAGS"] = (cxxflags + " -include cstdint").strip()
    if not shutil.which("clang") and shutil.which("gcc"):
        env.setdefault("CC", "gcc")
        env.setdefault("LDSHARED", "gcc -shared")
    return env


def _install_popfe_deps(log):
    # setuptools FIRST: pop-fe's ps3py helper builds a C extension
    # through distutils, which no longer ships with Python 3.12+;
    # the setuptools in the venv provides it. Without this the
    # PS3 helper silently fails to build (pop-fe's installer
    # ignores helper build failures).
    log("Installing setuptools into the pop-fe venv "
        "(needed by pop-fe's PS3 helper build) ...")
    _run_stream([str(venv_python()), "-m", "pip", "install",
                 "setuptools"], ENGINE_DIR, log, env=_install_env())
    log("Running pop-fe's own installer (pop-fe.py --install). "
        "This downloads Python packages and builds pop-fe's "
        "helper tools; it can take several minutes ...")
    rc = _run_stream([str(venv_python()), "pop-fe.py", "--install"],
                     ENGINE_DIR, log, env=_install_env())
    log("pop-fe --install finished (rc=%s)." % rc)
    return True


def verify_helpers(log=_noop):
    """pop-fe's installer ignores helper build failures, so check
    the important helpers actually exist and say so plainly."""
    if os.name == "nt":
        atr = ENGINE_DIR / "atracdenc" / "src" / "atracdenc.exe"
        lcp = ENGINE_DIR / "lcp.exe"
    else:
        atr = ENGINE_DIR / "atracdenc" / "src" / "atracdenc"
        lcp = ENGINE_DIR / "lcp"
    checks = {
        "atracdenc (PSP/PS3 CD audio)": atr.is_file(),
        "lcp (libcrypt patcher)": lcp.is_file(),
        "ps3py pkgcrypt (PS3 PKG)":
            bool(list((ENGINE_DIR / "PSL1GHT" / "tools" / "ps3py")
                      .glob("pkgcrypt*.so")))
            or bool(list((ENGINE_DIR / "PSL1GHT" / "tools"
                          / "ps3py").glob("pkgcrypt*.pyd"))),
        "psx-undither": (ENGINE_DIR / "psx-undither" / "build"
                         / "psxund").is_file()
                        or (ENGINE_DIR / "psx-undither" / "build"
                            / "psxund.exe").is_file(),
        "binmerge (multi-bin discs)":
            (ENGINE_DIR / "binmerge" / "binmerge").is_file()
            or (ENGINE_DIR / "binmerge.exe").is_file()
            or (ENGINE_DIR / "binmerge" / "binmerge.exe").is_file(),
    }
    for name, ok in checks.items():
        log("helper %-32s %s" % (name, "OK" if ok else "MISSING"))
    return checks


def _repair_lcp(log):
    """pop-fe's installer extracts the libcrypt patcher with
    'unzip -f' -- freshen mode, which silently skips files that
    do not exist on disk yet, so on every fresh install lcp is
    left trapped inside its zip (verified against pop-fe's
    install code and the zip's contents). If the zip is here
    and lcp is not, pull it out properly."""
    if os.name == "nt":
        return
    lcp = ENGINE_DIR / "lcp"
    if lcp.is_file():
        return
    zips = sorted(ENGINE_DIR.glob("libcrypt-patcher-*.zip"))
    if not zips:
        return
    try:
        import zipfile
        with zipfile.ZipFile(zips[0]) as zf:
            names = [n for n in zf.namelist() if n.endswith("/lcp")]
            if not names:
                return
            data = zf.read(names[0])
        lcp.write_bytes(data)
        lcp.chmod(0o755)
        log("Repaired the libcrypt patcher (lcp): pop-fe's "
            "installer leaves it inside its zip on fresh "
            "installs (an upstream 'unzip -f' quirk).")
    except Exception as e:  # noqa: BLE001
        log("Could not repair the libcrypt patcher (%s); it "
            "will be reported missing below." % e)


def ensure_popfe(log=_noop):
    """Full first-run setup. Returns (ok, helper_report)."""
    if not _clone_popfe(log):
        return False, {}
    if not _create_venv(log):
        return False, {}
    _install_popfe_deps(log)
    _repair_lcp(log)
    helpers = verify_helpers(log)
    missing = [n for n, ok in helpers.items() if not ok]
    if missing:
        log("WARNING: pop-fe is installed but these helpers are "
            "missing: " + ", ".join(missing)
            + ". Conversions needing them will fail inside pop-fe; "
              "the failure will show in the log, never silently.")
    else:
        log("pop-fe engine ready: all helpers present.")
    return engine_ready(), helpers


# ---------------------------------------------------------------------
# disc identity scanning (pop-fe's own extraction, imported)

SCAN_HELPER = r'''
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys


def emit(obj):
    print("SCANJSON " + json.dumps(obj), flush=True)


def main():
    scratch = sys.argv[1]
    files = sys.argv[2:]
    # pop-fe imports its sibling modules (bchunk, gamedb, ...)
    # by bare name; as a script its own folder is sys.path[0],
    # so put it there explicitly when importing it as a module.
    sys.path.insert(0, os.getcwd())
    spec = importlib.util.spec_from_file_location(
        "popfe", os.path.join(os.getcwd(), "pop-fe.py"))
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    os.makedirs(scratch, exist_ok=True)
    for idx, path in enumerate(files):
        cue, real = path, path
        workdir = os.path.join(scratch, "f%03d" % idx)
        try:
            os.makedirs(workdir, exist_ok=True)
            low = path.lower()
            if low.endswith(".ccd"):
                tmpcue = os.path.join(workdir, "TMP.cue")
                pf.write_cue(pf.ccd2cue(pf.parse_ccd(path)), tmpcue)
                src_dir = os.path.dirname(os.path.abspath(path))
                txt = open(tmpcue).read()

                def _abs(m):
                    name = m.group(1)
                    if os.path.isabs(name):
                        return m.group(0)
                    return 'FILE "%s" %s' % (
                        os.path.join(src_dir, name), m.group(2))
                txt = re.sub(r'FILE "([^"]+)" (\S+)', _abs, txt)
                open(tmpcue, "w").write(txt)
                cue = tmpcue
            elif low.endswith((".bin", ".img")):
                tmpcue = os.path.join(workdir, "TMP.cue")
                with open(tmpcue, "w") as f:
                    f.write('FILE "%s" BINARY\n' % os.path.abspath(path))
                    f.write("  TRACK 01 MODE2/2352\n")
                    f.write("    INDEX 01 00:00:00\n")
                cue = tmpcue
                real = tmpcue
            elif low.endswith(".zip"):
                import zipfile
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(workdir)
                found = None
                for root, _dirs, names in os.walk(workdir):
                    for n in sorted(names):
                        if n.lower().endswith(".cue"):
                            found = os.path.join(root, n)
                            break
                    if found:
                        break
                if not found:
                    raise ValueError("no .cue file inside the zip")
                cue = found
                real = found
            elif low.endswith(".chd"):
                tmpcue = os.path.join(workdir, "CD.cue")
                tmpbin = os.path.join(workdir, "CD.bin")
                subprocess.run(["chdman", "extractcd", "-f", "-i",
                                path, "-ob", tmpbin, "-o", tmpcue],
                               check=True, capture_output=True)
                cue = tmpcue
                real = tmpcue
            tmp_iso = os.path.join(workdir, "track1.iso")
            rid, md5 = pf.get_disc_id(cue, real, tmp_iso)
            title = pf.get_title_from_game(rid)
            emit({"path": path, "id": rid, "title": title,
                  "md5": md5})
        except Exception as e:  # noqa: BLE001
            emit({"path": path, "id": "UNKN00000",
                  "title": "Unknown", "md5": "",
                  "error": "%s: %s" % (type(e).__name__, e)})
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


main()
'''


def _scan_helper_path():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / "scan_helper.py"
    if not p.is_file() or p.read_text() != SCAN_HELPER:
        p.write_text(SCAN_HELPER)
    return p


def scan_identities(file_paths, log=_noop):
    """{path: {'id','title','md5'}} read by pop-fe's own code.
    Files pop-fe cannot identify come back with id UNKN00000."""
    results = {}
    if not file_paths:
        return results
    helper = _scan_helper_path()
    scratch = CACHE_DIR / "scan-scratch"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    cmd = [str(venv_python()), "-u", str(helper), str(scratch)]
    todo = []
    for f in file_paths:
        if f.lower().endswith(".chd") and not shutil.which("chdman"):
            log("chdman is not installed, so %s cannot be "
                "identified (pop-fe needs chdman for .chd files). "
                "It will be queued as 'identity unknown'." % f)
            results[f] = {"id": UNKNOWN_ID, "title": "Unknown",
                          "md5": ""}
        else:
            todo.append(f)
    if todo:
        env = dict(os.environ)
        env.update(_venv_env())
        proc = subprocess.Popen(
            cmd + todo, cwd=str(ENGINE_DIR), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("SCANJSON "):
                try:
                    obj = json.loads(line[len("SCANJSON "):])
                    results[obj["path"]] = {
                        "id": obj.get("id") or UNKNOWN_ID,
                        "title": obj.get("title") or "Unknown",
                        "md5": obj.get("md5") or ""}
                    if obj.get("error"):
                        log("identify %s: %s"
                            % (obj["path"], obj["error"]))
                except ValueError:
                    log(line)
            elif line.strip():
                log("pop-fe: " + line)
        proc.wait()
    for f in file_paths:
        results.setdefault(f, {"id": UNKNOWN_ID, "title": "Unknown",
                               "md5": ""})
    shutil.rmtree(scratch, ignore_errors=True)
    return results


# ---------------------------------------------------------------------
# archive inputs (.zip / .7z)

ARCHIVE_EXTS = {".zip", ".7z"}


def _extract_zip(archive, dest):
    """Extract a .zip member by member, refusing any member
    whose path would escape the destination (zip-slip)."""
    dest = Path(dest).resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if target != dest and dest not in target.parents:
                raise ValueError(
                    "unsafe path inside the zip: %s"
                    % info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, \
                        open(target, "wb") as out:
                    shutil.copyfileobj(src, out)


def _extract_7z(archive, dest):
    try:
        import py7zr
    except ImportError:
        raise RuntimeError(
            "the py7zr package is not installed, so .7z "
            "archives cannot be opened (it is bundled with "
            "the app packages and listed in requirements.txt "
            "for source installs)")
    with py7zr.SevenZipFile(archive, "r") as sz:
        sz.extractall(path=str(dest))


def extract_archive(archive, log=_noop):
    """Extract one .zip/.7z into a fresh folder under the
    LOCAL cache (cache/archive-scratch/) and return that
    folder, or None on failure. The source archive is only
    ever read -- extraction NEVER happens beside it, so an
    archive picked from a network share is unpacked onto
    local disk, not back onto the share. Each extraction
    gets its own folder and is left in place for the rest
    of the session (queue entries point into it); the whole
    archive-scratch folder can be deleted by hand whenever
    the app is closed."""
    archive = Path(archive)
    scratch = CACHE_DIR / "archive-scratch"
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_",
                  archive.stem) or "archive"
    tag = hashlib.md5(
        str(archive).encode("utf-8")).hexdigest()[:8]
    dest = scratch / ("%s-%s" % (stem, tag))
    n = 2
    while dest.exists():
        dest = scratch / ("%s-%s-%d" % (stem, tag, n))
        n += 1
    dest.mkdir(parents=True)
    log("Extracting %s to local scratch %s ..."
        % (archive.name, dest))
    try:
        if archive.suffix.lower() == ".zip":
            _extract_zip(archive, dest)
        else:
            _extract_7z(archive, dest)
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(dest, ignore_errors=True)
        log("Could not extract %s (%s: %s) -- skipped. "
            "Password-protected archives are not supported."
            % (archive.name, type(e).__name__, e))
        return None
    return dest


def expand_archives(paths, log=_noop):
    """Replace every archive input with the local folder it
    extracts to. Archives are recognised both as directly
    picked files and inside picked folders (at any depth);
    folders themselves stay in the list too, so loose discs
    sitting beside an archive are still collected. The
    returned roots are then scanned/grouped exactly as if
    the loose files had been picked."""
    roots = []
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix.lower() in ARCHIVE_EXTS:
            dest = extract_archive(p, log)
            if dest is not None:
                roots.append(str(dest))
        elif p.is_dir():
            roots.append(raw)
            for arc in sorted(p.rglob("*")):
                if arc.is_file() \
                        and arc.suffix.lower() in ARCHIVE_EXTS:
                    dest = extract_archive(arc, log)
                    if dest is not None:
                        roots.append(str(dest))
        else:
            roots.append(raw)
    return roots


# ---------------------------------------------------------------------
# collecting + grouping

_TRACK_SUFFIX = re.compile(r"\s*\(Track\s+\d+\)$", re.IGNORECASE)
_DISC_NUM = re.compile(
    r"(?i)(?:disc|disk|cd)\s*[-_.]?\s*(\d{1,2})\b")
_OF_NUM = re.compile(r"\((\d{1,2})\s*of\s*\d{1,2}\)", re.IGNORECASE)


def disc_number(filename):
    m = _DISC_NUM.search(filename) or _OF_NUM.search(filename)
    return int(m.group(1)) if m else 0


def collect_disc_files(paths):
    """Expand files/folders into a sorted list of disc image files.
    Bare .bin/.img files that belong to a sibling .cue/.ccd/.chd
    (same base name, allowing ' (Track N)' suffixes) are skipped --
    the cue/ccd already speaks for them."""
    found = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in DISC_EXTS \
                        and f.suffix.lower() not in ARCHIVE_EXTS:
                    found.append(f)
        elif p.is_file() and p.suffix.lower() in DISC_EXTS \
                and p.suffix.lower() not in ARCHIVE_EXTS:
            found.append(p)
    by_dir = {}
    for f in found:
        by_dir.setdefault(f.parent, []).append(f)
    out = []
    for folder, files in by_dir.items():
        speakers = {f.stem for f in files
                    if f.suffix.lower() in (".cue", ".ccd", ".chd")}
        for f in files:
            if f.suffix.lower() in (".bin", ".img"):
                base = _TRACK_SUFFIX.sub("", f.stem)
                if base in speakers or f.stem in speakers:
                    continue
            out.append(f)
    return sorted(out, key=lambda p: str(p).lower())


def _norm_title(title):
    return " ".join((title or "").upper().split()).strip()


def build_entries(paths, log=_noop):
    """Scan + group. Returns a list of entry dicts:
    {title, known, ids, discs: [{path, id, title, number}],
     cover_path, cover_mode, status, outputs}."""
    files = collect_disc_files(expand_archives(paths, log))
    if not files:
        return []
    log("Identifying %d disc file(s) with pop-fe ..." % len(files))
    ident = scan_identities([str(f) for f in files], log)
    groups = {}
    order = []
    for f in files:
        info = ident.get(str(f), {"id": UNKNOWN_ID,
                                  "title": "Unknown", "md5": ""})
        rid = info.get("id") or UNKNOWN_ID
        title = info.get("title") or "Unknown"
        known = rid != UNKNOWN_ID
        if known and title != "Unknown":
            key = ("title", _norm_title(title))
        elif known:
            key = ("id", rid)
        else:
            key = ("file", str(f))
        if key not in groups:
            groups[key] = {
                "title": title if known and title != "Unknown"
                         else f.stem,
                "known": known,
                "ids": [],
                "discs": [],
                "cover_path": None,
                "cover_mode": "none",
                "status": "queued",
                "outputs": [],
            }
            order.append(key)
        entry = groups[key]
        entry["discs"].append({
            "path": str(f), "id": rid, "title": title,
            "number": disc_number(f.name)})
        if rid not in entry["ids"]:
            entry["ids"].append(rid)
        log("%s -> %s / %s" % (f.name, rid, title))
    entries = []
    for key in order:
        entry = groups[key]
        entry["discs"].sort(key=lambda d: (
            d["number"] == 0, d["number"], d["id"], d["path"]))
        entries.append(entry)
        log("Queued: %s (%d disc(s), %s)"
            % (entry["title"], len(entry["discs"]),
               ", ".join(entry["ids"])))
    return entries


def build_set_entry(paths, log=_noop):
    """Deliberate multi-disc add: EVERYTHING given (files, or one
    folder holding them) is treated as the discs of ONE set and
    force-grouped into a single queue entry -- the user picked
    these discs as a set, so no title matching gets a say. Disc
    identities are still read with pop-fe's own code (for the
    entry's title, its per-disc IDs and the disc order); the
    title is the one most of the discs agree on."""
    files = collect_disc_files(expand_archives(paths, log))
    if not files:
        return []
    log("Identifying %d disc file(s) with pop-fe ..." % len(files))
    ident = scan_identities([str(f) for f in files], log)
    discs = []
    for f in files:
        info = ident.get(str(f), {"id": UNKNOWN_ID,
                                  "title": "Unknown", "md5": ""})
        rid = info.get("id") or UNKNOWN_ID
        title = info.get("title") or "Unknown"
        discs.append({"path": str(f), "id": rid, "title": title,
                      "number": disc_number(f.name)})
        log("%s -> %s / %s" % (f.name, rid, title))
    discs.sort(key=lambda d: (d["number"] == 0, d["number"],
                              d["id"], d["path"]))
    known_titles = [d["title"] for d in discs
                    if d["id"] != UNKNOWN_ID
                    and d["title"] != "Unknown"]
    title = None
    if known_titles:
        counts = {}
        for t in known_titles:
            counts[t] = counts.get(t, 0) + 1
        best = max(counts.values())
        title = next(t for t in known_titles if counts[t] == best)
    known = bool(known_titles)
    if not title:
        title = Path(discs[0]["path"]).stem
    ids = []
    for d in discs:
        if d["id"] not in ids:
            ids.append(d["id"])
    entry = {
        "title": title,
        "known": known,
        "ids": ids,
        "discs": discs,
        "cover_path": None,
        "cover_mode": "none",
        "status": "queued",
        "outputs": [],
    }
    log("Queued set: %s (%d disc(s), %s)"
        % (entry["title"], len(entry["discs"]), ", ".join(ids)))
    return [entry]


# ---------------------------------------------------------------------
# covers (libretro first, pop-fe's own scraper as fallback)

def dashed(serial):
    if serial and len(serial) == 9:
        return serial[:4] + "-" + serial[4:]
    return serial or ""


def local_cover_path(disc_path):
    """The cover file pop-fe itself looks for first: the disc
    file's path with its extension swapped for '_cover.png'
    (pop-fe computes it as path[:-4] + '_cover.png')."""
    s = str(disc_path)
    return Path(s[:-4] + "_cover.png")


def resolve_cover(entry, log=_noop):
    """Work out the cover pop-fe will use for this entry and fetch
    it if libretro has it. Sets entry['cover_path']/['cover_mode']:
    'local' (already beside disc 1), 'placed' (libretro cover
    written beside disc 1 -- the folder was writable and had no
    cover; an existing cover is NEVER overwritten), 'cached'
    (libretro cover in the app cache, passed with --cover) or
    'none' (libretro had nothing; pop-fe's own scraper decides
    during conversion). Returns the cover Path or None."""
    import covers_fetch
    covers_fetch.CACHE_DIR = str(CACHE_DIR)
    disc1 = entry["discs"][0]["path"]
    local = local_cover_path(disc1)
    if local.is_file():
        entry["cover_path"] = str(local)
        entry["cover_mode"] = "local"
        log("Cover: using the existing local cover %s" % local)
        return local
    serial = ""
    for rid in entry["ids"]:
        if rid != UNKNOWN_ID:
            serial = dashed(rid)
            break
    data = None
    try:
        data = covers_fetch.fetch_cover(
            {"system": "psx", "serial": serial,
             "title": entry["title"]})
    except Exception as e:  # noqa: BLE001
        log("Cover: libretro lookup failed (%s); pop-fe's own "
            "scraper will be used instead." % e)
    if not data:
        entry["cover_path"] = None
        entry["cover_mode"] = "none"
        log("Cover: libretro has no cover for %s; pop-fe's own "
            "scraper will fetch art during conversion."
            % entry["title"])
        return None
    folder = Path(disc1).parent
    if os.access(folder, os.W_OK) and not local.exists():
        try:
            local.write_bytes(data)
            entry["cover_path"] = str(local)
            entry["cover_mode"] = "placed"
            log("Cover: libretro cover saved beside disc 1 as %s "
                "(where pop-fe looks first)." % local)
            return local
        except OSError as e:
            log("Cover: could not save beside disc 1 (%s); "
                "using the app cache instead." % e)
    dest = CACHE_DIR / "covers" / (
        re.sub(r"[^A-Za-z0-9_.-]+", "_", entry["title"]) + ".png")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    except OSError as e:
        log("Cover: could not cache the libretro cover (%s)." % e)
        entry["cover_path"] = None
        entry["cover_mode"] = "none"
        return None
    entry["cover_path"] = str(dest)
    entry["cover_mode"] = "cached"
    log("Cover: libretro cover cached at %s (passed to pop-fe "
        "with --cover)." % dest)
    return dest


# ---------------------------------------------------------------------
# conversion

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename(name):
    name = _ILLEGAL_CHARS.sub("", name or "")
    return " ".join(name.split()).strip().strip(".") or "Game"


def auto_pkg_path(entry, outdir):
    """PS3 packages are never named by hand: '<Title> [<FirstDisc
    ID>].pkg' from the scanned data, one name per set, and an
    existing file is never overwritten -- ' (2)', ' (3)' ... are
    appended instead."""
    first_id = next((i for i in entry["ids"] if i != UNKNOWN_ID),
                    UNKNOWN_ID)
    base = "%s [%s]" % (sanitize_filename(entry["title"]), first_id)
    candidate = Path(outdir) / (base + ".pkg")
    n = 2
    while candidate.exists():
        candidate = Path(outdir) / ("%s (%d).pkg" % (base, n))
        n += 1
    return candidate


def build_argv(entry, target, outdir, ra_layout="cue"):
    outdir = Path(outdir)
    argv = [str(venv_python()), "-u", "pop-fe.py", "-v"]
    if target == "psp":
        argv += ["--psp-dir", str(outdir)]
    elif target == "ps2":
        argv += ["--ps2-dir", str(outdir)]
    elif target == "ps3":
        argv += ["--ps3-pkg", str(auto_pkg_path(entry, outdir))]
    elif target == "psio":
        argv += ["--psio-dir", str(outdir)]
    elif target == "psc":
        argv += ["--psc-dir", str(outdir)]
    elif target == "retroarch":
        flag = ("--retroarch-pbp-dir" if ra_layout == "pbp"
                else "--retroarch-cue-dir")
        argv += [flag, str(outdir)]
    else:
        raise ValueError("unknown target %r" % target)
    # NEVER ship the XMB highlight theme song: pop-fe would
    # otherwise download an SND0 for games that have one in its
    # database (or pick up a theme/local .snd0) and embed it.
    # '--snd0 no' is pop-fe's own off switch -- it short-circuits
    # every SND0 source. Cover art is unaffected (it travels via
    # --cover / ICON0), and the game's own music is part of the
    # disc and untouched.
    argv += ["--snd0", "no"]
    if entry.get("cover_path"):
        argv += ["--cover", entry["cover_path"]]
    argv += [d["path"] for d in entry["discs"]]
    return argv


_CUE_FILE = re.compile(r'FILE\s+"([^"]+)"', re.IGNORECASE)


def entry_bytes(entry):
    """Rough size of an entry's source data (for the free-space
    check): the disc files plus whatever a .cue references."""
    total = 0
    for d in entry["discs"]:
        p = Path(d["path"])
        try:
            total += p.stat().st_size
        except OSError:
            continue
        if p.suffix.lower() == ".cue":
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            for name in _CUE_FILE.findall(text):
                ref = Path(name)
                if not ref.is_absolute():
                    ref = p.parent / name
                try:
                    total += ref.stat().st_size
                except OSError:
                    pass
    return total


def _snapshot(folder):
    snap = {}
    folder = Path(folder)
    if not folder.is_dir():
        return snap
    for f in folder.rglob("*"):
        if f.is_file():
            try:
                st = f.stat()
                snap[str(f.relative_to(folder))] = (st.st_size,
                                                    st.st_mtime_ns)
            except OSError:
                pass
    return snap


class QueueRunner:
    """Sequential batch conversion. cancel() stops the run: the
    current pop-fe subprocess is terminated and every entry that
    has not run yet is reported as cancelled -- the summary always
    separates succeeded / failed / cancelled and keeps the
    reason for every failure."""

    def __init__(self):
        self._cancel = threading.Event()
        self._proc = None

    def cancel(self):
        self._cancel.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def run(self, entries, target, outdir, ra_layout="cue",
            log=_noop, status=None):
        status = status or (lambda _i, _s: None)
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        summary = {"ok": [], "failed": [], "cancelled": []}
        for i, entry in enumerate(entries):
            title = entry["title"]
            if self._cancel.is_set():
                entry["status"] = "cancelled"
                status(i, "cancelled")
                summary["cancelled"].append(title)
                continue
            status(i, "fetching cover")
            entry["status"] = "fetching cover"
            try:
                resolve_cover(entry, log)
            except Exception as e:  # noqa: BLE001
                log("Cover step failed for %s (%s) -- converting "
                    "without a GUI-fetched cover." % (title, e))
            argv = build_argv(entry, target, outdir, ra_layout)
            need = int(entry_bytes(entry) * 1.6) + 256 * 1024 * 1024
            free = min(shutil.disk_usage(str(ENGINE_DIR)).free,
                       shutil.disk_usage(str(outdir)).free)
            if free < need:
                reason = ("not enough free space: this disc set "
                          "needs about %.1f GB free for pop-fe's "
                          "temp files and the output, only %.1f GB "
                          "is available"
                          % (need / 1e9, free / 1e9))
                entry["status"] = "failed: " + reason
                status(i, entry["status"])
                summary["failed"].append((title, reason))
                log("FAILED %s -- %s" % (title, reason))
                continue
            log("Converting %s -> %s (%d disc(s)) ..."
                % (title, TARGET_LABEL.get(target, target),
                   len(entry["discs"])))
            log("  " + " ".join(argv[3:]))
            status(i, "converting")
            entry["status"] = "converting"
            before = _snapshot(outdir)
            tail = []
            rc = None
            env = dict(os.environ)
            env.update(_venv_env())
            try:
                self._proc = subprocess.Popen(
                    argv, cwd=str(ENGINE_DIR), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)
                for line in self._proc.stdout:
                    line = line.rstrip("\n")
                    if line.strip():
                        log("  " + line)
                        tail.append(line)
                        tail = tail[-12:]
                rc = self._proc.wait()
            except Exception as e:  # noqa: BLE001
                rc = -1
                tail.append("%s: %s" % (type(e).__name__, e))
            finally:
                self._proc = None
            after = _snapshot(outdir)
            entry["outputs"] = sorted(
                k for k in after
                if k not in before or before[k] != after[k])
            # pop-fe can exit 0 without producing anything: its
            # PS3 packer (pkg.py) is called with the return code
            # ignored and 'Finished' printed unconditionally, and
            # other steps swallow errors the same way. An exit
            # code of 0 is therefore NOT proof -- verify the
            # output really exists before calling it done.
            if rc == 0:
                missing = None
                if target == "ps3":
                    pkg_arg = argv[argv.index("--ps3-pkg") + 1]
                    pkg_file = Path(pkg_arg)
                    if not (pkg_file.is_file()
                            and pkg_file.stat().st_size > 0):
                        missing = ("pop-fe reported success but "
                                   "the package %s was not "
                                   "created -- its PS3 packer "
                                   "step fails silently upstream; "
                                   "the real error is in the log "
                                   "above (disk full/quota is the "
                                   "usual cause)"
                                   % pkg_file.name)
                elif not entry["outputs"]:
                    missing = ("pop-fe reported success but no "
                               "new or changed files appeared "
                               "in the output folder -- treating "
                               "this as a failure, not a success")
                if missing:
                    entry["status"] = "failed"
                    status(i, "failed")
                    summary["failed"].append((title, missing))
                    log("FAILED %s -- %s" % (title, missing))
                    continue
            if self._cancel.is_set() and rc != 0:
                entry["status"] = "cancelled"
                status(i, "cancelled")
                summary["cancelled"].append(title)
                log("Cancelled during %s." % title)
            elif rc == 0:
                entry["status"] = "done"
                status(i, "done")
                summary["ok"].append(title)
                log("DONE %s -> %s"
                    % (title, ", ".join(entry["outputs"]) or
                       "(output already existed)"))
            else:
                reason = ("pop-fe exited with code %s" % rc)
                if tail:
                    reason += "; last output: " + " | ".join(tail[-3:])
                entry["status"] = "failed"
                status(i, "failed")
                summary["failed"].append((title, reason))
                log("FAILED %s -- %s" % (title, reason))
        log("Batch finished: %d converted, %d failed, %d cancelled."
            % (len(summary["ok"]), len(summary["failed"]),
               len(summary["cancelled"])))
        for title, reason in summary["failed"]:
            log("  FAILED: %s -- %s" % (title, reason))
        return summary
