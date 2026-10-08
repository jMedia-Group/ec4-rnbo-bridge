import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import ec4_sysex as sx  # noqa: E402
from ec4bridge import DEFAULTS, Bridge  # noqa: E402
from layout import abbreviate, build_layout  # noqa: E402
from mock_runner import MockRunner, default_tree  # noqa: E402
from rnbo import parse_params  # noqa: E402


def synthetic_dump(seed=7):
    mem = bytearray((i * seed) & 0xFF for i in range(sx.MEMORY_SIZE))
    return sx.Dump(mem, 2, 0)


class FakeMidi:
    connected = True

    def __init__(self):
        self.sent = []

    def send_ccs(self, msgs, pause=0):
        self.sent += msgs


def cfg(**kw):
    c = dict(DEFAULTS)
    c.update(kw)
    return c


class SysexTests(unittest.TestCase):
    def test_roundtrip(self):
        d = synthetic_dump()
        raw = sx.build_dump(d)
        self.assertEqual(len(raw), 229340)  # same size the official editor produces
        back = sx.parse_dump(raw)
        self.assertEqual(back.memory, d.memory)
        self.assertEqual((back.fw_hi, back.fw_lo), (2, 0))
        self.assertEqual(b"".join(sx.split_for_sending(raw)), raw)

    def test_checksum_error(self):
        raw = bytearray(sx.build_dump(synthetic_dump()))
        i = raw.index(0x4D, 40)
        raw[i + 2] ^= 0x01
        with self.assertRaises(sx.DumpError):
            sx.parse_dump(bytes(raw))

    def test_rejects_v1(self):
        d = synthetic_dump()
        d.fw_hi = 1
        with self.assertRaises(sx.DumpError):
            sx.parse_dump(sx.build_dump(d))

    def test_apply_layout_only_touches_one_setup(self):
        d = synthetic_dump()
        before = bytes(d.memory)
        names = [[None] * 16 for _ in range(16)]
        names[0][0] = "Cutf"
        names[0][1] = "Re_s"  # '_' is not displayable -> space
        sx.apply_layout(d, 15, "RNBO", ["Syn1", "Syn2"], names, cc_base=16, resolution="7bit", mode="Acc1")
        n = sx.read_names(d, 15)
        self.assertEqual(n["setup"], "RNBO")
        self.assertEqual(n["groups"][:3], ["Syn1", "Syn2", "    "])
        self.assertEqual(n["encoders"][0][:3], ["Cutf", "Re s", "    "])
        base = sx._group_base(15, 3)
        self.assertEqual(d.memory[base + 5], (2 << 4) | 3)  # CC abs, channel 4
        self.assertEqual(d.memory[base + 16 + 5], 21)  # CC 16+5
        self.assertEqual(d.memory[base + 64 + 5], 127)
        self.assertEqual(d.memory[base + 80 + 5] >> 4, 4)  # Acc1
        self.assertEqual(d.memory[base + 80 + 5] & 0xF, 0)  # unused -> display off
        self.assertEqual(d.memory[sx._group_base(15, 0) + 80] & 0xF, 2)  # used -> 0..100
        # everything outside setup 16's areas is unchanged
        changed = [i for i in range(len(before)) if before[i] != d.memory[i]]
        allowed = set(range(sx.ADDR_SETUP_NAMES + 60, sx.ADDR_SETUP_NAMES + 64))
        allowed |= set(range(sx.ADDR_GROUP_NAMES + 15 * 64, sx.ADDR_GROUP_NAMES + 16 * 64))
        allowed |= set(range(sx._group_base(15, 0), sx._group_base(15, 0) + 16 * 192))
        self.assertTrue(set(changed) <= allowed)
        # and the result is still a valid dump
        sx.parse_dump(sx.build_dump(d))

    def test_14bit_limits(self):
        d = synthetic_dump()
        names = [["Abcd"] * 16 for _ in range(16)]
        sx.apply_layout(d, 0, "R", [], names, cc_base=16, resolution="14bit", mode="Acc3")
        b = sx._group_base(0, 0)
        self.assertEqual(d.memory[b] >> 4, 4)
        self.assertEqual(d.memory[b + 48], 0)
        self.assertEqual(d.memory[b + 64] + ((d.memory[b + 96] >> 4) << 8), 4095)  # = 16383
        with self.assertRaises(ValueError):
            sx.apply_layout(d, 0, "R", [], names, cc_base=20, resolution="14bit", mode="Acc3")


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.params = parse_params(default_tree())

    def test_parse(self):
        keys = [p.key for p in self.params]
        self.assertEqual(keys[0], "0/volume")  # display_order wins
        self.assertIn("0/env/attack", keys)
        self.assertEqual(len([k for k in keys if k.startswith("0/")]), 20)
        self.assertEqual(len([k for k in keys if k.startswith("1/")]), 3)
        wave = next(p for p in self.params if p.pid == "wave")
        self.assertEqual(wave.enum_values, ["sine", "saw", "square"])
        self.assertAlmostEqual(wave.normalized, 0.5)
        cut = next(p for p in self.params if p.pid == "cutoff")
        self.assertEqual(cut.label, "Cutoff")
        self.assertEqual(cut.address, "/rnbo/inst/0/params/cutoff/normalized")
        self.assertEqual(next(p for p in self.params if p.inst == 1).inst_name, "Delay")

    def test_layout(self):
        lay = build_layout(self.params, cfg())
        groups = {}
        for s in lay.slots:
            groups.setdefault(s.group, []).append(s)
        self.assertEqual([len(groups[g]) for g in sorted(groups)], [16, 4, 3])
        self.assertEqual(lay.group_names[:4], ["Pol1", "Pol2", "Dely", ""])
        for g in groups.values():
            names = [s.short.lower() for s in g]
            self.assertEqual(len(names), len(set(names)), names)
        self.assertTrue(all(len(s.short) <= 4 for s in lay.slots))

    def test_filters_and_overrides(self):
        lay = build_layout(self.params, cfg(exclude=["extra"], names={"0/cutoff": "Filt"},
                                            group_names={"1": "Echo"}, new_group_per_instance=False))
        self.assertEqual(len(lay.slots), 9)
        self.assertEqual(next(s for s in lay.slots if s.param.pid == "cutoff").short, "Filt")
        self.assertEqual({s.group for s in lay.slots}, {0})  # packed into one group

    def test_strip_prefixes(self):
        from mock_runner import make_instance
        tree = {"CONTENTS": {"0": make_instance(0, "j.reverb", [("j.size", 0, 0.5), ("decay", 1, 0.5)])}}
        lay = build_layout(parse_params(tree), cfg(strip_prefixes=["j."]))
        self.assertEqual(lay.group_names[0], "Revr")
        self.assertEqual([s.short for s in lay.slots], ["Size", "Decy"])
        lay = build_layout(parse_params(tree), cfg())
        self.assertEqual(lay.group_names[0], "JRe")  # without the option

    def test_overflow(self):
        many = parse_params(default_tree()) * 13  # 299 params
        lay = build_layout(many, cfg(new_group_per_instance=False))
        self.assertEqual(len(lay.slots), 256)
        self.assertEqual(len(lay.skipped), len(many) - 256)

    def test_abbreviate(self):
        self.assertEqual(abbreviate("cutoff"), "Cutf")
        self.assertEqual(abbreviate("resonance"), "Resn")
        self.assertEqual(abbreviate("osc2Level"), "Os2L")
        self.assertEqual(abbreviate("mix"), "Mix")


