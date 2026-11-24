"""Pop-fe GUI — libretro cover fetcher.

Copied from the Recomp Steam Importer's covers_fetch.py (itself a
compact implementation of the Recomp Covers fetch policy; the
full tool is the separate Recomp Covers app): the libretro-
thumbnails GitHub mirror first, the canonical thumbnails host
second, up to 3 attempts with backoff on network errors, NO retry
on a 404 (a missing name is definitive — the next name variant
gets its turn), and every download validated as a real image
before it is used. Name variants follow the converter family's
rules (dump flags stripped, (JUE)->(World), (V1.1)->(Rev 1),
region-suffixed forms, GB/GBC enhancement suffixes), PSX covers
resolve through the Redump DAT by serial, and GB<->GBC sets fall
through to each other. When the exact variants miss, a fuzzy
match against the set's real file listing (a tree-only git
clone, cached a week — the same mechanism Recomp Covers uses)
finds names like 'Tetris (World) (Rev 1).png'; only when that
also misses does the fetcher report "no cover".
"""
import io
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "PopFEGUI/1.0"
CACHE_DIR = None        # popfe_engine points this at the app cache dir

SYSTEMS = {
    "psx": ("Sony_-_PlayStation", "Sony%20-%20PlayStation", ()),
    "nes": ("Nintendo_-_Nintendo_Entertainment_System",
            "Nintendo%20-%20Nintendo%20Entertainment%20System", ()),
    "gb": ("Nintendo_-_Game_Boy", "Nintendo%20-%20Game%20Boy", ("gbc",)),
    "gbc": ("Nintendo_-_Game_Boy_Color",
            "Nintendo%20-%20Game%20Boy%20Color", ("gb",)),
    "snes": ("Nintendo_-_Super_Nintendo_Entertainment_System",
             "Nintendo%20-%20Super%20Nintendo%20Entertainment%20System",
             ()),
    "gba": ("Nintendo_-_Game_Boy_Advance",
            "Nintendo%20-%20Game%20Boy%20Advance", ()),
    "n64": ("Nintendo_-_Nintendo_64", "Nintendo%20-%20Nintendo%2064", ()),
    "genesis": ("Sega_-_Mega_Drive_-_Genesis",
                "Sega%20-%20Mega%20Drive%20-%20Genesis", ()),
    "vb": ("Nintendo_-_Virtual_Boy", "Nintendo%20-%20Virtual%20Boy", ()),
    "sms": ("Sega_-_Master_System_-_Mark_III",
            "Sega%20-%20Master%20System%20-%20Mark%20III", ()),
    "gg": ("Sega_-_Game_Gear", "Sega%20-%20Game%20Gear", ()),
}

GH_TEMPLATE = ("https://raw.githubusercontent.com/libretro-thumbnails/"
               "{gh}/master/Named_Boxarts/{name}.png")
CANON_TEMPLATE = ("https://thumbnails.libretro.com/"
                  "{canon}/Named_Boxarts/{name}.png")
REDUMP_DAT_URL = ("https://raw.githubusercontent.com/libretro/"
                  "libretro-database/master/metadat/redump/"
                  "Sony%20-%20PlayStation.dat")

_ILLEGAL = dict.fromkeys(map(ord, '\\/:*?"<>|'), None)


def _lr_encode(name):
    return name.translate(_ILLEGAL).replace("&", "_")


def _get_once(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:  # noqa: BLE001
        return None, None


def _get_retry(url, attempts=3, timeout=25):
    for i in range(attempts):
        data, status = _get_once(url, timeout)
        if data is not None:
            return data
        if status == 404:
            return None
        if i + 1 < attempts:
            time.sleep(1 * (i + 1))
    return None


def valid_image(data):
    if not data or len(data) < 4000:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) < 24:
            return False
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w >= 100 and h >= 100
    return data[:3] == b"\xff\xd8\xff"


