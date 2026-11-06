"""Host memory, per-device memory, disk and the cache whitelist the web page can clean."""
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

HOME = Path.home()


def _out(cmd, timeout=20) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def memory() -> dict:
    total = int(_out(["sysctl", "-n", "hw.memsize"]).strip() or 0)
    vm = _out(["vm_stat"])
    page = int(re.search(r"page size of (\d+)", vm)[1]) if "page size of" in vm else 16384
    pages = {m[1].strip(): int(m[2]) for m in re.finditer(r"^(.+?):\s+(\d+)\.", vm, re.M)}
    available = sum(pages.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative")) * page
    disk = shutil.disk_usage("/System/Volumes/Data" if Path("/System/Volumes/Data").exists() else "/")
    return {"total": total, "used": max(0, total - available) if total else 0,
            "disk_total": disk.total, "disk_free": disk.free}


def device_memory() -> dict:
    """Resident memory (bytes) of simulator and emulator processes, keyed by UDID or AVD name."""
    usage: dict[str, int] = {}
    for line in _out(["ps", "-axo", "rss=,command="]).splitlines():
        rss, _, command = line.strip().partition(" ")
        if not rss.isdigit():
            continue
        key = None
        m = re.search(r"CoreSimulator/Devices/([0-9A-F-]{36})/", command)
        if m:
            key = m[1]
        else:
            m = re.search(r"(?:qemu-system\S*|emulator\S*)\s.*?(?:-avd|@)\s*(\S+)", command)
            if m and ("qemu" in command or "/emulator" in command):
                key = m[1]
        if key:
            usage[key] = usage.get(key, 0) + int(rss) * 1024
    return usage


def top_processes(limit=8) -> list:
    rows = []
    for line in _out(["ps", "-axo", "rss=,comm="]).splitlines():
        rss, _, comm = line.strip().partition(" ")
        if rss.isdigit():
            rows.append({"name": os.path.basename(comm.strip()), "rss": int(rss) * 1024})
    merged: dict[str, int] = {}
    for r in rows:
        merged[r["name"]] = merged.get(r["name"], 0) + r["rss"]
    return [{"name": k, "rss": v} for k, v in sorted(merged.items(), key=lambda kv: -kv[1])[:limit]]


# --- caches -----------------------------------------------------------------
# Only these are ever deleted, and only their contents: the page cannot name a path.

CACHES = {
    "xcode-derived-data": {
        "label": "Xcode DerivedData", "paths": [HOME / "Library/Developer/Xcode/DerivedData"],
        "busy": ["xcodebuild", "Xcode"], "note": "Build products; rebuilt on the next build."},
    "xcode-device-support": {
        "label": "Xcode iOS DeviceSupport", "paths": [HOME / "Library/Developer/Xcode/iOS DeviceSupport"],
        "busy": ["Xcode"], "note": "Symbols per connected iOS version; copied again when a device connects."},
    "gradle-caches": {
        "label": "Gradle caches", "paths": [HOME / ".gradle/caches"],
        "busy": ["GradleDaemon", "gradlew", "Android Studio"],
        "note": "Downloaded dependencies; the next sync downloads them again."},
    "android-build-cache": {
        "label": "Android build cache", "paths": [HOME / ".android/cache"],
        "busy": ["Android Studio"], "note": "SDK manager and build caches."},
    "uv": {
        "label": "uv cache", "paths": [HOME / ".cache/uv"], "busy": ["uv"],
        "note": "Python wheels and builds; downloaded again on demand."},
    "npm": {
        "label": "npm cache", "paths": [HOME / ".npm/_cacache"], "busy": ["npm"],
        "note": "Package tarballs."},
    "simulators-unavailable": {
        "label": "Unavailable simulators", "paths": [], "busy": [],
        "command": ["xcrun", "simctl", "delete", "unavailable"],
        "note": "Simulators whose runtime was removed. Not a path: size is unknown."},
}

_sizes: dict[str, tuple[float, int]] = {}
_lock = threading.Lock()


def _du(path: Path) -> int:
    if not path.exists():
        return 0
    out = _out(["du", "-sk", str(path)], timeout=300).split()
    return int(out[0]) * 1024 if out and out[0].isdigit() else 0


def cache_size(key: str, max_age=300) -> int | None:
    with _lock:
        cached = _sizes.get(key)
    if cached and time.time() - cached[0] < max_age:
        return cached[1]
    spec = CACHES[key]
    if not spec["paths"]:
        return None
    size = sum(_du(p) for p in spec["paths"])
    with _lock:
        _sizes[key] = (time.time(), size)
    return size


def running(names) -> list[str]:
    found = []
    ps = _out(["ps", "-axo", "command="])
    for name in names:
        if re.search(rf"(^|/|\s){re.escape(name)}(\s|$|\.app)", ps, re.M):
            found.append(name)
    return found


def clean_cache(key: str, force=False) -> dict:
    """Delete the contents of one whitelisted cache. Refuses while its tool runs unless forced."""
    if key not in CACHES:
        raise KeyError(key)
    spec = CACHES[key]
    busy = running(spec["busy"])
    if busy and not force:
        return {"status": "busy", "processes": busy}
    if spec.get("command"):
        out = subprocess.run(spec["command"], capture_output=True, text=True)
        return {"status": "ok" if out.returncode == 0 else "failed", "freed": 0, "error": out.stderr.strip()}
    before = sum(_du(p) for p in spec["paths"])
    errors = 0
    for root in spec["paths"]:
        if not root.is_dir() or root.is_symlink():
            continue
        for child in root.iterdir():
            try:
                shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
            except OSError:
                errors += 1
    after = sum(_du(p) for p in spec["paths"])
    with _lock:
        _sizes[key] = (time.time(), after)
    return {"status": "ok", "freed": max(0, before - after), "errors": errors}
