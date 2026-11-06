"""Run the web page as a macOS LaunchAgent."""
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .db import state_dir

LABEL = "dev.devlease.server"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(port: int) -> dict:
    exe = shutil.which("devlease") or str(Path(sys.argv[0]).resolve())
    log = str(state_dir() / "server.log")
    env = {k: os.environ[k] for k in ("DEVLEASE_DIR", "ANDROID_HOME", "ANDROID_SDK_ROOT") if k in os.environ}
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    return {"Label": LABEL, "ProgramArguments": [exe, "serve", "--port", str(port)],
            "RunAtLoad": True, "KeepAlive": True, "StandardOutPath": log, "StandardErrorPath": log,
            "EnvironmentVariables": env}


def install(port: int) -> str:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(build_plist(port)))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    out = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "launchctl bootstrap failed")
    return str(path)


def uninstall() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    plist_path().unlink(missing_ok=True)
