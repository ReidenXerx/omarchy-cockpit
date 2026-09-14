#!/usr/bin/python3
"""python3 tests/connectivity_test.py -- the Ports, Network and Devices providers against recorded
tool output, a fake sysfs tree and real processes, then each one end to end through the real
runner. Nothing here stops a process, opens a URL, scans for networks or touches a Bluetooth
device."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.dont_write_bytecode = True   # never leave __pycache__ inside the plugin
sys.path.insert(0, str(BIN))
import plugin_safety as safe  # noqa: E402
import cockpit_common as common  # noqa: E402


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cockpit = load(BIN / "cockpit", "cockpit_runner")
ports = load(ROOT / "providers" / "45-ports", "provider_ports")
network = load(ROOT / "providers" / "72-network", "provider_network")
devices = load(ROOT / "providers" / "75-devices", "provider_devices")


def result(stdout=b"", returncode=0):
    return safe.Result(returncode, stdout if isinstance(stdout, bytes) else stdout.encode(), b"", False, False)


def printed(module):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        module.main()
    return json.loads(out.getvalue())


def fake_reader(path, cap):
    """read_system_file's contract for a tree outside /sys: None when missing, TooLarge over the cap."""
    p = pathlib.Path(path)
    if not p.is_file():
        return None
    data = p.read_bytes()
    if len(data) > cap:
        raise safe.TooLarge(f"{path}: exceeds {cap} bytes")
    return data


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="cockpit-connectivity-", dir=safe.runtime_dir()))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(mock.patch.stopall)

    def spawn(self, *argv):
        """A child that has already exec'd: until then /proc still shows this test's own name and command line."""
        argv = list(argv) or ["/usr/bin/sleep", "60"]
        proc = subprocess.Popen(argv)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        expected = "\0".join(argv).encode()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if pathlib.Path(f"/proc/{proc.pid}/cmdline").read_bytes().rstrip(b"\0") == expected:
                    break
            except OSError:
                pass
            time.sleep(0.01)
        return proc

    def assertSurvivesRunner(self, rows):
        doc, dropped = cockpit.sanitize({"rows": rows}, "test")
        self.assertEqual(dropped, 0)
        self.assertEqual(doc["rows"], rows)


# ------------------------------------------------------------------ ports

