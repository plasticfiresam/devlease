# devlease

A machine-wide lease registry for iOS simulators and Android emulators.

Ten coding agents on one machine each boot a simulator, and when none is free they create another
one: the disk fills with devices and the memory with emulators nobody uses. `devlease` keeps a
fixed pool, hands each device to one agent at a time for a period the agent states, makes the
others wait in order, and gives a human one page to see and clean it all.

```sh
devlease run --kind ios --profile phone --ttl 20m --boot -- sh -c 'flutter test -d "$DEVLEASE_SERIAL"'
devlease status
devlease serve        # http://127.0.0.1:7788
```

## Install

```sh
uv tool install git+https://github.com/plasticfiresam/devlease
# or: pipx install git+https://github.com/plasticfiresam/devlease
```

Python 3.11 or newer, no other dependencies. macOS with Xcode (`xcrun simctl`) and the Android SDK
(`ANDROID_HOME`, default `~/Library/Android/sdk`) for the real devices.

## How it behaves

- **A fixed pool.** The devices the system already has are the pool (`devlease status` rescans at
  most once a minute). Agents do not create devices; they ask (see below).
- **A lease has a period.** `--ttl` is required and capped machine-wide (default 30 minutes; a longer
  value is refused). `renew` extends it, each time by at most the cap. A lease also ends when the
  process given with `--pid` dies, and `run` stops its command when the lease runs out.
- **A fair queue.** `acquire` waits when nothing matching is free, says once who holds what and for
  how long, and serves waiters in order: a later agent may take a device only if no earlier waiter
  could use it. `--wait` bounds the wait (exit 75 when it runs out, like `heavy`).
- **Limits on what runs at once.** `limit_ios` and `limit_android` (default 2 each) cap the leased
  devices, so memory stays bounded however large the pool is.
- **Android emulators get their own ports.** The lease reports the serial (`emulator-5554`), and
  one AVD is never started twice.
- **Release shuts the device down** (`simctl shutdown`, `adb emu kill`), also for expired leases.
- **Writes are serialised** by `BEGIN IMMEDIATE` in a SQLite file (WAL) under `~/.cache/devlease`
  (`DEVLEASE_DIR` moves it), so every checkout and agent shares one registry.

Exit codes: `0` ok, `2` bad arguments, `3` nothing in the pool matches, `4` a creation request
waits for a human, `75` the wait ran out, `124` (`run`) the lease ran out.

## A device that is not in the pool

```sh
devlease request-device --kind android --name Pixel_9_API_36 --profile pixel_9 --os 36 --reason "..."
```

If a matching device exists, its id is returned. Otherwise the device is created at once while the
pool is below `max_devices_<kind>` (default 8 iOS, 4 Android) and the disk has `min_free_gb` free
(default 20); past that the request waits for a human in the web page or
`devlease requests approve|reject <id>`. A missing Android system image fails the request with the
`sdkmanager` command to run: images are never downloaded for an agent.

## Enforcement

Rules in a prompt are easy to skip, so add a hook. For Claude Code, in `~/.claude/settings.json`:

```json
{ "hooks": { "PreToolUse": [ { "matcher": "Bash",
  "hooks": [ { "type": "command", "command": "devlease hook pretool" } ] } ] } }
```

It blocks `simctl create|clone`, `avdmanager create avd` and `flutter emulators --create`, and any
command that drives a pool device (`boot`, `install`, `launch`, `flutter run -d`, `adb -s`,
`emulator -avd` and the like) while nobody holds a lease on it. Commands starting with `devlease`
pass. If the registry itself is broken, the hook lets the command through. It cannot tell which
agent holds a lease, only that one exists, and it reads the command text, so an agent that hides
the id in a script slips past it.

## The web page

`devlease serve [--port 7788]` serves one page on `127.0.0.1`: every device with its state, holder,
time left and resident memory; the queue; creation requests with Approve and Reject; host memory
and disk; and the caches (Xcode DerivedData and DeviceSupport, Gradle, Android build cache, uv, npm,
unavailable simulators) with their sizes and a Clean button. A cache that its tool is using (for
example Gradle while Android Studio runs) asks for confirmation first.

The page only offers the fixed list of caches; it takes no path or command from the browser, and
POSTs need a custom header and a local `Host`.

To keep it running as a macOS LaunchAgent: `devlease service install` (and `uninstall`).

## Settings

`devlease config` shows the numbers; `devlease config max_ttl 1800` changes one. Keys: `max_ttl`
(seconds), `limit_ios`, `limit_android`, `max_devices_ios`, `max_devices_android`, `min_free_gb`,
`port`.

## For agents

The skill in [`skills/devlease`](skills/devlease/SKILL.md) tells an agent how to use all this. Link
that folder into the agent's skills directory.

## Develop

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The tests run the real CLI, hook and web service against a fake device list.
