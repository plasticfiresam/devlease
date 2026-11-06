"""The registry rules: who may hold which device, for how long, in what order."""
import os
import re
import shutil
import time
from dataclasses import dataclass

from . import backends
from .db import cfg, tx

KINDS = ("ios", "android")


class NoMatch(Exception):
    """No device in the pool fits the request; the agent may ask for one to be created."""


class Timeout(Exception):
    pass


class BadRequest(Exception):
    pass


@dataclass
class Spec:
    kind: str
    profile: str | None = None  # "phone", "tablet" or a part of the device name
    os: str | None = None
    device: str | None = None   # exact name or native id


def matches(dev, spec: Spec) -> bool:
    if spec.device and spec.device not in (dev["name"], dev["native_id"]):
        return False
    if spec.profile:
        if spec.profile in ("phone", "tablet"):
            if dev["profile"] != spec.profile:
                return False
        elif spec.profile.lower() not in dev["name"].lower():
            return False
    if spec.os and not (dev["os"] == spec.os or dev["os"].startswith(spec.os + ".")):
        return False
    return True


def alive(pid) -> bool:
    if not pid:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def sync(con, kind: str, force=False, backend=None) -> None:
    """Mirror the devices the system has into the registry (at most once a minute)."""
    key = f"synced_{kind}"
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row and not force and time.time() - float(row["value"]) < 60:
        return
    found = (backend or backends.get(kind)).list_devices()
    with tx(con):
        seen = set()
        used_ports = {r["port"] for r in con.execute("SELECT port FROM devices WHERE kind=? AND port IS NOT NULL", (kind,))}
        for d in found:
            seen.add(d["native_id"])
            old = con.execute("SELECT id, port FROM devices WHERE kind=? AND native_id=?", (kind, d["native_id"])).fetchone()
            if old:
                con.execute("UPDATE devices SET name=?, profile=?, os=?, present=1 WHERE id=?",
                            (d["name"], d["profile"], d["os"], old["id"]))
                continue
            port = None
            if kind == "android":
                port = next(p for p in range(5554, 5700, 2) if p not in used_ports)
                used_ports.add(port)
            con.execute("INSERT INTO devices(kind, native_id, name, profile, os, port) VALUES(?,?,?,?,?,?)",
                        (kind, d["native_id"], d["name"], d["profile"], d["os"], port))
        for row in con.execute("SELECT id, native_id FROM devices WHERE kind=?", (kind,)).fetchall():
            if row["native_id"] not in seen:
                con.execute("UPDATE devices SET present=0 WHERE id=?", (row["id"],))
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, str(time.time())))


def reap(con, now: float) -> list:
    """End leases that ran out or whose holder process died. Call inside a transaction."""
    ended = []
    for lease in con.execute("SELECT l.*, d.kind, d.native_id, d.port FROM leases l JOIN devices d ON d.id=l.device_id "
                             "WHERE l.released_at IS NULL").fetchall():
        reason = "expired" if lease["expires_at"] <= now else None
        if not reason and not alive(lease["pid"]):
            reason = "holder-dead"
        if reason:
            con.execute("UPDATE leases SET released_at=?, end_reason=? WHERE id=?", (now, reason, lease["id"]))
            ended.append(lease)
    for w in con.execute("SELECT id, pid FROM waiters").fetchall():
        if not alive(w["pid"]):
            con.execute("DELETE FROM waiters WHERE id=?", (w["id"],))
    return ended


def shutdown_all(ended) -> None:
    for lease in ended:
        try:
            backends.get(lease["kind"]).shutdown(lease)
        except Exception:
            pass


def _spec_of(w) -> Spec:
    return Spec(w["kind"], w["profile"], w["os"], w["device"])