class BridgeTests(unittest.TestCase):
    def make(self, **kw):
        self.midi = FakeMidi()
        self.osc = []
        with tempfile.TemporaryDirectory() as d:
            c = cfg(layout_txt=os.path.join(d, "l.txt"), backup_syx=os.path.join(d, "none.syx"), **kw)
            b = Bridge(c, midi=self.midi, osc_send=lambda a, v: self.osc.append((a, v)))
            b.update_from_params(parse_params(default_tree()))
        return b

    def test_resync_on_layout(self):
        self.make()
        self.assertEqual(len(self.midi.sent), 23)
        self.assertIn((0, 16, round(0.8 * 127)), self.midi.sent)  # volume, group 1 enc 1

    def test_encoder_to_osc(self):
        b = self.make()
        b.on_cc(2, 17, 127)  # group 3 enc 2 -> delay feedback
        self.assertEqual(self.osc, [("/rnbo/inst/1/params/feedback/normalized", 1.0)])
        b.on_cc(2, 31, 64)  # unused encoder -> ignored
        b.on_cc(5, 16, 64)  # unused group -> ignored
        b.on_cc(0, 1, 64)  # other CC -> ignored
        self.assertEqual(len(self.osc), 1)

    def test_feedback_and_holdoff(self):
        b = self.make()
        self.midi.sent.clear()
        b.on_osc("/rnbo/inst/1/params/mix/normalized", 0.5)
        self.assertEqual(self.midi.sent, [(2, 18, 64)])
        b.on_cc(2, 18, 10)
        self.midi.sent.clear()
        b.on_osc("/rnbo/inst/1/params/mix/normalized", 10 / 127)  # echo right after a move
        self.assertEqual(self.midi.sent, [])
        b.on_osc("/rnbo/inst/1/params/mix", 0.5)  # raw value address is ignored
        self.assertEqual(self.midi.sent, [])

    def test_14bit(self):
        b = self.make(resolution="14bit")
        self.midi.sent.clear()
        b.on_cc(0, 16, 64)  # MSB alone does nothing
        self.assertEqual(self.osc, [])
        b.on_cc(0, 48, 0)  # LSB completes it
        self.assertEqual(self.osc[0][0], "/rnbo/inst/0/params/volume/normalized")
        self.assertAlmostEqual(self.osc[0][1], (64 << 7) / 16383)
        time.sleep(0.3)
        b.on_osc("/rnbo/inst/0/params/volume/normalized", 1.0)
        self.assertEqual(self.midi.sent, [(0, 16, 127), (0, 48, 127)])

    def test_polled_value_change_is_fed_back(self):
        b = self.make()
        self.midi.sent.clear()
        tree = default_tree()
        tree["CONTENTS"]["1"]["CONTENTS"]["params"]["CONTENTS"]["mix"]["CONTENTS"]["normalized"]["VALUE"] = [1.0]
        changed = b.update_from_params(parse_params(tree))
        self.assertFalse(changed)
        self.assertEqual(self.midi.sent, [(2, 18, 127)])

    def test_osc_over_udp(self):
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import BlockingOSCUDPServer
        from pythonosc.udp_client import SimpleUDPClient
        b = self.make()
        self.midi.sent.clear()
        disp = Dispatcher()
        disp.set_default_handler(b.on_osc)
        srv = BlockingOSCUDPServer(("127.0.0.1", 0), disp)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        SimpleUDPClient("127.0.0.1", srv.server_address[1]).send_message(
            "/rnbo/inst/0/params/env/attack/normalized", 0.25)
        for _ in range(50):
            if self.midi.sent:
                break
            time.sleep(0.02)
        srv.shutdown()
        srv.server_close()
        attack = (0, 16 + 3)  # volume, cutoff, resonance, env/attack
        self.assertEqual(self.midi.sent, [(attack[0], attack[1], 32)])


