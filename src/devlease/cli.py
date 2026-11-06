"""Command line: acquire, run, release, renew, status, request-device, config, serve, service, hook."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

from . import backends, pool
from .db import DEFAULTS, connect, tx

EXIT_TIMEOUT = 75   # same code as `heavy`: the wait ran out
EXIT_NO_MATCH = 3


def parse_duration(text: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600}
    text = text.strip()
    if text and text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text)


def holder_name() -> str:
    return os.environ.get("DEVLEASE_HOLDER") or os.getcwd()


def emit(data) -> None:
    print(json.dumps(data, ensure_ascii=False))


def add_spec(p, need_ttl=True):
    p.add_argument("--kind", required=True, choices=pool.KINDS)
    p.add_argument("--profile", help="phone, tablet, or a part of the device name")
    p.add_argument("--os", help="iOS version such as 27.0, or Android API level such as 35")
    p.add_argument("--device", help="exact device name or id")
    p.add_argument("--ttl", required=need_ttl, help="how long you need it, e.g. 20m (at most the machine cap)")
    p.add_argument("--wait", default="20m", help="how long to wait for a free device (default 20m)")
    p.add_argument("--note", default="", help="what the device is for, shown to the others who wait")


def acquire(con, args, pid):
    spec = pool.Spec(args.kind, args.profile, args.os, args.device)
    ttl = parse_duration(args.ttl)
    pool.check_ttl(con, ttl)
    pool.sync(con, args.kind)
    deadline = time.time() + parse_duration(args.wait)
    waiter, told = None, False
    try:
        while True:
            lease, waiter, ended = pool.attempt(con, waiter, spec, ttl, holder_name(), pid, args.note)
            pool.shutdown_all(ended)
            if lease:
                waiter = None
                return lease
            if time.time() >= deadline:
                raise pool.Timeout(f"no {args.kind} device became free in {args.wait}")
            if not told:
                print(pool.waiting_message(con, spec), file=sys.stderr, flush=True)
                told = True
            time.sleep(1)
    finally:
        if waiter is not None:
            with tx(con):
                con.execute("DELETE FROM waiters WHERE id=?", (waiter,))


def boot(con, lease):
    row = con.execute("SELECT d.* FROM leases l JOIN devices d ON d.id=l.device_id WHERE l.id=?",
                      (lease["lease"],)).fetchone()
    backends.get(lease["kind"]).boot(row)


def cmd_acquire(args) -> int:
    con = connect()
    lease = acquire(con, args, args.pid)
    if args.boot:
        try:
            boot(con, lease)
        except backends.BackendError as e:
            pool.release(con, lease["lease"])
            print(f"devlease: could not boot {lease['device']}: {e}", file=sys.stderr)
            return 1
    emit(lease)
    return 0


def cmd_run(args) -> int:
    """Hold a device while one command runs; release it however the command ends."""
    if not args.command:
        print("devlease: run needs a command after --", file=sys.stderr)
        return 2
    con = connect()
    lease = acquire(con, args, os.getpid())
    env = {**os.environ, "DEVLEASE_DEVICE": lease["id"], "DEVLEASE_SERIAL": lease["serial"],
           "DEVLEASE_LEASE": str(lease["lease"])}
    child = None

    def stop(signum, _frame):
        if child and child.poll() is None:
            child.send_signal(signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    code = 1
    try:
        if args.boot:
            boot(con, lease)
        child = subprocess.Popen(args.command, env=env)
        try:
            code = child.wait(timeout=max(1, lease["expires_at"] - time.time()))
        except subprocess.TimeoutExpired:
            print(f"devlease: lease {lease['lease']} ran out, stopping the command", file=sys.stderr)
            child.terminate()
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.kill()
            code = 124
    except FileNotFoundError as e:
        print(f"devlease: {e}", file=sys.stderr)
        code = 127
    except backends.BackendError as e:
        print(f"devlease: could not boot {lease['device']}: {e}", file=sys.stderr)
    finally:
        pool.release(con, lease["lease"])
    return code


def cmd_release(args) -> int:
    con = connect()
    emit(pool.release(con, args.lease, shutdown=not args.keep_booted))
    return 0


def cmd_renew(args) -> int:
    emit(pool.renew(connect(), args.lease, parse_duration(args.ttl)))
    return 0


def cmd_status(args) -> int:
    con = connect()
    for kind in pool.KINDS:
        try:
            pool.sync(con, kind)
        except backends.BackendError:
            pass
    state = pool.state(con)
    if args.json:
        emit(state)
        return 0
    for d in state["devices"]:
        l = d["lease"]
        who = f"held by {l['holder']} for another {int(l['expires_at'] - state['now'])}s: {l['note']}" if l else "free"
        print(f"{d['kind']:8}{d['name']:34}{d['os']:6}{who}")
    for w in state["waiters"]:
        print(f"waiting: {w['kind']} {w['profile'] or ''} {w['os'] or ''} - {w['holder']}: {w['note']}")
    return 0


def cmd_request(args) -> int:
    con = connect()
    result = pool.request_device(con, args.kind, args.name, args.profile, args.os, holder_name(),
                                 args.reason, args.image or "")
    emit(result)
    return {"exists": 0, "created": 0, "pending": 4, "failed": 1}.get(result["status"], 1)


def cmd_requests(args) -> int:
    con = connect()
    if args.action == "list":
        emit(pool.state(con)["requests"])
        return 0
    emit(pool.decide(con, args.id, args.action == "approve"))
    return 0


def cmd_config(args) -> int:
    con = connect()
    if args.key is None:
        emit({k: int(con.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()["value"])
              if con.execute("SELECT 1 FROM settings WHERE key=?", (k,)).fetchone() else v
              for k, v in DEFAULTS.items()})
        return 0
    if args.key not in DEFAULTS:
        print(f"devlease: unknown key; known: {', '.join(DEFAULTS)}", file=sys.stderr)
        return 2
    if args.value is not None:
        with tx(con):
            con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (args.key, str(int(args.value))))
    emit({args.key: pool.cfg(con, args.key)})
    return 0


def cmd_serve(args) -> int:
    from .server import serve
    serve(args.port or pool.cfg(connect(), "port"))
    return 0


def cmd_service(args) -> int:
    from . import service
    port = args.port or pool.cfg(connect(), "port")
    if args.action == "install":
        print(service.install(port))
        print(f"devlease: http://127.0.0.1:{port}")
    else:
        service.uninstall()
    return 0


def cmd_hook(args) -> int:
    from .hook import main as hook_main
    return hook_main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="devlease", description="Lease iOS simulators and Android emulators.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("acquire", help="take a device; waits in a queue when all are busy")
    add_spec(a)
    a.add_argument("--boot", action="store_true", help="boot the device and wait until it is ready")
    a.add_argument("--pid", type=int, help="end the lease when this process dies")
    a.set_defaults(fn=cmd_acquire)

    r = sub.add_parser("run", help="hold a device for one command: devlease run --kind ios --ttl 20m -- cmd")
    add_spec(r)
    r.add_argument("--boot", action="store_true")
    r.add_argument("command", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)

    rel = sub.add_parser("release", help="give a device back")
    rel.add_argument("lease", type=int)
    rel.add_argument("--keep-booted", action="store_true")
    rel.set_defaults(fn=cmd_release)

    ren = sub.add_parser("renew", help="extend a lease (each time at most the machine cap)")
    ren.add_argument("lease", type=int)
    ren.add_argument("--ttl", required=True)
    ren.set_defaults(fn=cmd_renew)

    st = sub.add_parser("status", aliases=["list"], help="the registry and who holds what")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_status)

    q = sub.add_parser("request-device", help="ask for a device that is not in the pool")
    q.add_argument("--kind", required=True, choices=pool.KINDS)
    q.add_argument("--name", required=True, help="name of the new device")
    q.add_argument("--profile", required=True,
                   help="iOS: simulator device type such as 'iPhone 17 Pro'; Android: device definition such as pixel_8")
    q.add_argument("--os", required=True, help="iOS version such as 27.0, or Android API level such as 35")
    q.add_argument("--image", help="Android system image package, if not google_apis for this machine")
    q.add_argument("--reason", required=True)
    q.set_defaults(fn=cmd_request)

    rq = sub.add_parser("requests", help="list, approve or reject creation requests")
    rq.add_argument("action", choices=["list", "approve", "reject"])
    rq.add_argument("id", type=int, nargs="?")
    rq.set_defaults(fn=cmd_requests)

    c = sub.add_parser("config", help="show or change machine-wide limits")
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    c.set_defaults(fn=cmd_config)

    s = sub.add_parser("serve", help="run the web page")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)

    sv = sub.add_parser("service", help="install or remove the web page as a LaunchAgent")
    sv.add_argument("action", choices=["install", "uninstall"])
    sv.add_argument("--port", type=int)
    sv.set_defaults(fn=cmd_service)

    h = sub.add_parser("hook", help="agent hook entry point")
    h.add_argument("event", choices=["pretool"])
    h.set_defaults(fn=cmd_hook)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "run" and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if args.cmd == "requests" and args.action != "list" and args.id is None:
        print("devlease: requests approve|reject needs an id", file=sys.stderr)
        return 2
    try:
        return args.fn(args)
    except pool.BadRequest as e:
        print(f"devlease: {e}", file=sys.stderr)
        return 2
    except pool.NoMatch as e:
        print(f"devlease: {e}. If you need it, ask: devlease request-device --kind {args.kind} ...", file=sys.stderr)
        return EXIT_NO_MATCH
    except pool.Timeout as e:
        print(f"devlease: {e}", file=sys.stderr)
        return EXIT_TIMEOUT
    except backends.BackendError as e:
        print(f"devlease: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
