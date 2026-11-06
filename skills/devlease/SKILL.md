---
name: devlease
description: Lease iOS simulators and Android emulators through the machine-wide `devlease` registry instead of booting or creating them yourself. Use before running an app, tests, screenshots or `flutter run` on a simulator or emulator, when a simulator/emulator is busy, or when you need a device that does not exist yet.
---

# devlease

Simulators and emulators are shared by every agent on this machine, and each one costs gigabytes
of disk and memory. `devlease` hands them out one holder at a time, in order, for a stated period.
A hook blocks the commands that skip it.

## Rules

1. Take a device with `devlease` before you boot it or run anything on it. Never run
   `xcrun simctl create|clone`, `avdmanager create avd` or `flutter emulators --create`: the hook
   rejects them.
2. State how long you need the device with `--ttl`. The machine cap is 30 minutes; a longer value is
   refused. Estimate the work, and take what you need, not the maximum.
3. Give the device back as soon as you finish. If you cannot say when you will finish, use `run`:
   it releases the device however the command ends.
4. Do not wait around the queue by polling: `acquire` already waits in order and tells you who
   holds the device and for how long. If `--wait` runs out (exit 75), tell the user instead of
   working around the registry.
5. Do not use a device that someone else leased, even if it is booted.

## Commands

```sh
# One command on a device. DEVLEASE_DEVICE (UDID or AVD name) and DEVLEASE_SERIAL (UDID or
# emulator-5554) exist only inside the command, so expand them in `sh -c '...'` with single quotes.
devlease run --kind ios --profile phone --os 27 --ttl 20m --boot --note "login e2e" -- \
  sh -c 'flutter test integration_test -d "$DEVLEASE_SERIAL"'

# Several commands: take, use, give back.
devlease acquire --kind android --ttl 25m --boot --note "screenshots"   # prints JSON with "lease", "id", "serial"
devlease renew <lease> --ttl 15m        # each renewal is also capped at 30 minutes
devlease release <lease>                # shuts the device down

devlease status                         # who holds what, who waits
```

Selectors: `--profile phone|tablet|<part of the name>`, `--os 27.0` (iOS) or `--os 35` (Android API),
`--device "<exact name or id>"`. The shortest working selector is the best one.

Exit codes: `0` ok, `2` bad arguments (for example ttl above the cap), `3` no device in the pool
fits, `4` a creation request waits for a human, `75` no device became free in `--wait`.

## A device that is not in the pool

On exit `3`, ask for it. Do not create it yourself.

```sh
devlease request-device --kind android --name Pixel_9_API_36 --profile pixel_9 --os 36 --reason "needs API 36 for the permission test"
devlease request-device --kind ios --name "iPhone 17 Pro 27.0" --profile "iPhone 17 Pro" --os 27.0 --reason "..."
```

Result `exists` means a matching device is already there: use the `id` it returns. `created` means it
is ready, so `acquire` it. `pending` (exit 4) means the pool or the disk is at its limit and a human
decides in the web page; continue with another task or report it. `failed` carries the reason, such as
a missing system image: report it, do not install images yourself.

## For the user

The web page (`devlease serve`, default `http://127.0.0.1:7788`) shows the registry, holders, queue,
memory and caches, and cleans caches. Limits: `devlease config`.