def attempt(con, waiter_id, spec: Spec, ttl: float, holder: str, pid: int | None, note: str):
    """One try to take a device. Returns (lease row | None, waiter id, ended leases)."""
    now = time.time()
    with tx(con):
        ended = reap(con, now)
        present = con.execute("SELECT * FROM devices WHERE kind=? AND present=1 ORDER BY name", (spec.kind,)).fetchall()
        if not any(matches(d, spec) for d in present):
            raise NoMatch(f"no {spec.kind} device in the pool matches {spec}")
        if waiter_id is None:
            waiter_id = con.execute(
                "INSERT INTO waiters(kind, profile, os, device, holder, pid, note, since) VALUES(?,?,?,?,?,?,?,?)",
                (spec.kind, spec.profile, spec.os, spec.device, holder, os.getpid(), note, now)).lastrowid
        busy = {r["device_id"] for r in con.execute("SELECT device_id FROM leases WHERE released_at IS NULL")}
        if len(busy & {d["id"] for d in present}) < cfg(con, f"limit_{spec.kind}"):
            earlier = [_spec_of(w) for w in con.execute(
                "SELECT * FROM waiters WHERE kind=? AND id<? ORDER BY id", (spec.kind, waiter_id))]
            for d in present:
                if d["id"] in busy or not matches(d, spec):
                    continue
                if any(matches(d, e) for e in earlier):
                    continue  # someone who came first can use this device
                lease_id = con.execute(
                    "INSERT INTO leases(device_id, holder, pid, note, acquired_at, expires_at) VALUES(?,?,?,?,?,?)",
                    (d["id"], holder, pid, note, now, now + ttl)).lastrowid
                con.execute("DELETE FROM waiters WHERE id=?", (waiter_id,))
                return lease_view(con, lease_id), waiter_id, ended
        return None, waiter_id, ended


def lease_view(con, lease_id: int) -> dict:
    row = con.execute("SELECT l.*, d.kind, d.native_id, d.name, d.profile, d.os, d.port FROM leases l "
                      "JOIN devices d ON d.id=l.device_id WHERE l.id=?", (lease_id,)).fetchone()
    if not row:
        raise BadRequest(f"no lease {lease_id}")
    backend = backends.get(row["kind"])
    return {"lease": row["id"], "kind": row["kind"], "device": row["name"], "id": row["native_id"],
            "serial": backend.serial(row), "profile": row["profile"], "os": row["os"],
            "expires_at": row["expires_at"], "holder": row["holder"], "released_at": row["released_at"],
            "end_reason": row["end_reason"]}


def waiting_message(con, spec: Spec) -> str:
    rows = con.execute(
        "SELECT l.holder, l.note, l.expires_at, d.name FROM leases l JOIN devices d ON d.id=l.device_id "
        "WHERE l.released_at IS NULL AND d.kind=? ORDER BY l.expires_at", (spec.kind,)).fetchall()
    lines = [f"devlease: no free {spec.kind} device for {spec}, waiting"]
    for r in rows:
        left = max(0, int(r["expires_at"] - time.time()))
        lines.append(f"devlease:   {r['name']}: held by {r['holder']} for another {left}s: {r['note']}")
    return "\n".join(lines)


def check_ttl(con, ttl: float) -> None:
    limit = cfg(con, "max_ttl")
    if ttl <= 0 or ttl > limit:
        raise BadRequest(f"--ttl must be between 1s and {limit}s (the machine-wide cap), got {int(ttl)}s")


def renew(con, lease_id: int, ttl: float) -> dict:
    check_ttl(con, ttl)
    now = time.time()
    with tx(con):
        ended = reap(con, now)
        row = con.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
        if not row or row["released_at"] is not None:
            raise BadRequest(f"lease {lease_id} is not active (expired or released)")
        con.execute("UPDATE leases SET expires_at=? WHERE id=?", (now + ttl, lease_id))
    shutdown_all(ended)
    return lease_view(con, lease_id)


def release(con, lease_id: int, shutdown=True) -> dict:
    now = time.time()
    with tx(con):
        row = con.execute("SELECT l.*, d.kind, d.native_id, d.port FROM leases l JOIN devices d ON d.id=l.device_id "
                          "WHERE l.id=?", (lease_id,)).fetchone()
        if not row:
            raise BadRequest(f"no lease {lease_id}")
        if row["released_at"] is None:
            con.execute("UPDATE leases SET released_at=?, end_reason='released' WHERE id=?", (now, lease_id))
    if shutdown and row["released_at"] is None:
        shutdown_all([row])
    return lease_view(con, lease_id)