class RunLoopTest(unittest.TestCase):
    """Runs cmd_run for real (HTTP + UDP sockets) with only the ALSA layer faked."""

    def test_run_loop(self):
        import socket

        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import BlockingOSCUDPServer
        from pythonosc.udp_client import SimpleUDPClient

        import ec4bridge

        runner = MockRunner()
        got = []
        disp = Dispatcher()
        disp.set_default_handler(lambda a, *v: got.append((a, v)))
        osc_in = BlockingOSCUDPServer(("127.0.0.1", 0), disp)  # the runner's OSC port
        threading.Thread(target=osc_in.serve_forever, daemon=True).start()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        listen_port = s.getsockname()[1]
        s.close()

        fake = FakeMidi()
        fake.on_cc = None
        fake.ensure_connected = lambda: False
        fake.close = lambda: None

        def fake_open(cfg, **kw):
            fake.on_cc = kw.get("on_cc")
            return fake

        orig = ec4bridge.open_midi
        ec4bridge.open_midi = fake_open
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as d:
            c = cfg(oscquery_port=runner.port, osc_port=osc_in.server_address[1], listen_port=listen_port,
                    poll_interval=0.2, layout_txt=os.path.join(d, "layout.txt"),
                    backup_syx=os.path.join(d, "none.syx"))
            t = threading.Thread(target=ec4bridge.cmd_run, args=(c, None, stop), daemon=True)
            t.start()
            try:
                for _ in range(100):
                    if fake.sent and got:
                        break
                    time.sleep(0.05)
                # listener registered with the runner, all values sent to the EC4
                self.assertIn(("/rnbo/listeners/add", (f"127.0.0.1:{listen_port}",)), got)
                self.assertEqual(len(fake.sent), 23)
                # turn an encoder -> OSC to the runner
                fake.on_cc(0, 17, 127)
                for _ in range(50):
                    if any(a.endswith("cutoff/normalized") for a, _ in got):
                        break
                    time.sleep(0.02)
                self.assertIn(("/rnbo/inst/0/params/cutoff/normalized", (1.0,)), got)
                # runner pushes a value to the listener port -> CC to the EC4
                fake.sent.clear()
                time.sleep(0.3)
                SimpleUDPClient("127.0.0.1", listen_port).send_message("/rnbo/inst/1/params/time/normalized", 0.0)
                for _ in range(50):
                    if fake.sent:
                        break
                    time.sleep(0.02)
                self.assertEqual(fake.sent, [(2, 16, 0)])
                # load a different patcher -> layout rebuilt and written
                runner.tree["CONTENTS"].pop("1")
                for _ in range(50):
                    with open(c["layout_txt"]) as f:
                        if "Dely" not in f.read():
                            break
                    time.sleep(0.05)
                with open(c["layout_txt"]) as f:
                    self.assertNotIn("Dely", f.read())
            finally:
                stop.set()
                t.join(timeout=5)
                ec4bridge.open_midi = orig
                osc_in.shutdown()
                osc_in.server_close()
                runner.close()
        self.assertFalse(t.is_alive())
        self.assertIn(("/rnbo/listeners/del", (f"127.0.0.1:{listen_port}",)), got)


