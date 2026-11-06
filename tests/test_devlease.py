"""Run the real CLI, hook and web service against a fake device list in a temp directory."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEVICES = {"devices": [
    {"kind": "ios", "native_id": "UDID-PHONE", "name": "iPhone 17", "profile": "phone", "os": "27.0"},
    {"kind": "ios", "native_id": "UDID-PAD", "name": "iPad Pro 11-inch (M5)", "profile": "tablet", "os": "27.0"},
    {"kind": "ios", "native_id": "UDID-OLD", "name": "iPhone 17 Old", "profile": "phone", "os": "26.5"},
    {"kind": "android", "native_id": "Pixel_8_API_35", "name": "Pixel_8_API_35", "profile": "phone", "os": "35"},
]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fake = self.dir / "fake.json"
        self.fake.write_text(json.dumps(DEVICES))
        self.env = {**os.environ, "DEVLEASE_DIR": str(self.dir / "state"), "DEVLEASE_FAKE_FILE": str(self.fake),
                    "PYTHONPATH": str(ROOT / "src"), "DEVLEASE_HOLDER": "test"}

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, **kw):
        return subprocess.run([sys.executable, "-m", "devlease", *args], capture_output=True, text=True,
                              env=self.env, **kw)

    def popen(self, *args):
        return subprocess.Popen([sys.executable, "-m", "devlease", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env)

    def acquire(self, *extra, ttl="5m"):
        out = self.cli("acquire", "--kind", "ios", "--ttl", ttl, *extra)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def log(self):
        p = self.fake.with_suffix(".log")
        return p.read_text().splitlines() if p.exists() else []


class Leasing(Base):
    def test_ttl_above_the_cap_is_refused(self):
        out = self.cli("acquire", "--kind", "ios", "--ttl", "31m")
        self.assertEqual(out.returncode, 2)
        self.assertIn("1800s", out.stderr)

    def test_ttl_is_required(self):
        self.assertNotEqual(self.cli("acquire", "--kind", "ios").returncode, 0)

    def test_profile_and_os_pick_the_matching_device(self):
        self.assertEqual(self.acquire("--profile", "tablet")["id"], "UDID-PAD")
        self.assertEqual(self.acquire("--os", "26.5")["id"], "UDID-OLD")

    def test_nothing_matches_exits_3_and_points_to_request_device(self):
        out = self.cli("acquire", "--kind", "ios", "--ttl", "5m", "--os", "99")
        self.assertEqual(out.returncode, 3)
        self.assertIn("request-device", out.stderr)

    def test_second_agent_waits_for_the_only_matching_device_then_gets_it(self):
        first = self.acquire("--profile", "tablet")
        waiter = self.popen("acquire", "--kind", "ios", "--ttl", "5m", "--profile", "tablet", "--wait", "30s")
        time.sleep(2)
        self.assertIsNone(waiter.poll(), "must still be waiting")
        self.assertEqual(json.loads(self.cli("status", "--json").stdout)["waiters"][0]["kind"], "ios")
        self.cli("release", str(first["lease"]))
        out, err = waiter.communicate(timeout=15)
        self.assertEqual(waiter.returncode, 0, err)
        self.assertEqual(json.loads(out)["id"], "UDID-PAD")
        self.assertIn("held by test", err)

    def test_wait_timeout_exits_75(self):
        self.acquire("--profile", "tablet")
        out = self.cli("acquire", "--kind", "ios", "--ttl", "5m", "--profile", "tablet", "--wait", "2s")
        self.assertEqual(out.returncode, 75)

    def test_limit_of_simultaneous_devices_is_enforced(self):
        self.acquire("--profile", "tablet")
        self.acquire("--os", "26.5")
        out = self.cli("acquire", "--kind", "ios", "--ttl", "5m", "--wait", "2s")
        self.assertEqual(out.returncode, 75, "limit_ios is 2, the third must wait although a phone is free")

    def test_earlier_waiter_keeps_its_place(self):
        a = self.acquire("--profile", "tablet")
        self.acquire("--os", "26.5")
        w1 = self.popen("acquire", "--kind", "ios", "--ttl", "5m", "--profile", "tablet", "--wait", "30s")
        time.sleep(1.5)
        w2 = self.popen("acquire", "--kind", "ios", "--ttl", "5m", "--profile", "tablet", "--wait", "30s")
        time.sleep(1.5)
        self.cli("release", str(a["lease"]))
        out1, _ = w1.communicate(timeout=15)
        self.assertEqual(w1.returncode, 0)
        self.assertEqual(json.loads(out1)["id"], "UDID-PAD")
        self.assertIsNone(w2.poll(), "the later waiter keeps waiting")
        w2.kill()
        w2.communicate()

    def test_expired_lease_is_taken_over_and_the_device_shut_down(self):
        self.acquire("--profile", "tablet", ttl="1s")
        time.sleep(2)
        self.assertEqual(self.acquire("--profile", "tablet")["id"], "UDID-PAD")
        self.assertIn("shutdown UDID-PAD", self.log())

    def test_dead_holder_pid_frees_the_device(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.acquire("--profile", "tablet", "--pid", str(dead.pid))
        self.assertEqual(self.acquire("--profile", "tablet")["id"], "UDID-PAD")

    def test_renew_extends_but_not_past_the_cap(self):
        lease = self.acquire("--profile", "tablet")
        out = self.cli("renew", str(lease["lease"]), "--ttl", "20m")
        self.assertGreater(json.loads(out.stdout)["expires_at"], lease["expires_at"] + 600)
        self.assertEqual(self.cli("renew", str(lease["lease"]), "--ttl", "40m").returncode, 2)

    def test_renew_of_a_released_lease_fails(self):
        lease = self.acquire("--profile", "tablet")
        self.cli("release", str(lease["lease"]))
        self.assertEqual(self.cli("renew", str(lease["lease"]), "--ttl", "5m").returncode, 2)

    def test_boot_and_release_shutdown(self):
        lease = self.acquire("--profile", "tablet", "--boot")
        self.assertIn("boot UDID-PAD", self.log())
        self.cli("release", str(lease["lease"]))
        self.assertIn("shutdown UDID-PAD", self.log())

    def test_android_lease_reports_the_emulator_serial(self):
        out = self.cli("acquire", "--kind", "android", "--ttl", "5m")
        self.assertEqual(json.loads(out.stdout)["serial"], "emulator-5554")

    def test_run_exports_the_device_and_releases_after_the_command(self):
        out = self.cli("run", "--kind", "ios", "--ttl", "5m", "--profile", "tablet", "--",
                       sys.executable, "-c", "import os,sys;print(os.environ['DEVLEASE_DEVICE']);sys.exit(7)")
        self.assertEqual(out.returncode, 7)
        self.assertEqual(out.stdout.strip(), "UDID-PAD")
        states = json.loads(self.cli("status", "--json").stdout)["devices"]
        self.assertTrue(all(d["lease"] is None for d in states))

    def test_run_stops_the_command_when_the_lease_runs_out(self):
        out = self.cli("run", "--kind", "ios", "--ttl", "2s", "--profile", "tablet", "--",
                       sys.executable, "-c", "import time;time.sleep(60)")
        self.assertEqual(out.returncode, 124)
        self.assertIn("ran out", out.stderr)

    def test_killed_waiter_does_not_hold_up_the_queue(self):
        a = self.acquire("--profile", "tablet")
        w = self.popen("acquire", "--kind", "ios", "--ttl", "5m", "--profile", "tablet", "--wait", "60s")
        time.sleep(1.5)
        w.kill()
        w.communicate()
        self.cli("release", str(a["lease"]))
        self.assertEqual(self.acquire("--profile", "tablet")["id"], "UDID-PAD")


class Requests(Base):
    def request(self, *extra):
        return self.cli("request-device", "--kind", "android", "--reason", "needs API 36", *extra)

    def test_missing_device_is_created_and_then_leasable(self):
        out = self.request("--name", "Pixel_9_API_36", "--profile", "pixel_9", "--os", "36")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["status"], "created")
        self.assertIn("create Pixel_9_API_36", self.log())
        lease = self.cli("acquire", "--kind", "android", "--ttl", "5m", "--device", "Pixel_9_API_36")
        self.assertEqual(lease.returncode, 0, lease.stderr)

    def test_existing_device_is_returned_instead_of_a_duplicate(self):
        out = self.request("--name", "Another", "--profile", "pixel_8", "--os", "35")
        self.assertEqual(json.loads(out.stdout), {"status": "exists", "device": "Pixel_8_API_35", "id": "Pixel_8_API_35"})
        self.assertNotIn("create Another", self.log())

    def test_full_pool_leaves_the_request_for_a_human(self):
        self.cli("config", "max_devices_android", "1")
        out = self.request("--name", "Pixel_9_API_36", "--profile", "pixel_9", "--os", "36")
        self.assertEqual(out.returncode, 4)
        req = json.loads(out.stdout)
        self.assertEqual(req["status"], "pending")
        self.assertNotIn("create Pixel_9_API_36", self.log())
        done = json.loads(self.cli("requests", "approve", str(req["request"])).stdout)
        self.assertEqual(done["status"], "created")

    def test_failed_creation_is_recorded_with_the_reason(self):
        out = self.request("--name", "Broken", "--profile", "unavailable", "--os", "99")
        self.assertEqual(json.loads(out.stdout)["status"], "failed")
        self.assertIn("not installed", json.loads(out.stdout)["error"])


class Hook(Base):
    def hook(self, command):
        event = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        return subprocess.run([sys.executable, "-m", "devlease", "hook", "pretool"], input=event,
                              capture_output=True, text=True, env=self.env)

    def test_creating_devices_is_blocked(self):
        for cmd in ("xcrun simctl create X iPhone-17 ios27", "avdmanager create avd -n x -k y",
                    "flutter emulators --create --name x"):
            out = self.hook(cmd)
            self.assertEqual(out.returncode, 2, cmd)
            self.assertIn("devlease", out.stderr)

    def test_using_an_unleased_pool_device_is_blocked_and_a_leased_one_allowed(self):
        self.cli("status")  # fills the pool
        self.assertEqual(self.hook("xcrun simctl boot UDID-PAD").returncode, 2)
        lease = self.acquire("--profile", "tablet")
        self.assertEqual(self.hook("xcrun simctl boot UDID-PAD").returncode, 0)
        self.assertEqual(self.hook("flutter run -d UDID-PHONE").returncode, 2)
        self.cli("release", str(lease["lease"]))
        self.assertEqual(self.hook("xcrun simctl boot UDID-PAD").returncode, 2)

    def test_listing_shutting_down_and_devlease_itself_are_allowed(self):
        self.cli("status")
        for cmd in ("xcrun simctl list devices", "xcrun simctl shutdown UDID-PAD",
                    "devlease acquire --kind ios --ttl 5m --device UDID-PAD", "ls"):
            self.assertEqual(self.hook(cmd).returncode, 0, cmd)

    def test_android_serial_and_avd_name_are_recognised(self):
        self.cli("status")
        self.assertEqual(self.hook("adb -s emulator-5554 shell ls").returncode, 2)
        self.assertEqual(self.hook("emulator -avd Pixel_8_API_35").returncode, 2)

    def test_broken_registry_lets_commands_through(self):
        (self.dir / "state").mkdir()
        (self.dir / "state" / "devlease.db").write_text("not a database")
        self.assertEqual(self.hook("xcrun simctl create X Y Z").returncode, 0)


class Web(Base):
    def setUp(self):
        super().setUp()
        os.environ.update({k: self.env[k] for k in ("DEVLEASE_DIR", "DEVLEASE_FAKE_FILE")})
        sys.path.insert(0, str(ROOT / "src"))
        from devlease import server
        self.server = server.make_server(0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        with urllib.request.urlopen(req) as r:
            return r.read()

    def post(self, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", json.dumps(body or {}).encode(),
                                     {"X-Devlease": "1", **(headers or {})}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_page_and_state_show_devices_and_holders(self):
        self.assertIn(b"devlease", self.get("/"))
        lease = self.acquire("--profile", "tablet", "--note", "demo")
        state = json.loads(self.get("/api/state"))
        pad = next(d for d in state["devices"] if d["native_id"] == "UDID-PAD")
        self.assertEqual(pad["lease"]["note"], "demo")
        self.assertIn("used", state["memory"])
        self.assertEqual(self.post(f"/api/leases/{lease['lease']}/release")[0], 200)
        state = json.loads(self.get("/api/state"))
        self.assertTrue(all(d["lease"] is None for d in state["devices"]))

    def test_posts_without_the_custom_header_or_with_a_foreign_host_are_refused(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/sync", b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 403)
        self.assertEqual(self.post("/api/sync", headers={"Host": "evil.example"})[0], 403)

    def test_only_whitelisted_caches_can_be_cleaned(self):
        self.assertEqual(self.post("/api/caches/..%2F..%2Fetc/clean")[0], 404)
        self.assertEqual(self.post("/api/caches/not-a-cache/clean")[0], 404)

    def test_cache_clean_removes_contents_and_keeps_the_directory(self):
        from devlease import system
        target = self.dir / "cache"
        (target / "sub").mkdir(parents=True)
        (target / "sub" / "f").write_text("x" * 5000)
        system.CACHES["test"] = {"label": "t", "paths": [target], "busy": [], "note": ""}
        self.addCleanup(system.CACHES.pop, "test")
        status, body = self.post("/api/caches/test/clean")
        self.assertEqual((status, body["status"]), (200, "ok"))
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_busy_cache_is_refused_until_forced(self):
        from devlease import system
        target = self.dir / "cache2"
        target.mkdir()
        (target / "f").write_text("x")
        system.CACHES["busy"] = {"label": "t", "paths": [target], "busy": ["python3", "Python"], "note": ""}
        self.addCleanup(system.CACHES.pop, "busy")
        _, body = self.post("/api/caches/busy/clean")
        self.assertEqual(body["status"], "busy")
        self.assertTrue((target / "f").exists())
        _, body = self.post("/api/caches/busy/clean", {"force": True})
        self.assertEqual(body["status"], "ok")

    def test_request_approval_through_the_page(self):
        self.cli("config", "max_devices_android", "1")
        req = json.loads(self.cli("request-device", "--kind", "android", "--name", "P9", "--profile", "pixel_9",
                                  "--os", "36", "--reason", "x").stdout)
        status, body = self.post(f"/api/requests/{req['request']}/approve")
        self.assertEqual((status, body["status"]), (200, "created"))


if __name__ == "__main__":
    unittest.main()