def state(con) -> dict:
    """Everything the status command and the web page show."""
    now = time.time()
    with tx(con):
        ended = reap(con, now)
    shutdown_all(ended)
    devices = []
    for d in con.execute("SELECT * FROM devices WHERE present=1 ORDER BY kind, name").fetchall():
        lease = con.execute("SELECT * FROM leases WHERE device_id=? AND released_at IS NULL", (d["id"],)).fetchone()
        devices.append({**dict(d), "serial": backends.get(d["kind"]).serial(d),
                        "lease": dict(lease) if lease else None})
    return {
        "now": now,
        "devices": devices,
        "waiters": [dict(w) for w in con.execute("SELECT * FROM waiters ORDER BY id")],
        "requests": [dict(r) for r in con.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 30")],
        "history": [dict(r) for r in con.execute(
            "SELECT l.id, l.holder, l.note, l.acquired_at, l.released_at, l.end_reason, d.name FROM leases l "
            "JOIN devices d ON d.id=l.device_id WHERE l.released_at IS NOT NULL ORDER BY l.id DESC LIMIT 20")],
        "limits": {k: cfg(con, k) for k in ("max_ttl", "limit_ios", "limit_android")},
    }


# --- creation requests ------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"[\s_\-]+", " ", text.lower()).strip()


def request_device(con, kind: str, name: str, profile: str, os_version: str, holder: str, reason: str,
                   image: str = "") -> dict:
    """An agent asks for a device that is not in the pool. Returns the request, or the existing device."""
    sync(con, kind)
    for d in con.execute("SELECT * FROM devices WHERE kind=? AND present=1", (kind,)):
        if d["name"] == name:
            return {"status": "exists", "device": d["name"], "id": d["native_id"]}
        same_model = (_norm(d["name"]) == _norm(profile) if kind == "ios"
                      else _norm(profile) in _norm(d["name"]))
        if same_model and (not os_version or matches(d, Spec(kind, None, os_version))):
            return {"status": "exists", "device": d["name"], "id": d["native_id"]}
    with tx(con):
        req = con.execute("SELECT id FROM requests WHERE kind=? AND name=? AND status IN ('pending','approved')",
                          (kind, name)).fetchone()
        if req:
            return {"status": "pending", "request": req["id"]}
        count = con.execute("SELECT COUNT(*) FROM devices WHERE kind=? AND present=1", (kind,)).fetchone()[0]
        free_gb = shutil.disk_usage(os.path.expanduser("~")).free / 1e9
        auto = count < cfg(con, f"max_devices_{kind}") and free_gb >= cfg(con, "min_free_gb")
        req_id = con.execute(
            "INSERT INTO requests(kind, name, profile, os, image, holder, reason, status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (kind, name, profile, os_version, image, holder, reason, "pending", time.time())).lastrowid
    if auto:
        return decide(con, req_id, approve=True)
    return {"status": "pending", "request": req_id,
            "note": "the pool is at its size limit or the disk is nearly full: a human must approve it"}


def decide(con, req_id: int, approve: bool) -> dict:
    with tx(con):
        req = con.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
        if not req:
            raise BadRequest(f"no request {req_id}")
        if req["status"] != "pending":
            return {"status": req["status"], "request": req_id, "result": req["result"]}
        if not approve:
            con.execute("UPDATE requests SET status='rejected', decided_at=? WHERE id=?", (time.time(), req_id))
            return {"status": "rejected", "request": req_id}
        con.execute("UPDATE requests SET status='approved', decided_at=? WHERE id=?", (time.time(), req_id))
    try:
        backends.get(req["kind"]).create(req["name"], req["profile"], req["os"], req["image"])
    except Exception as e:
        con.execute("UPDATE requests SET status='failed', result=? WHERE id=?", (str(e), req_id))
        return {"status": "failed", "request": req_id, "error": str(e)}
    sync(con, req["kind"], force=True)
    con.execute("UPDATE requests SET status='created', result='ok' WHERE id=?", (req_id,))
    dev = con.execute("SELECT name, native_id FROM devices WHERE kind=? AND name=?", (req["kind"], req["name"])).fetchone()
    return {"status": "created", "request": req_id, "device": dev["name"] if dev else req["name"],
            "id": dev["native_id"] if dev else req["name"]}
