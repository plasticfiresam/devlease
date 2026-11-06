"""Talks to the real simulators and emulators (or to a file in tests)."""
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path


class BackendError(RuntimeError):
    pass


def _run(cmd, timeout=120, input=None):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)
    except FileNotFoundError as e:
        raise BackendError(f"{cmd[0]} not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise BackendError(f"{' '.join(cmd)} timed out after {timeout}s") from e
    if out.returncode != 0:
        raise BackendError(f"{' '.join(cmd)} failed: {(out.stderr or out.stdout).strip()}")
    return out.stdout


def sdk_root() -> Path:
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            return Path(os.environ[var])
    return Path.home() / "Library" / "Android" / "sdk"


def sdk_tool(*parts: str) -> str:
    path = sdk_root().joinpath(*parts)
    if path.exists():
        return str(path)
    found = shutil.which(parts[-1])
    if not found:
        raise BackendError(f"{parts[-1]} not found under {sdk_root()} or in PATH")
    return found


def ios_profile(name: str) -> str:
    return "tablet" if name.startswith("iPad") else "phone"


class IOS:
    kind = "ios"

    def list_devices(self):
        data = json.loads(_run(["xcrun", "simctl", "list", "devices", "available", "-j"]))
        found = []
        for runtime, devices in data["devices"].items():
            m = re.search(r"\.iOS-(\d+)-(\d+)", runtime)
            if not m:
                continue
            for d in devices:
                found.append({"native_id": d["udid"], "name": d["name"],
                              "profile": ios_profile(d["name"]), "os": f"{m[1]}.{m[2]}"})
        return found

    def boot(self, device):
        subprocess.run(["xcrun", "simctl", "boot", device["native_id"]], capture_output=True)
        _run(["xcrun", "simctl", "bootstatus", device["native_id"], "-b"], timeout=300)

    def shutdown(self, device):
        subprocess.run(["xcrun", "simctl", "shutdown", device["native_id"]], capture_output=True)

    def serial(self, device):
        return device["native_id"]

    def create(self, name, profile, os_version, image=""):
        types = json.loads(_run(["xcrun", "simctl", "list", "devicetypes", "-j"]))["devicetypes"]
        matches = [t for t in types if t["name"] == profile]
        if not matches:
            raise BackendError(f"no simulator device type named '{profile}'")
        runtime = "com.apple.CoreSimulator.SimRuntime.iOS-" + os_version.replace(".", "-")
        udid = _run(["xcrun", "simctl", "create", name, matches[0]["identifier"], runtime]).strip()
        return udid

    def delete(self, device):
        _run(["xcrun", "simctl", "delete", device["native_id"]])


class Android:
    kind = "android"

    def _avd_dir(self) -> Path:
        return Path(os.environ.get("ANDROID_AVD_HOME") or Path.home() / ".android" / "avd")

    def list_devices(self):
        found = []
        for ini in sorted(self._avd_dir().glob("*.ini")):
            name = ini.stem
            config = self._avd_dir() / f"{name}.avd" / "config.ini"
            text = config.read_text() if config.is_file() else ""
            m = re.search(r"image\.sysdir\.1\s*=\s*system-images/android-([\w.]+)/", text)
            api = m[1] if m else ""
            tablet = re.search(r"tablet|pad|pixel_c", name + text.partition("hw.device.name")[2][:40], re.I)
            found.append({"native_id": name, "name": name,
                          "profile": "tablet" if tablet else "phone", "os": api})
        return found

    def serial(self, device):
        return f"emulator-{device['port']}"

    def boot(self, device):
        emulator = sdk_tool("emulator", "emulator")
        adb = sdk_tool("platform-tools", "adb")
        serial = self.serial(device)
        log = open(os.devnull, "wb")
        subprocess.Popen([emulator, "-avd", device["native_id"], "-port", str(device["port"]),
                          "-no-snapshot-save", "-no-boot-anim"], stdout=log, stderr=log,
                         start_new_session=True)
        deadline = time.time() + 300
        while time.time() < deadline:
            out = subprocess.run([adb, "-s", serial, "shell", "getprop", "sys.boot_completed"],
                                 capture_output=True, text=True)
            if out.stdout.strip() == "1":
                return
            time.sleep(3)
        raise BackendError(f"{serial} did not finish booting in 300s")

    def shutdown(self, device):
        try:
            subprocess.run([sdk_tool("platform-tools", "adb"), "-s", self.serial(device), "emu", "kill"],
                           capture_output=True, timeout=30)
        except (BackendError, subprocess.TimeoutExpired):
            pass

    def create(self, name, profile, api, image=""):
        arch = "arm64-v8a" if platform.machine() == "arm64" else "x86_64"
        package = image or f"system-images;android-{api};google_apis;{arch}"
        if not (sdk_root() / package.replace(";", "/")).is_dir():
            raise BackendError(f"system image '{package}' is not installed; "
                               f"install it with: sdkmanager \"{package}\"")
        _run([sdk_tool("cmdline-tools", "latest", "bin", "avdmanager"), "create", "avd",
              "-n", name, "-k", package, "-d", profile], input="no\n")
        return name

    def delete(self, device):
        _run([sdk_tool("cmdline-tools", "latest", "bin", "avdmanager"), "delete", "avd",
              "-n", device["native_id"]])


class Fake:
    """Devices listed in $DEVLEASE_FAKE_FILE; boots and creations are appended to its log."""

    def __init__(self, kind):
        self.kind = kind
        self.path = Path(os.environ["DEVLEASE_FAKE_FILE"])

    def _data(self):
        return json.loads(self.path.read_text())

    def _log(self, line):
        with open(self.path.with_suffix(".log"), "a") as f:
            f.write(line + "\n")

    def list_devices(self):
        return [d for d in self._data()["devices"] if d["kind"] == self.kind]

    def boot(self, device):
        self._log(f"boot {device['native_id']}")

    def shutdown(self, device):
        self._log(f"shutdown {device['native_id']}")

    def serial(self, device):
        return f"emulator-{device['port']}" if self.kind == "android" else device["native_id"]

    def create(self, name, profile, os_version, image=""):
        data = self._data()
        if profile == "unavailable":
            raise BackendError("system image is not installed")
        data["devices"].append({"kind": self.kind, "native_id": name, "name": name,
                                "profile": "tablet" if "pad" in name.lower() else "phone",
                                "os": os_version})
        self.path.write_text(json.dumps(data))
        self._log(f"create {name}")
        return name

    def delete(self, device):
        data = self._data()
        data["devices"] = [d for d in data["devices"] if d["native_id"] != device["native_id"]]
        self.path.write_text(json.dumps(data))


def get(kind: str):
    if os.environ.get("DEVLEASE_FAKE_FILE"):
        return Fake(kind)
    return {"ios": IOS, "android": Android}[kind]()