class CliTests(unittest.TestCase):
    def test_list_and_make_syx(self):
        runner = MockRunner()
        try:
            with tempfile.TemporaryDirectory() as d:
                backup = os.path.join(d, "ec4-backup.syx")
                with open(backup, "wb") as f:
                    f.write(sx.build_dump(synthetic_dump()))
                conf = os.path.join(d, "config.json")
                with open(conf, "w") as f:
                    json.dump({"oscquery_port": runner.port, "ec4_setup": 16}, f)
                r = subprocess.run([sys.executable, os.path.join(ROOT, "ec4bridge.py"), "-c", conf, "list"],
                                   capture_output=True, text=True, timeout=20)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("Group  1 [Pol1]", r.stdout)
                self.assertIn("env/attack", r.stdout)
                r = subprocess.run([sys.executable, os.path.join(ROOT, "ec4bridge.py"), "-c", conf, "make-syx"],
                                   capture_output=True, text=True, timeout=20)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                with open(os.path.join(d, "ec4-layout.syx"), "rb") as f:
                    out = sx.parse_dump(f.read())
                names = sx.read_names(out, 15)
                self.assertEqual(names["groups"][:3], ["Pol1", "Pol2", "Dely"])
                self.assertEqual(names["encoders"][2][:3], ["Time", "Fedb", "Mix "])
        finally:
            runner.close()

    def test_list_without_runner(self):
        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, "config.json")
            with open(conf, "w") as f:
                json.dump({"oscquery_port": 1}, f)
            r = subprocess.run([sys.executable, os.path.join(ROOT, "ec4bridge.py"), "-c", conf, "list"],
                               capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 1)
            self.assertIn("Could not reach", r.stdout)


if __name__ == "__main__":
    unittest.main()