class Ports(Sandbox):
    def test_addresses(self):
        split = ports.split_address
        self.assertEqual(split("127.0.0.1:5173"), ("127.0.0.1", 5173))
        self.assertEqual(split("[::]:22"), ("::", 22))
        self.assertEqual(split("*:1716"), ("*", 1716))
        self.assertEqual(split("127.0.0.53%lo:53"), ("127.0.0.53", 53))
        self.assertEqual(split("[fe80::1%wlan0]:5353"), ("fe80::1", 5353))
        self.assertEqual(split("[::ffff:127.0.0.1]:8080"), ("127.0.0.1", 8080))
        for bad in ("127.0.0.1:notaport", "999.1.1.1:80", "127.0.0.1:70000", "127.0.0.1:0", "nocolon", "[::1]:", ""):
            self.assertIsNone(split(bad), bad)
        self.assertEqual([ports.reach(h) for h in ("*", "0.0.0.0", "::", "127.0.0.1", "::1", "192.168.1.5", "224.0.0.251",
                                                    "ff02::fb")],
                         ["network", "network", "network", "local", "local", "network", "multicast", "multicast"])

    def test_script_hints(self):
        hint = ports.script_hint
        self.assertEqual(hint("node\0/x/node_modules/npm/bin/npx-cli.js\0vite\0--port\0" "5173"), "vite")
        self.assertEqual(hint("node\0--inspect\0/srv/app/server.mjs\0"), "server")
        self.assertEqual(hint("python3\0-m\0http.server\08000"), "http.server")
        self.assertEqual(hint("python3\0-c\0import time; time.sleep(60)"), "")
        self.assertEqual(hint("java\0-jar\0/opt/app.jar"), "app")
        self.assertEqual(hint("node\0/usr/lib/node_modules/npm/bin/npm-cli.js"), "npm-cli")
        self.assertEqual(hint("node"), "")
        self.assertEqual(hint("node\0/x/evil\x1b]0;x\x07.js"), "evil�]0;x�")

    def test_rows_for_processes_you_own_and_one_line_for_the_rest(self):
        server = self.spawn()
        lan = self.spawn()
        script = self.root / "devserver.py"
        script.write_text("import time\ntime.sleep(60)\n")
        interpreter = self.spawn("/usr/bin/python3", str(script))
        children = sorted((self.spawn(), self.spawn()), key=lambda p: p.pid)
        gone = subprocess.Popen(["/usr/bin/true"])
        gone.wait()
        text = "\n".join([
            f'tcp LISTEN 0 4096 127.0.0.1:5173 0.0.0.0:* users:(("sleep",pid={server.pid},fd=21))',
            f'tcp LISTEN 0 511 [::1]:5174 [::]:* users:(("sleep",pid={server.pid},fd=22))',
            f'udp UNCONN 0 0 224.0.0.251:5353 0.0.0.0:* users:(("brave",pid={server.pid},fd=277))',
            f'udp UNCONN 0 0 [ff02::fb]:5353 [::]:* users:(("brave",pid={server.pid},fd=9))',
            f'tcp LISTEN 0 10 10.0.0.5:9000 0.0.0.0:* users:(("fake name",pid={lan.pid},fd=3))',
            f'udp UNCONN 0 0 0.0.0.0:41641 0.0.0.0:* users:(("sleep",pid={lan.pid},fd=4))',
            f'tcp LISTEN 0 128 [::]:8000 [::]:* users:(("python3",pid={interpreter.pid},fd=3))',
            f'tcp LISTEN 0 50 *:1716 *:* users:(("x",pid={children[1].pid},fd=3),("x",pid={children[0].pid},fd=3))',
            "udp UNCONN 0 0 0.0.0.0:5353 0.0.0.0:*",
            "tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:*",
            "tcp LISTEN 0 128 [::]:22 [::]:*",
            "udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*",
            "udp UNCONN 0 0 [fe80::1%wlan0]:546 [::]:*",
            'tcp LISTEN 0 10 127.0.0.1:631 0.0.0.0:* users:(("cupsd",pid=1,fd=7))',
            f'tcp LISTEN 0 10 127.0.0.1:7000 0.0.0.0:* users:(("gone",pid={gone.pid},fd=7))',
            "garbage line",
            "tcp LISTEN 0 10 127.0.0.1:notaport 0.0.0.0:*",
            "tcp LISTEN 0 10 999.1.1.1:80 0.0.0.0:*",
            "sctp LISTEN 0 10 127.0.0.1:80 0.0.0.0:*",
        ])
        rows = ports.rows_for(ports.listeners(text), os.getuid(), common.uptime(), ports.clock_ticks())
        self.assertSurvivesRunner(rows)

        # What other machines can reach comes first; the system line comes last.
        self.assertEqual([r["title"] for r in rows],
                         ["python3 · devserver", "sleep", "sleep", "sleep", "System services"])
        self.assertEqual(rows[-1]["detail"], "tcp 22, 631, 7000 · udp 53, 546, 5353")
        self.assertNotIn("buttons", rows[-1])

        def row_for(pid):
            matches = [r for r in rows if any(b["action"].get("pid") == pid for b in r.get("buttons", []))]
            self.assertEqual(len(matches), 1, pid)
            return matches[0]

        local = row_for(server.pid)
        self.assertTrue(local["detail"].startswith("tcp 5173, 5174 · local only · up "), local["detail"])
        self.assertEqual(local["glyph"], ports.GLYPH_LOCAL)
        self.assertEqual(local["buttons"][0]["action"], {"kind": "open-url", "url": "http://127.0.0.1:5173/"})
        stop = local["buttons"][1]
        self.assertEqual(stop["action"], {"kind": "stop-process", "pid": server.pid, "start": common.proc_start(server.pid)})
        self.assertEqual(stop["confirm"], f"Stop sleep (pid {server.pid})?")
        self.assertEqual(common.validate_action(stop["action"]), stop["action"])

        # Bound to a LAN address only: reachable, but not at 127.0.0.1, so there is nothing to open.
        reachable = row_for(lan.pid)
        self.assertTrue(reachable["detail"].startswith("tcp 9000 · udp 41641 · reachable on your network"))
        self.assertEqual([b["action"]["kind"] for b in reachable["buttons"]], ["stop-process"])
        self.assertEqual(reachable["glyph"], ports.GLYPH_NETWORK)

        self.assertEqual(row_for(interpreter.pid)["buttons"][0]["action"]["url"], "http://127.0.0.1:8000/")
        # A socket shared by two processes is shown once, on the lower pid.
        shared = row_for(children[0].pid)
        self.assertIn("tcp 1716 ·", shared["detail"])
        self.assertFalse([r for r in rows if any(b["action"].get("pid") == children[1].pid for b in r.get("buttons", []))])

    def test_a_port_bound_to_ipv6_loopback_only_opens_there(self):
        server = self.spawn()
        rows = ports.rows_for(ports.listeners(f'tcp LISTEN 0 511 [::1]:5174 [::]:* users:(("s",pid={server.pid},fd=22))'),
                              os.getuid(), common.uptime(), 100)
        self.assertEqual(rows[0]["buttons"][0]["action"], {"kind": "open-url", "url": "http://[::1]:5174/"})
        rows = ports.rows_for(ports.listeners(f'udp UNCONN 0 0 127.0.0.1:5000 0.0.0.0:* users:(("s",pid={server.pid},fd=2))'),
                              os.getuid(), common.uptime(), 100)
        self.assertEqual([b["action"]["kind"] for b in rows[0]["buttons"]], ["stop-process"])

    def test_no_stop_button_without_a_start_time(self):
        server = self.spawn()
        mock.patch.object(common, "proc_start", lambda pid: None).start()
        rows = ports.rows_for(ports.listeners(f'tcp LISTEN 0 1 127.0.0.1:5173 0.0.0.0:* users:(("s",pid={server.pid},fd=2))'),
                              os.getuid(), common.uptime(), 100)
        self.assertEqual([b["action"]["kind"] for b in rows[0]["buttons"]], ["open-url"])
        self.assertEqual(rows[0]["detail"], "tcp 5173 · local only")

    def test_hostile_names_and_caps(self):
        mock.patch.object(common, "proc_read",
                          lambda pid, name, cap: "evil\x1b]0;x\x07name that is far too long for a row\n" if name == "comm" else None).start()
        mock.patch.object(common, "proc_start", lambda pid: "123").start()
        mock.patch.object(ports, "owned", lambda pid, uid, cache: True).start()
        many = "\n".join(f'tcp LISTEN 0 1 127.0.0.1:{1000 + i} 0.0.0.0:* users:(("x",pid={1000 + i},fd=3))' for i in range(100))
        wide = "\n".join(f'tcp LISTEN 0 1 127.0.0.1:{2000 + i} 0.0.0.0:* users:(("x",pid=5,fd={i}))' for i in range(10))
        rows = ports.rows_for(ports.listeners(many + "\n" + wide), os.getuid(), 1000.0, 100)
        self.assertLessEqual(len(rows), ports.ROWS_MAX)
        self.assertEqual(rows[0]["title"], common.clean_text("evil\x1b]0;x\x07name that is far too long for a row", ports.NAME_MAX))
        self.assertEqual(len(rows[0]["title"]), ports.NAME_MAX)
        self.assertLessEqual(len(rows[0]["buttons"][1]["label"]), cockpit.LABEL_MAX)
        self.assertSurvivesRunner(rows)
        wide_row = [r for r in rows if any(b["action"].get("pid") == 5 for b in r["buttons"])][0]
        self.assertTrue(wide_row["detail"].startswith("tcp 2000, 2001, 2002, 2003, 2004, 2005 +4 · local only"))
        huge = "tcp LISTEN 0 1 127.0.0.1:1 0.0.0.0:*\n" * 5000
        self.assertEqual(len(ports.listeners(huge)), ports.LINES_MAX)

    def test_main_prints_a_document_when_ss_fails(self):
        for patch in (lambda: mock.patch.object(safe, "has_tool", lambda name: False),
                      lambda: mock.patch.object(safe, "run", lambda argv, **kw: result(b"", 1)),
                      lambda: mock.patch.object(safe, "run", mock.Mock(side_effect=OSError("boom"))),
                      lambda: mock.patch.object(safe, "run", lambda argv, **kw: safe.Result(0, b"x", b"", False, True))):
            with patch():
                doc = printed(ports)
            self.assertEqual((doc["section"], doc["priority"], doc["rows"]), ("Ports", 45, []))


