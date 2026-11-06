"""PreToolUse hook: agents may not create devices or use a pool device without a lease."""
import json
import re
import sys
import time

CREATE = [
    (r"\bsimctl\s+(create|clone)\b", "xcrun simctl create/clone"),
    (r"\bavdmanager\s+create\s+avd\b", "avdmanager create avd"),
    (r"\bflutter\s+emulators\s+--create\b", "flutter emulators --create"),
]
# Words that mean the command drives a device; list, shutdown and delete stay allowed.
USES = re.compile(r"(?<![\w-])(boot|install|uninstall|launch|spawn|terminate|openurl|push|pull|erase|privacy|"
                  r"run|test|attach|-avd|-destination|--device-id|-d|-s|shell|logcat)(?![\w-])")
HELP = ("Ask for a lease instead: devlease acquire --kind ios|android --ttl 20m --note \"<task>\"; "
        "need a device that is not in the pool: devlease request-device. See the devlease skill.")


def check(command: str, active_ids: set, pool_ids: set) -> str | None:
    """Return the reason to block the command, or None."""
    stripped = command.lstrip()
    if re.match(r"(?:\S*/)?devlease\b", stripped):
        return None
    for pattern, label in CREATE:
        if re.search(pattern, command):
            return f"`{label}` is not allowed: devices are created only through the pool. {HELP}"
    if not USES.search(command):
        return None
    tokens = set(re.findall(r"[\w.:\-]+", command))
    idle = (tokens & pool_ids) - active_ids
    if idle:
        return f"{sorted(idle)[0]} is not leased by anyone. {HELP}"
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            return 0
        command = (event.get("tool_input") or {}).get("command", "")
        if not command:
            return 0
        from . import pool
        from .db import connect, tx
        con = connect()
        with tx(con):
            ended = pool.reap(con, time.time())
        pool.shutdown_all(ended)
        active, known = set(), set()
        for d in con.execute("SELECT d.id, d.native_id, d.name, d.kind, d.port, "
                             "(SELECT 1 FROM leases l WHERE l.device_id=d.id AND l.released_at IS NULL) AS leased "
                             "FROM devices d WHERE d.present=1"):
            names = {d["native_id"], d["name"]} | ({f"emulator-{d['port']}"} if d["port"] else set())
            known |= names
            if d["leased"]:
                active |= names
        reason = check(command, active, known)
    except Exception:
        return 0  # a broken registry must not stop every command
    if reason:
        print(f"devlease: {reason}", file=sys.stderr)
        return 2
    return 0