def to_png(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        out = io.BytesIO()
        im.convert("RGB").save(out, "PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return None


def _cache_path(system, name):
    if CACHE_DIR is None:
        return None
    return Path(CACHE_DIR) / "covers" / system / (_lr_encode(name) + ".png")


def _try_name(syskey, name):
    cache = _cache_path(syskey, name)
    if cache is not None and cache.is_file():
        data = cache.read_bytes()
        if valid_image(data):
            return data
    gh, canon, _ = SYSTEMS[syskey]
    enc = urllib.parse.quote(_lr_encode(name), safe="")
    for url in (GH_TEMPLATE.format(gh=gh, name=enc),
                CANON_TEMPLATE.format(canon=canon, name=enc)):
        data = _get_retry(url)
        if data and valid_image(data):
            png = to_png(data)
            if png:
                if cache is not None:
                    try:
                        cache.parent.mkdir(parents=True, exist_ok=True)
                        cache.write_bytes(png)
                    except OSError:
                        pass
                return png
    return None


def name_variants(title, system=None):
    title = (title or "").strip()
    if not title:
        return []
    out = []

    def add(n):
        n = (n or "").strip()
        if n and n not in out:
            out.append(n)

    add(title)
    no_brackets = re.sub(r"\s*\[[^\]]*\]", "", title).strip()
    add(no_brackets)
    norm = re.sub(r"\((JUE|JU|UE|U|J)\)", "(World)", no_brackets)
    norm = re.sub(r"\(V(\d+)\.(\d+)\)", r"(Rev \2)", norm)
    add(norm)
    core = title.split(" (")[0].strip()
    add(core)
    for base in (core, norm.split(" (")[0].strip()):
        for suffix in (" (USA)", " (USA, Europe)", " (World)"):
            add(base + suffix)
    if system in ("gb", "gbc"):
        for base in list(out):
            add(base + " (SGB Enhanced) (GB Compatible)")
            add(base + " (SGB Enhanced)")
            add(base + " (GB Compatible)")
    return out


# --- set listing (fuzzy fallback), ported from Recomp Covers ---
# Some sets name files with conventions no variant generation
# will guess ('Tetris (World) (Rev 1).png'). As a last resort,
# list the set's real file names with a tree-only git clone (no
# API quota involved), cache the list for a week, and match on
# core title + regions.

LISTING_TTL = 7 * 24 * 3600
REGION_WORDS = {
    "usa", "europe", "japan", "world", "australia", "brazil",
    "korea", "canada", "france", "germany", "spain", "italy",
    "netherlands", "sweden", "denmark", "finland", "norway",
    "asia", "taiwan", "china", "russia", "mexico", "portugal",
    "greece", "india", "israel", "uk", "scandinavia",
    "latin america", "new zealand",
}


def _set_listing(syskey):
    """All Named_Boxarts stems in a system's set, cached a week."""
    if CACHE_DIR is None:
        return []
    dest = Path(CACHE_DIR) / "listings" / f"{syskey}.json"
    stale = []
    try:
        blob = json.loads(dest.read_text())
        stale = blob.get("names", [])
        if time.time() - blob.get("ts", 0) < LISTING_TTL and stale:
            return stale
    except Exception:  # noqa: BLE001
        pass
    if not shutil.which("git"):
        return stale
    tmp = Path(tempfile.mkdtemp(prefix="importer-list-"))
    try:
        repo = ("https://github.com/libretro-thumbnails/"
                + SYSTEMS[syskey][0])
        subprocess.run(["git", "clone", "--quiet", "--depth", "1",
                        "--filter=tree:0", "--no-checkout", repo,
                        str(tmp / "repo")],
                       check=True, capture_output=True, timeout=300)
        out = subprocess.run(
            ["git", "-C", str(tmp / "repo"), "ls-tree", "-r",
             "--name-only", "HEAD"],
            check=True, capture_output=True, timeout=300).stdout
        names = [line[len("Named_Boxarts/"):-len(".png")]
                 for line in out.decode(errors="replace").splitlines()
                 if line.startswith("Named_Boxarts/")
                 and line.endswith(".png")]
        if names:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps({"ts": time.time(),
                                        "names": names}))
            return names
    except Exception:  # noqa: BLE001
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return stale


def _core(name):
    return _lr_encode(name).split(" (")[0].strip().lower()


def _regions(name):
    found = set()
    for group in re.findall(r"\(([^)]*)\)", name):
        for tok in re.split(r"[,+]", group):
            tok = tok.strip().lower()
            if tok in REGION_WORDS:
                found.add(tok)
    return found


def _penalty_tags(name):
    low = name.lower()
    return any(t in low for t in ("(proto)", "(beta)", "[hack",
                                  "(sample)", "(demo)", "(unl)"))


def _fuzzy_candidates(syskey, title):
    """Best listing matches: same core title, the title's regions
    present, fewest extras, no proto/beta/hack penalty first."""
    names = _set_listing(syskey)
    core, want = _core(title), _regions(title)
    scored, partial = [], []
    for n in names:
        if _core(n) != core:
            continue
        regs = _regions(n)
        score = 100 - 4 * len(regs - want) - len(n)
        if _penalty_tags(n) and not _penalty_tags(title):
            score -= 25
        if want and not want.issubset(regs):
            partial.append((score + 12 * len(want & regs), n))
            continue
        scored.append((score, n))
    pool = scored or partial
    pool.sort(key=lambda t: -t[0])
    return [n for _, n in pool[:6]]


def fetch_named(system, title):
    """Cover PNG bytes for a cartridge title, or None."""
    if system not in SYSTEMS:
        return None
    systems = [system] + [s for s in SYSTEMS[system][2] if s != system]
    for name in name_variants(title, system):
        for syskey in systems:
            data = _try_name(syskey, name)
            if data:
                return data
    for syskey in systems:
        for cand in _fuzzy_candidates(syskey, title):
            data = _try_name(syskey, cand)
            if data:
                return data
    return None


_SERIAL_DASH = re.compile(r"\b([A-Z]{4})-(\d{5})\b")


def _redump_names(serial):
    if CACHE_DIR is None:
        return []
    dat = Path(CACHE_DIR) / "redump-psx.dat"
    try:
        if not dat.exists() or dat.stat().st_size < 100000:
            data = _get_retry(REDUMP_DAT_URL, timeout=60)
            if not data:
                return []
            dat.parent.mkdir(parents=True, exist_ok=True)
            dat.write_bytes(data)
        text = dat.read_text(errors="replace")
    except OSError:
        return []
    for block in text.split("\ngame ("):
        if f'\tserial "{serial}"' in block:
            m = re.search(r'\tname "([^"]+)"', block)
            if m:
                return [m.group(1)]
    return []


def fetch_serial(serial):
    """Cover PNG bytes for a PSX serial, or None."""
    if not serial or not _SERIAL_DASH.match(serial):
        return None
    names = []
    for n in _redump_names(serial):
        names.append(n)
        if " - " in n:
            names.append(n.split(" - ", 1)[1])
    for name in names:
        data = _try_name("psx", name)
        if data:
            return data
    if names:
        for cand in _fuzzy_candidates("psx", names[0]):
            data = _try_name("psx", cand)
            if data:
                return data
    return None


def fetch_cover(game):
    """Cover PNG bytes for a scanner game record, or None."""
    if game.get("system") == "psx":
        return fetch_serial(game.get("serial")) \
            or fetch_named("psx", game.get("title") or "")
    return fetch_named(game.get("system"), game.get("title") or "")