# ------------------------------------------------------------------ network

NM_STATUS = "\n".join([
    r"wlan0:wifi:connected:Cafe\: Guest",
    "enp3s0:ethernet:connected:Wired connection 1",
    "wg0:wireguard:connected (externally):wg0",
    "tailscale0:tun:connected (externally):tailscale0",
    "lo:loopback:connected (externally):lo",
    "p2p-dev-wlan0:wifi-p2p:disconnected:",
    "docker0:bridge:connected (externally):docker0",
    "vethab12:ethernet:connected (externally):vethab12",
    "enp0s1:ethernet:unavailable:",
    r"we\\ird:ethernet:connected:x",
    "wwan0:gsm:connecting (getting IP configuration):Carrier",
    "usb0:gsm:connected:Phone\x1b[31m tether",
    "broken line",
])
NM_WIFI = "\n".join([r"no:wlan0:Other:80:130 Mbit/s", r"yes:wlan0:Cafe\: Guest:22:54 Mbit/s", "yes:wlan0:Second:90:1 Mbit/s"])
IP_ADDR = [
    {"ifname": "lo", "flags": ["LOOPBACK", "UP", "LOWER_UP"], "link_type": "loopback",
     "addr_info": [{"family": "inet", "local": "127.0.0.1", "scope": "host"}]},
    {"ifname": "wlan0", "flags": ["BROADCAST", "UP", "LOWER_UP"], "link_type": "ether",
     "addr_info": [{"family": "inet6", "local": "fe80::1", "scope": "link"},
                   {"family": "inet", "local": "192.168.1.20", "scope": "global"}]},
    {"ifname": "enp3s0", "flags": ["BROADCAST", "UP", "LOWER_UP"], "link_type": "ether",
     "addr_info": [{"family": "inet6", "local": "2001:db8::5", "scope": "global"}]},
    {"ifname": "wg0", "flags": ["POINTOPOINT", "UP", "LOWER_UP"], "link_type": "none",
     "addr_info": [{"family": "inet", "local": "10.8.0.2", "scope": "global"}]},
    {"ifname": "enp9s0", "flags": ["BROADCAST", "UP"], "link_type": "ether",
     "addr_info": [{"family": "inet", "local": "10.9.9.9", "scope": "global"}]},
    {"ifname": "tailscale0", "flags": ["POINTOPOINT", "UP", "LOWER_UP"], "link_type": "none",
     "addr_info": [{"family": "inet", "local": "100.64.0.1", "scope": "global"}]},
    {"ifname": "enp7s0", "flags": ["BROADCAST", "UP", "LOWER_UP"], "link_type": "ether", "addr_info": []},
    "junk", {"ifname": 5}, {"ifname": "x", "addr_info": "nope"},
]


class Network(Sandbox):
    def setUp(self):
        super().setUp()
        self.sys = self.root / "net"
        for name, speed in (("enp3s0", "1000\n"), ("wlan0", None), ("wg0", None)):
            (self.sys / name).mkdir(parents=True)
            if speed:
                (self.sys / name / "speed").write_text(speed)
        (self.sys / "wlan0" / "wireless").mkdir()
        mock.patch.object(network, "NET_ROOT", str(self.sys)).start()
        mock.patch.object(network, "READ", fake_reader).start()
        self.calls = []
        self.answers = {"status": result(NM_STATUS), "wifi": result(NM_WIFI), "connectivity": result("portal\n"),
                        "ip": result(json.dumps(IP_ADDR))}

    def fake_run(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[0] == "ip":
            answer = self.answers["ip"]
        elif "status" in argv:
            answer = self.answers["status"]
        elif "wifi" in argv:
            answer = self.answers["wifi"]
        else:
            answer = self.answers["connectivity"]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def document(self, tools=("ip", "nmcli")):
        mock.patch.object(safe, "run", self.fake_run).start()
        mock.patch.object(safe, "has_tool", lambda name: name in tools).start()
        return printed(network)

    def test_terse_fields(self):
        self.assertEqual(network.terse_fields(r"a\:b:c\\d:"), ["a:b", "c\\d", ""])
        self.assertEqual(network.terse_fields("plain"), ["plain"])
        self.assertEqual(network.terse_fields("trailing\\"), ["trailing\\"])

    def test_network_manager_rows(self):
        doc = self.document()
        rows = doc["rows"]
        self.assertSurvivesRunner(rows)
        self.assertEqual((doc["section"], doc["priority"], doc["timeoutSec"]), ("Network", 72, 8))
        self.assertEqual(rows[0], {"glyph": network.GLYPH_OFFLINE, "title": "Sign in to this network",
                                   "detail": "a captive portal stands between you and the internet", "state": "warn"})
        self.assertEqual(rows[1], {"glyph": network.GLYPH_WIFI, "title": "Cafe: Guest",
                                   "detail": "wlan0 · 22% · 54 Mbit/s · 192.168.1.20", "state": "warn"})
        self.assertEqual(rows[2], {"glyph": network.GLYPH_WIRED, "title": "Wired connection 1",
                                   "detail": "enp3s0 · 1 Gb/s · 2001:db8::5", "state": "ok"})
        self.assertEqual(rows[3], {"glyph": network.GLYPH_VPN, "title": "wg0", "detail": "wg0 · 10.8.0.2", "state": "ok"})
        self.assertEqual(rows[4], {"glyph": network.GLYPH, "title": "Phone�[31m tether", "detail": "usb0 · gsm",
                                   "state": "ok"})
        self.assertEqual(len(rows), 5)
        self.assertFalse(any("action" in r or "buttons" in r for r in rows))
        # Nothing asks NetworkManager to scan, and every call has a deadline inside the budget.
        wifi_call = [argv for argv, _ in self.calls if "wifi" in argv][0]
        self.assertEqual(wifi_call[-2:], ["--rescan", "no"])
        self.assertTrue(all(0 < kw["timeout"] <= 3 and kw["max_output"] for _, kw in self.calls))

    def test_connectivity_states(self):
        for state, title in (("full\n", None), ("unknown\n", None), ("none\n", "No internet"),
                             ("limited\n", "Limited connectivity")):
            self.answers["connectivity"] = result(state)
            self.calls = []
            rows = self.document()["rows"]
            leading = rows[0]["title"] if rows and rows[0]["state"] == "warn" and rows[0]["glyph"] == network.GLYPH_OFFLINE else None
            self.assertEqual(leading, title, state)
            mock.patch.stopall()
            mock.patch.object(network, "NET_ROOT", str(self.sys)).start()
            mock.patch.object(network, "READ", fake_reader).start()

    def test_without_network_manager_the_kernel_view_is_used(self):
        for tools, status in ((("ip",), result(NM_STATUS)), (("ip", "nmcli"), result(b"Error: NetworkManager is not running.", 8)),
                              (("ip", "nmcli"), OSError("boom"))):
            self.answers["status"] = status
            rows = self.document(tools)["rows"]
            self.assertEqual(rows, [
                {"glyph": network.GLYPH_WIFI, "title": "wlan0", "detail": "Wi-Fi · 192.168.1.20", "state": "ok"},
                {"glyph": network.GLYPH_WIRED, "title": "enp3s0", "detail": "wired · 1 Gb/s · 2001:db8::5", "state": "ok"},
                {"glyph": network.GLYPH_VPN, "title": "wg0", "detail": "tunnel · 10.8.0.2", "state": "ok"},
            ], tools)
            mock.patch.stopall()
            mock.patch.object(network, "NET_ROOT", str(self.sys)).start()
            mock.patch.object(network, "READ", fake_reader).start()

    def test_nothing_answers(self):
        doc = self.document(tools=())
        self.assertEqual(doc["rows"], [])
        self.answers["ip"] = result(b"[" * 50 + b"]" * 50)
        self.answers["status"] = result(b"", 1)
        self.assertEqual(self.document()["rows"], [])

    def test_link_speeds(self):
        speed = self.sys / "enp3s0" / "speed"
        for text, shown in (("2500\n", "2.5 Gb/s"), ("100\n", "100 Mb/s"), ("-1\n", ""), ("fast\n", ""), ("9" * 40, "")):
            speed.write_text(text)
            self.assertEqual(network.link_speed("enp3s0"), shown, text)
        self.assertEqual(network.link_speed("missing0"), "")


# ------------------------------------------------------------------ devices

MONITORS = [
    {"name": "eDP-1", "description": "AU Optronics 0x3CA2", "make": "AU Optronics", "model": "0x3CA2", "width": 2560,
     "height": 1600, "refreshRate": 240.024, "scale": 1.6, "disabled": False, "dpmsStatus": True, "mirrorOf": "none"},
    {"name": "DP-3", "description": "Dell Inc. DELL U2723QE 5KC0P83", "make": "Dell Inc.", "model": "DELL U2723QE",
     "serial": "5KC0P83", "width": 3840, "height": 2160, "refreshRate": 59.997, "scale": 1.5, "disabled": False,
     "dpmsStatus": True, "mirrorOf": "none"},
    {"name": "HDMI-A-1", "description": "Samsung Electric Company Odyssey G5 H4ZR", "make": "Samsung Electric Company",
     "model": "Odyssey G5", "serial": "H4ZR", "width": 2560, "height": 1440, "refreshRate": 144.0, "scale": 1.0,
     "disabled": True},
    {"name": "DP-4", "description": "Unknown 0x1234 SER1", "make": "", "model": "0x1234", "serial": "SER1",
     "width": "x", "height": None, "refreshRate": True, "scale": "big"},
    {"name": "DP-5\x1b[31m", "make": "LG Electronics", "model": "LG ULTRAGEAR", "width": 1920, "height": 1080,
     "refreshRate": 0, "scale": 1, "mirrorOf": "DP-3", "dpmsStatus": False},
    "junk", {"name": 5}, {"name": ""},
]


def variant(kind, value):
    return {"type": kind, "data": value}


BLUEZ = {"type": "a{oa{sa{sv}}}", "data": [{
    "/org/bluez": {"org.freedesktop.DBus.Introspectable": {}, "org.bluez.AgentManager1": {}},
    "/org/bluez/hci0": {"org.bluez.Adapter1": {"Powered": variant("b", True)}},
    "/org/bluez/hci0/dev_AA": {"org.bluez.Device1": {"Alias": variant("s", "Buds\x07"), "Icon": variant("s", "audio-headset"),
                                                     "Connected": variant("b", True)},
                               "org.bluez.Battery1": {"Percentage": variant("y", 15)}},
    "/org/bluez/hci0/dev_BB": {"org.bluez.Device1": {"Name": variant("s", "Keyboard K3"), "Icon": variant("s", "input-keyboard"),
                                                     "Connected": variant("b", True)},
                               "org.bluez.Battery1": {"Percentage": variant("y", 80)}},
    "/org/bluez/hci0/dev_CC": {"org.bluez.Device1": {"Alias": variant("s", "Old mouse"), "Icon": variant("s", "input-mouse"),
                                                     "Connected": variant("b", False)}},
    "/org/bluez/hci0/dev_DD": {"org.bluez.Device1": {"Connected": variant("b", True), "Icon": variant("s", "something-else")},
                               "org.bluez.Battery1": {"Percentage": variant("y", True)}},
    "/org/bluez/hci0/dev_EE": {"org.bluez.Device1": {"Connected": variant("b", "true")}},
    "/org/bluez/hci0/dev_FF": {"org.bluez.Device1": {"Connected": variant("b", True), "Icon": variant("s", ["x"]),
                                                     "Alias": variant("s", 7)},
                               "org.bluez.Battery1": {"Percentage": variant("y", 250)}},
    "/bad": "not a dict",
}]}


class Devices(Sandbox):
    def usb(self, name, **attributes):
        path = self.usb_root / name
        path.mkdir(parents=True)
        for key, value in attributes.items():
            (path / key).write_text(value + "\n")

    def setUp(self):
        super().setUp()
        self.usb_root = self.root / "usb"
        self.usb("usb1", removable="unknown", bDeviceClass="09", product="xHCI Host Controller")
        self.usb("1-1", removable="fixed", bDeviceClass="ef", product="Integrated Webcam", speed="480")
        self.usb("1-1:1.0", bInterfaceClass="0e")
        self.usb("1-2", removable="removable", bDeviceClass="00", product="USB Flash", speed="5000")
        self.usb("1-2:1.0", bInterfaceClass="08")
        self.usb("1-3", removable="removable", bDeviceClass="09", product="4-Port Hub", speed="480")
        self.usb("1-3.1", removable="unknown", bDeviceClass="00", manufacturer="Logitech", idVendor="046d",
                 idProduct="c52b", speed="12")
        self.usb("1-3.1:1.0", bInterfaceClass="03")
        self.usb("1-3.2", removable="unknown", product="Evil\x1b]0;x\x07", speed="abc")
        self.usb("1-3.2.1", removable="unknown", product="Behind two hubs", speed="480")
        self.usb("1-4", removable="unknown", product="Internal adapter", speed="12")
        self.usb("1-5", removable="removable", bDeviceClass="00", product="Hub in disguise")
        self.usb("1-5:1.0", bInterfaceClass="09")
        self.usb("1-6", removable="removable", idVendor="1234", idProduct="abcd", speed="1.5")
        self.usb("2-1.1", removable="unknown", product="Orphan", speed="480")
        self.usb("notadevice", removable="removable", product="Nope")
        self.usb("1-7", removable="removable", product="x" * 200)
        mock.patch.object(devices, "USB_ROOT", str(self.usb_root)).start()
        mock.patch.object(devices, "READ", fake_reader).start()

    def test_displays(self):
        rows = devices.display_rows(MONITORS)
        self.assertSurvivesRunner(rows)
        self.assertEqual(rows, [
            {"glyph": devices.GLYPH_MONITOR, "title": "DELL U2723QE", "detail": "DP-3 · 3840×2160 @ 60 Hz · scale 1.5",
             "state": "ok"},
            {"glyph": devices.GLYPH_MONITOR, "title": "Samsung Odyssey G5", "detail": "HDMI-A-1 · off", "state": "idle"},
            {"glyph": devices.GLYPH_MONITOR, "title": "Unknown 0x1234", "detail": "DP-4", "state": "ok"},
            {"glyph": devices.GLYPH_MONITOR, "title": "LG ULTRAGEAR", "detail": "DP-5�[31m · 1920×1080 · mirrors DP-3 · asleep",
             "state": "ok"},
        ])
        self.assertEqual(devices.display_rows(None), [])
        self.assertEqual(devices.display_rows({"name": "DP-1"}), [])

    def test_bluetooth_connected_devices_only(self):
        rows = devices.bluetooth_rows(BLUEZ)
        self.assertSurvivesRunner(rows)
        self.assertEqual(rows, [
            {"glyph": devices.GLYPH_BLUETOOTH, "title": "Bluetooth device", "detail": "Bluetooth", "state": "ok"},
            {"glyph": devices.GLYPH_BLUETOOTH, "title": "Bluetooth device", "detail": "Bluetooth", "state": "ok"},
            {"glyph": devices.GLYPH_HEADPHONES, "title": "Buds�", "detail": "Bluetooth · 15%", "state": "warn"},
            {"glyph": devices.GLYPH_KEYBOARD, "title": "Keyboard K3", "detail": "Bluetooth · 80%", "state": "ok"},
        ])
        for reply in (None, {}, {"data": []}, {"data": ["x"]}, {"data": "x"}, []):
            self.assertEqual(devices.bluetooth_rows(reply), [], reply)

    def test_usb_lists_external_devices_and_what_hangs_off_external_hubs(self):
        rows = devices.usb_rows(devices.usb_entries())
        self.assertSurvivesRunner(rows)
        self.assertEqual([(r["title"][:20], r["detail"], r["glyph"]) for r in rows], [
            ("Behind two hubs", "USB · 480 Mbps", devices.GLYPH_USB),
            ("Evil�]0;x�", "USB", devices.GLYPH_USB),
            ("Logitech 046d:c52b", "USB · 12 Mbps", devices.GLYPH_USB),
            ("USB device 1234:abcd", "USB · 1.5 Mbps", devices.GLYPH_USB),
            ("USB Flash", "USB · 5 Gbps", devices.GLYPH_STORAGE),
            ("x" * 20, "USB", devices.GLYPH_USB),
        ])
        self.assertEqual(len(rows[-1]["title"]), 128)
        self.assertEqual(devices.speed_text("nan"), "")
        self.assertEqual(devices.speed_text("inf"), "")
        self.assertEqual(devices.speed_text("20000"), "20 Gbps")

    def test_oversized_attributes_are_not_read(self):
        (self.usb_root / "1-2" / "product").write_text("y" * 5000)
        titles = [r["title"] for r in devices.usb_rows(devices.usb_entries())]
        self.assertIn("USB device :", titles)   # no product, no manufacturer, no ids: still one plain row

    def test_main_orders_sections_and_never_starts_bluez(self):
        seen = []

        def fake_run(argv, **kw):
            seen.append(argv)
            if argv[0] == "hyprctl":
                return result(json.dumps(MONITORS[:2]))
            return result(json.dumps(BLUEZ))

        mock.patch.object(safe, "run", fake_run).start()
        mock.patch.object(safe, "has_tool", lambda name: True).start()
        doc = printed(devices)
        self.assertEqual((doc["section"], doc["priority"], doc["timeoutSec"]), ("Devices", 75, 8))
        glyphs = [r["glyph"] for r in doc["rows"]]
        self.assertEqual(glyphs[0], devices.GLYPH_MONITOR)
        self.assertEqual(glyphs[1:5], [devices.GLYPH_BLUETOOTH, devices.GLYPH_BLUETOOTH, devices.GLYPH_HEADPHONES,
                                       devices.GLYPH_KEYBOARD])
        self.assertIn("--auto-start=no", [a for argv in seen for a in argv if a.startswith("--auto")])
        self.assertEqual(seen[0], ["hyprctl", "-j", "monitors", "all"])

    def test_main_when_nothing_answers(self):
        mock.patch.object(devices, "USB_ROOT", str(self.root / "missing")).start()
        with mock.patch.object(safe, "has_tool", lambda name: False):
            self.assertEqual(printed(devices)["rows"], [])
        with mock.patch.object(safe, "has_tool", lambda name: True):
            with mock.patch.object(safe, "run", mock.Mock(side_effect=OSError("boom"))):
                self.assertEqual(printed(devices)["rows"], [])
            with mock.patch.object(safe, "run", lambda argv, **kw: result(b"[" * 40 + b"]" * 40)):
                self.assertEqual(printed(devices)["rows"], [])


# ------------------------------------------------------------------ live

class Live(Sandbox):
    """Each provider as the hub runs it: a separate interpreter under the real runner, read-only."""

    def run_provider(self, name):
        mock.patch.object(cockpit, "CACHE", self.root / "cache").start()
        path = ROOT / "providers" / name
        provider = cockpit.Provider(name, [cockpit.PYTHON, str(path)], "built-in", str(path), None)
        doc, note = cockpit.run_one(provider, force=True)
        self.assertEqual(note, "ok")
        for row in doc["rows"]:
            self.assertNotIn("action", row)
            for button in row.get("buttons", []):
                self.assertEqual(common.validate_action(button["action"]), button["action"])
                self.assertIn(button["action"]["kind"], ("open-url", "stop-process"))
        self.assertLessEqual(doc["timeoutSec"], cockpit.TIMEOUT_MAX)
        return doc

    @unittest.skipUnless(safe.has_tool("ss"), "ss not installed")
    def test_ports(self):
        self.assertEqual(self.run_provider("45-ports")["section"], "Ports")

    @unittest.skipUnless(safe.has_tool("ip") or safe.has_tool("nmcli"), "neither ip nor nmcli installed")
    def test_network(self):
        self.assertEqual(self.run_provider("72-network")["section"], "Network")

    def test_devices(self):
        self.assertEqual(self.run_provider("75-devices")["section"], "Devices")

    def test_sources_follow_the_house_rules(self):
        for name in ("45-ports", "72-network", "75-devices"):
            source = (ROOT / "providers" / name).read_text()
            self.assertTrue(source.startswith("#!/usr/bin/python3\n"), name)
            for banned in ("import subprocess", "shutil.which", "os.system", "/usr/bin/env", "shell=True"):
                self.assertNotIn(banned, source, name)
            self.assertFalse([c for c in source if ord(c) < 32 and c != "\n"], name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
