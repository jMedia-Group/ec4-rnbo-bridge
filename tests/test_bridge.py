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
from layout import abbreviate, build_layout, format_table  # noqa: E402
from mock_runner import MockRunner, default_tree  # noqa: E402
from rnbo import parse_params  # noqa: E402


def synthetic_dump(seed=7):
    mem = bytearray((i * seed) & 0xFF for i in range(sx.MEMORY_SIZE))
    return sx.Dump(mem, 2, 0)


class FakeMidi:
    connected = True

    def __init__(self):
        self.sent = []
        self.sysex = []

    def send_ccs(self, msgs, pause=0):
        self.sent += msgs

    def send_sysex(self, data):
        self.sysex.append(bytes(data))


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
        allowed |= {sx.ADDR_KEY2 + (15 * 16 + g) * 32 + e for g in range(16) for e in range(16)}
        self.assertTrue(set(changed) <= allowed)
        # and the result is still a valid dump
        sx.parse_dump(sx.build_dump(d))

    def test_display_setting(self):
        d = synthetic_dump()
        names = [["Abcd"] * 16 for _ in range(16)]
        b = sx._group_base(0, 0)
        for disp, code in [("127", 1), ("+-63", 4), ("onoff", 7), ("off", 0), (None, 2)]:
            sx.apply_layout(d, 0, "R", [], names, cc_base=16, resolution="7bit", mode="Acc1", display=disp)
            self.assertEqual(d.memory[b + 80] & 0xF, code, disp)
        sx.apply_layout(d, 0, "R", [], names, cc_base=16, resolution="14bit", mode="Acc3", display="9999")
        self.assertEqual(d.memory[b + 80] & 0xF, 8)
        with self.assertRaises(ValueError):
            sx.apply_layout(d, 0, "R", [], names, cc_base=16, resolution="7bit", mode="Acc1", display="1000")
        with self.assertRaises(ValueError):
            sx.apply_layout(d, 0, "R", [], names, cc_base=16, resolution="14bit", mode="Acc1", display="127")

    def test_live_names_placeholders(self):
        d = synthetic_dump()
        names = [[None] * 16 for _ in range(16)]
        names[0][0] = "cutf"
        sx.apply_layout(d, 15, "RNBO", ["syn"], names, cc_base=16, resolution="7bit",
                        mode="Acc1", live_names=True)
        n = sx.read_names(d, 15)
        self.assertEqual(set(sum(n["encoders"], [])), {"----"})  # every encoder writable live
        self.assertEqual(n["groups"][0], "syn ")  # group names still stored
        for g in (0, 7):
            b = sx._group_base(15, g)
            self.assertEqual({d.memory[b + 80 + e] & 0xF for e in range(16)}, {2})  # display on everywhere
        sx.apply_layout(d, 15, "RNBO", ["syn"], names, cc_base=16, resolution="7bit", mode="Acc1")
        self.assertEqual(sx.read_names(d, 15)["encoders"][0][:2], ["cutf", "    "])

    def test_push_buttons_jump_to_groups(self):
        d = synthetic_dump()
        names = [[None] * 16 for _ in range(16)]
        sx.apply_layout(d, 15, "R", [], names, cc_base=16, resolution="7bit", mode="Acc1", push_jumps=True)
        for g in (0, 9):
            b = sx._group_base(15, g)
            self.assertEqual([d.memory[b + 112 + e] for e in range(16)], [0x60 | e for e in range(16)])
        sx.apply_layout(d, 15, "R", [], names, cc_base=16, resolution="7bit", mode="Acc1")
        b = sx._group_base(15, 9)
        self.assertEqual({d.memory[b + 112 + e] for e in range(16)}, {0x09})  # off

    def test_push_star_switched_off(self):
        d = synthetic_dump()
        k2 = sx.ADDR_KEY2
        for i in range(16 * 16 * 32):  # every push button: star on, lower value 0x6c
            d.memory[k2 + i] = 0xEC
        before = bytes(d.memory)
        names = [[None] * 16 for _ in range(16)]
        sx.apply_layout(d, 15, "R", [], names, cc_base=16, resolution="7bit", mode="Acc1", push_jumps=True)
        for g in (0, 15):
            a = k2 + (15 * 16 + g) * 32
            self.assertEqual(set(d.memory[a:a + 16]), {0x6C})  # star off, lower value kept
            self.assertEqual(set(d.memory[a + 16:a + 32]), {0xEC})  # link/upper bytes untouched
        a = k2 + (3 * 16) * 32
        self.assertEqual(d.memory[a:a + 512], before[a:a + 512])  # other setups untouched

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
        self.assertEqual(lay.group_names[:4], ["pol1", "pol2", "Dely", ""])
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
        self.assertEqual(lay.group_names[0], "revr")
        self.assertEqual([s.short for s in lay.slots], ["size", "decy"])
        lay = build_layout(parse_params(tree), cfg())
        self.assertEqual(lay.group_names[0], "jRev")  # without the option

    def test_group_title_style(self):
        lay = build_layout(self.params, cfg(group_title_style="number", group_names={"0": "Syn"}))
        self.assertEqual(lay.group_names[:3], ["G01", "G02", "G03"])
        self.assertEqual(lay.group_names[15], "G16")  # unused groups too
        lay = build_layout(self.params, cfg(group_title_style="blank"))
        self.assertEqual(set(lay.group_names), {""})
        lay = build_layout(self.params, cfg())
        self.assertEqual(lay.group_names[:3], ["pol1", "pol2", "Dely"])
        # titles are stored in the dump and survive a round trip
        d = synthetic_dump()
        lay = build_layout(self.params, cfg(group_title_style="number"))
        sx.apply_layout(d, 15, "RNBO", lay.group_names, lay.encoder_names(), cc_base=16,
                        resolution="7bit", mode="Acc1", live_names=True)
        names = sx.read_names(sx.parse_dump(sx.build_dump(d)), 15)
        self.assertEqual(names["groups"][:2] + names["groups"][-1:], ["G01 ", "G02 ", "G16 "])

    def test_hide_devices(self):
        lay = build_layout(self.params, cfg(hide_devices=["delay"]))  # alias "Delay", any case
        self.assertEqual({s.param.inst for s in lay.slots}, {0})
        self.assertEqual(lay.hidden, ["1 Delay"])
        lay = build_layout(self.params, cfg(hide_devices=["0"]))  # by instance number
        self.assertEqual({s.param.inst for s in lay.slots}, {1})
        self.assertEqual(lay.slots[0].group, 0)  # remaining device moves up to group 1
        self.assertEqual(lay.group_names[0], "Dely")
        lay = build_layout(self.params, cfg(hide_devices=["^poly"]))
        self.assertEqual(lay.hidden, ["0 polysynth"])
        lay = build_layout(self.params, cfg(hide_devices=["synth$", "1"]))
        self.assertEqual(lay.slots, [])
        self.assertIn("No parameters left after hiding devices", format_table(lay))
        self.assertIn("Hidden devices (hide_devices): 0 polysynth, 1 Delay", format_table(lay))

    def test_hide_devices_with_prefix(self):
        from mock_runner import make_instance
        tree = {"CONTENTS": {"0": make_instance(0, "j.reverb", [("size", 0, 0.5)]),
                             "1": make_instance(1, "j.mix", [("gain", 0, 0.5)])}}
        lay = build_layout(parse_params(tree), cfg(strip_prefixes=["j."], hide_devices=["^reverb$"]))
        self.assertEqual([s.param.inst for s in lay.slots], [1])

    def test_overflow(self):
        many = parse_params(default_tree()) * 13  # 299 params
        lay = build_layout(many, cfg(new_group_per_instance=False))
        self.assertEqual(len(lay.slots), 256)
        self.assertEqual(len(lay.skipped), len(many) - 256)

    def test_abbreviate(self):
        # default keeps the case from RNBO
        self.assertEqual(abbreviate("cutoff"), "cutf")
        self.assertEqual(abbreviate("Cutoff"), "Cutf")
        self.assertEqual(abbreviate("resonance"), "resn")
        self.assertEqual(abbreviate("osc2Level"), "os2L")
        self.assertEqual(abbreviate("LFO"), "LFO")
        self.assertEqual(abbreviate("mix"), "mix")
        self.assertEqual(abbreviate("Filter Env Amount"), "FiEA")
        self.assertEqual(abbreviate("filter env amount"), "fiEA")
        # names with spaces: first word's letters + capital initial of the next word(s)
        self.assertEqual(abbreviate("foo bar"), "fooB")
        self.assertEqual(abbreviate("delay time"), "delT")
        self.assertEqual(abbreviate("Delay Time"), "DelT")
        self.assertEqual(abbreviate("osc 2 level"), "os2L")
        self.assertEqual(abbreviate("a b c d e"), "aBCD")
        self.assertEqual(abbreviate("  mix   level "), "mixL")
        self.assertEqual(abbreviate("foo bar", case="upper"), "FOOB")
        self.assertEqual(abbreviate("foo bar", case="title"), "FooB")
        # camelCase / under_scores follow the same rule
        self.assertEqual(abbreviate("fooBar"), "fooB")
        self.assertEqual(abbreviate("filterEnv"), "filE")
        self.assertEqual(abbreviate("filterEnvAmount"), "fiEA")
        self.assertEqual(abbreviate("lfo_rate"), "lfoR")
        self.assertEqual(abbreviate("LFORate"), "LFOR")
        self.assertEqual(abbreviate("j.reverb"), "jRev")
        self.assertEqual(abbreviate("j.reverb", 3), "jRe")
        self.assertEqual(abbreviate("x y"), "xY")
        self.assertEqual(abbreviate("filterEnv amount"), "filA")  # spaces win; camel word kept whole
        # other styles
        self.assertEqual(abbreviate("cutoff", case="title"), "Cutf")
        self.assertEqual(abbreviate("osc2Level", case="title"), "Os2L")
        self.assertEqual(abbreviate("cutoff", case="upper"), "CUTF")
        self.assertEqual(abbreviate("LFO", case="lower"), "lfo")

    def test_name_case_setting(self):
        lay = build_layout(self.params, cfg(name_case="upper", names={"0/cutoff": "Filt"}))
        self.assertEqual(lay.group_names[:3], ["POL1", "POL2", "DELY"])
        self.assertEqual(next(s for s in lay.slots if s.param.pid == "cutoff").short, "Filt")  # typed as-is
        self.assertEqual(next(s for s in lay.slots if s.param.pid == "mix").short, "MIX")


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
        self.assertEqual(len(self.midi.sent), 256)  # 23 parameters + 233 unused encoders
        self.assertIn((0, 16, round(0.8 * 127)), self.midi.sent)  # volume, group 1 enc 1
        self.assertIn((1, 20, 0), self.midi.sent)  # group 2 encoder 5: unused -> 0
        self.assertIn((15, 31, 0), self.midi.sent)  # group 16 encoder 16: unused -> 0
        self.assertNotIn((0, 16, 0), self.midi.sent)  # used encoders are not zeroed

    def test_zero_unused_off(self):
        self.make(zero_unused=False)
        self.assertEqual(len(self.midi.sent), 23)

    def test_new_graph_zeroes_leftovers(self):
        b = self.make()
        self.midi.sent.clear()
        tree = default_tree()
        tree["CONTENTS"].pop("0")  # new graph: only the 3-parameter delay
        b.update_from_params(parse_params(tree))
        self.assertIn((0, 19, 0), self.midi.sent)  # group 1 encoder 4 had a synth parameter -> 0
        self.assertIn((1, 16, 0), self.midi.sent)  # old second synth page -> 0
        self.assertIn((0, 16, 16), self.midi.sent)  # delay time moved here: 250 of 0..2000 -> 16

    def test_zero_unused_14bit(self):
        self.make(resolution="14bit")
        self.assertIn((5, 16, 0), self.midi.sent)
        self.assertIn((5, 48, 0), self.midi.sent)  # MSB and LSB

    def test_encoder_to_osc(self):
        b = self.make()
        b.on_cc(2, 17, 127)  # group 3 enc 2 -> delay feedback
        self.assertEqual(self.osc, [("/rnbo/inst/1/params/feedback/normalized", 1.0)])
        b.on_cc(2, 31, 64)  # unused encoder -> ignored
        b.on_cc(5, 16, 64)  # unused group -> ignored
        b.on_cc(0, 1, 64)  # other CC -> ignored
        self.assertEqual(len(self.osc), 1)

    def test_late_report_does_not_snap_knob_back(self):
        b = self.make()  # default hold-off: 1 s
        self.midi.sent.clear()
        b.on_cc(2, 18, 100)  # turn 'mix' up
        time.sleep(0.4)  # a slow runner reports an older value 400 ms later
        b.on_osc("/rnbo/inst/1/params/mix/normalized", 20 / 127)
        self.assertEqual(self.midi.sent, [])  # the knob is not pulled back

    def test_poll_never_pulls_back_a_turned_knob(self):
        b = self.make(poll_interval=0.0)
        tree = default_tree()
        node = tree["CONTENTS"]["1"]["CONTENTS"]["params"]["CONTENTS"]["mix"]["CONTENTS"]["normalized"]
        node["VALUE"] = [0.3]
        b.update_from_params(parse_params(tree))
        b.on_cc(2, 18, 120)  # user turns mix up; snapshots still say 0.3 for a while
        self.midi.sent.clear()
        b.update_from_params(parse_params(tree))
        b.update_from_params(parse_params(tree))
        self.assertEqual(self.midi.sent, [])

    def test_echo_of_feedback_is_not_a_turn(self):
        b = self.make()
        time.sleep(0.01)
        b.on_osc("/rnbo/inst/1/params/mix/normalized", 0.5)  # changed in the web UI
        self.assertEqual(self.midi.sent[-1], (2, 18, 64))
        b.on_cc(2, 18, 64)  # EC4 echoes it straight back (MIDI thru/merge)
        self.assertEqual(self.osc, [])  # not sent to RNBO as a turn
        b.on_cc(2, 18, 65)  # a real turn
        self.assertEqual(self.osc, [("/rnbo/inst/1/params/mix/normalized", 65 / 127)])

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

    def test_warns_when_ec4_sends_14bit_but_config_is_7bit(self):
        b = self.make()
        with self.assertLogs("ec4bridge", level="WARNING") as logs:
            b.on_cc(0, 48, 33)  # fine half of a 14-bit pair
            b.on_cc(0, 48, 34)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("14-bit", logs.output[0])

    def test_14bit(self):
        b = self.make(resolution="14bit", feedback_holdoff_ms=250)
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
        self.assertEqual(self.midi.sent, [])  # seen once: could be a snapshot mid-change
        b.update_from_params(parse_params(tree))  # same value in the next poll -> trust it
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
                self.assertEqual(len(fake.sent), 256)
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
                for _ in range(50):  # the last UDP message may still be in flight
                    if any(a == "/rnbo/listeners/del" for a, _ in got):
                        break
                    time.sleep(0.02)
                osc_in.shutdown()
                osc_in.server_close()
                runner.close()
        self.assertFalse(t.is_alive())
        self.assertIn(("/rnbo/listeners/del", (f"127.0.0.1:{listen_port}",)), got)


import ec4_remote as rm  # noqa: E402


def key_press(shift_key=None, user_key=None, pressed=True):
    body = []
    if shift_key is not None:
        body += [0x4E, 0x2A, 0x10 + shift_key]
    if user_key is not None:
        body += [0x4E, 0x26, 0x11 + user_key]
    body += [0x4E, 0x2E, 0x11 if pressed else 0x10]
    return bytes([*rm.HEADER, *body, 0xF7])


def report(setup=None, group=None):
    body = []
    if setup is not None:
        body += [0x4E, 0x28, 0x10 + setup]
    if group is not None:
        body += [0x4E, 0x24, 0x10 + group]
    return bytes([*rm.HEADER, *body, 0xF7])


class RemoteProtocolTests(unittest.TestCase):
    """Byte layout must match DrivenByMoss' EC4Display/EC4ControlSurface."""

    def test_names_page_bytes(self):
        m = rm.names_page(["Ab", None] + [None] * 14)
        self.assertEqual(m[:13], bytes([0xF0, 0, 0, 0, 0x4E, 0x2C, 0x1B, 0x4E, 0x22, 0x10, 0x4A, 0x20, 0x10]))
        self.assertEqual(m[13:19], bytes([0x4D, 0x24, 0x11, 0x4D, 0x26, 0x12]))  # 'A' 0x41, 'b' 0x62
        self.assertEqual(len(m), 13 + 64 * 3 + 1)
        self.assertEqual(m[-1], 0xF7)

    def test_overlay_and_requests(self):
        self.assertEqual(rm.overlay_show(True)[-4:], bytes([0x4E, 0x22, 0x14, 0xF7]))
        self.assertEqual(rm.overlay_show(False)[-4:], bytes([0x4E, 0x22, 0x15, 0xF7]))
        self.assertEqual(rm.overlay_text(["x"])[7:10], bytes([0x4E, 0x22, 0x13]))
        self.assertEqual(len(rm.overlay_text([])), 13 + 80 * 3 + 1)
        self.assertEqual(rm.REQUEST_INFO, bytes([0xF0, 0, 0, 0, 0x4E, 0x20, 0x10, 0xF7]))

    def test_parse_key_events(self):
        self.assertEqual(rm.parse_report(key_press(shift_key=15)), {"shift_key": 15, "pressed": True})
        self.assertEqual(rm.parse_report(key_press(shift_key=0, pressed=False)), {"shift_key": 0, "pressed": False})
        self.assertEqual(rm.parse_report(key_press(user_key=1)), {"user_key": 1, "pressed": True})

    def test_parse_report(self):
        self.assertEqual(rm.parse_report(report(15, 0)), {"setup": 15, "group": 0})
        self.assertEqual(rm.parse_report(report(group=3)), {"group": 3})
        self.assertIsNone(rm.parse_report(bytes([0xF0, 0, 0, 0, 0x41, 0xF7])))
        self.assertIsNone(rm.parse_report(rm.REQUEST_INFO))


class LiveDisplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def make(self, **kw):
        self.midi = FakeMidi()
        self.osc = []
        d = self.tmp.name
        c = cfg(layout_txt=os.path.join(d, "l.txt"), backup_syx=os.path.join(d, "none.syx"),
                notify_seconds=0.1, **kw)
        b = Bridge(c, midi=self.midi, osc_send=lambda a, v: self.osc.append((a, v)))
        b.update_from_params(parse_params(default_tree()))
        return b

    def tearDown(self):
        self.tmp.cleanup()

    def names_written(self):
        return [m for m in self.midi.sysex if m[7:10] == bytes([0x4E, 0x22, 0x10])]

    @staticmethod
    def text(m):
        return "".join(chr(((m[i + 1] & 0xF) << 4) | (m[i + 2] & 0xF)) for i in range(13, len(m) - 1, 3))

    def test_names_written_on_start_and_group_change(self):
        b = self.make()
        pages = self.names_written()
        self.assertEqual(len(pages), 1)
        self.assertTrue(self.text(pages[0]).startswith("volmCutfresnatck"))
        b.on_sysex(report(15, 2))  # user selects group 3 on the RNBO setup
        self.assertTrue(self.text(self.names_written()[-1]).startswith("timefedbmix "))
        b.on_sysex(report(15, 9))  # empty group -> blank names
        self.assertEqual(self.text(self.names_written()[-1]), " " * 64)

    def test_other_setup_is_left_alone(self):
        b = self.make()
        b.on_sysex(report(3, 0))  # user switches the EC4 to their own setup 4
        self.midi.sent.clear()
        self.midi.sysex.clear()
        b.on_cc(0, 16, 100)  # that setup's encoder must not move RNBO
        self.assertEqual(self.osc, [])
        b.on_osc("/rnbo/inst/1/params/mix/normalized", 0.5)  # and no feedback goes out
        b.update_from_params(parse_params(default_tree()))
        self.assertEqual(self.midi.sent, [])
        self.assertEqual(self.names_written(), [])
        b.on_sysex(report(15, 0))  # back on the RNBO setup -> values + names resent
        self.assertEqual(len(self.midi.sent), 256)
        self.assertEqual(len(self.names_written()), 1)
        b.on_cc(0, 16, 127)
        self.assertEqual(self.osc[-1], ("/rnbo/inst/0/params/volume/normalized", 1.0))

    def test_graph_change_notifies_and_renames(self):
        b = self.make()
        self.midi.sysex.clear()
        tree = default_tree()
        tree["CONTENTS"].pop("0")  # load a different graph
        self.assertTrue(b.update_from_params(parse_params(tree)))
        overlay = [m for m in self.midi.sysex if m[7:10] == bytes([0x4E, 0x22, 0x13])]
        self.assertEqual(len(overlay), 1)
        self.assertIn("RNBO graph loaded", self.text(overlay[0]))
        self.assertIn(" 1 Delay", self.text(overlay[0]))
        self.assertIn(rm.overlay_show(True), self.midi.sysex)
        self.assertTrue(self.text(self.names_written()[-1]).startswith("timefedbmix "))
        time.sleep(0.3)
        self.assertEqual(self.midi.sysex[-1], rm.overlay_show(False))  # overlay hidden again

    def test_group_change_popup(self):
        b = self.make()
        b.on_sysex(report(15, 0))
        self.midi.sysex.clear()
        b.on_sysex(report(15, 1))  # switch to group 2 = second page of the synth
        overlay = [self.text(m) for m in self.midi.sysex if m[7:10] == bytes([0x4E, 0x22, 0x13])]
        self.assertEqual(len(overlay), 1)
        self.assertTrue(overlay[0].startswith("Group 2"))
        self.assertIn("polysynth", overlay[0])
        self.assertIn("page 2 of 2", overlay[0])
        self.midi.sysex.clear()
        b.on_sysex(report(15, 2))
        self.assertIn("Delay", [self.text(m) for m in self.midi.sysex if m[7:10] == bytes([0x4E, 0x22, 0x13])][0])

    def test_group_popup_off(self):
        b = self.make(notify_group_change=False)
        b.on_sysex(report(15, 0))
        self.midi.sysex.clear()
        b.on_sysex(report(15, 2))
        self.assertFalse(any(m[7:10] == bytes([0x4E, 0x22, 0x13]) for m in self.midi.sysex))
        self.assertTrue(self.names_written())

    def test_quiet_during_dump(self):
        import ec4bridge
        pause = os.path.join(self.tmp.name, "ec4bridge.pause")
        b = self.make(pause_file=pause)
        self.midi.sent.clear()
        self.midi.sysex.clear()
        with ec4bridge.pause_bridge(b.cfg, settle=0):
            self.assertTrue(os.path.exists(pause))
            b.on_osc("/rnbo/inst/1/params/mix/normalized", 0.9)  # a moving parameter
            b.resync()
            b.write_names()
            b.notify(["x"])
            b.on_sysex(report(15, 2))  # even a group change must not trigger output
            b.on_cc(0, 16, 64)
        self.assertEqual(self.midi.sent, [])
        self.assertEqual(self.midi.sysex, [])
        self.assertEqual(self.osc, [])
        self.assertFalse(os.path.exists(pause))
        b.on_osc("/rnbo/inst/1/params/mix/normalized", 0.5)  # back to normal afterwards
        self.assertEqual(self.midi.sent, [(2, 18, 64)])

    def test_stale_pause_file_ignored(self):
        pause = os.path.join(self.tmp.name, "old.pause")
        open(pause, "w").close()
        old = time.time() - 3600
        os.utime(pause, (old, old))
        b = self.make(pause_file=pause)
        self.assertFalse(b.paused())

    def overlays(self):
        """Overlay screen contents after each write (writes may start mid-screen)."""
        screen = [" "] * 80
        out = []
        for m in self.midi.sysex:
            if m[7:10] != bytes([0x4E, 0x22, 0x13]):
                continue
            off = (m[11] - 0x20) * 16 + (m[12] - 0x10)
            for i, ch in enumerate(self.text(m)):
                screen[off + i] = ch
            out.append("".join(screen))
        return out

    def overlay_writes(self):
        return [m for m in self.midi.sysex if m[7:10] == bytes([0x4E, 0x22, 0x13])]

    def test_device_list_popup(self):
        b = self.make(notify_group_change=True, device_list_mode="toggle")
        b.on_sysex(report(15, 0))
        self.midi.sysex.clear()
        b.on_sysex(key_press(shift_key=15))  # SHIFT + push encoder 16
        ov = self.overlays()
        self.assertEqual(len(ov), 1)
        self.assertEqual(ov[0][:20], " 1 polysy1 2 polysy2")
        self.assertEqual(ov[0][20:40], " 3 Delay".ljust(20))
        self.assertIn(rm.overlay_show(True), self.midi.sysex)
        self.midi.sysex.clear()
        b.on_sysex(key_press(shift_key=15))  # again: only one page, so it closes
        self.assertEqual(self.overlays(), [])
        self.assertEqual(self.midi.sysex[-1], rm.overlay_show(False))
        # open, then the user pushes encoder 3: the EC4 jumps to group 3 and reports it
        b.on_sysex(key_press(shift_key=15))
        self.midi.sysex.clear()
        b.on_sysex(report(15, 2))
        self.assertIsNone(b._list_page)
        self.assertTrue(self.overlays()[0].startswith("Group 3"))
        self.assertTrue(self.text(self.names_written()[-1]).startswith("timefedbmix "))

    def test_device_list_ignores_release_other_keys_and_setups(self):
        b = self.make()
        b.on_sysex(report(15, 0))
        self.midi.sysex.clear()
        b.on_sysex(key_press(shift_key=15, pressed=False))
        b.on_sysex(key_press(shift_key=3))
        b.on_sysex(key_press(user_key=1))
        self.assertEqual(self.overlays(), [])
        b.on_sysex(report(4, 0))  # user is on one of their own setups
        b.on_sysex(key_press(shift_key=15))
        self.assertEqual(self.overlays(), [])

    def test_device_list_user_key_and_paging(self):
        from mock_runner import make_instance
        tree = {"CONTENTS": {str(i): make_instance(i, f"dev{i}", [("p", 0, 0.5)]) for i in range(10)}}
        self.midi = FakeMidi()
        b = Bridge(cfg(layout_txt=os.path.join(self.tmp.name, "l.txt"), backup_syx="none",
                       device_list_key="user1", device_list_mode="toggle"), midi=self.midi)
        b.update_from_params(parse_params(tree))
        b.on_sysex(report(15, 0))
        self.midi.sysex.clear()
        b.on_sysex(key_press(shift_key=15))  # not the configured key
        self.assertEqual(self.overlays(), [])
        b.on_sysex(key_press(user_key=1))
        b.on_sysex(key_press(user_key=1))
        ov = self.overlays()
        self.assertEqual(len(ov), 2)
        self.assertTrue(ov[0].startswith(" 1 dev0    2 dev1"))
        self.assertTrue(ov[1].startswith(" 9 dev8   10 dev9"))
        b.on_sysex(key_press(user_key=1))  # past the last page -> closed
        self.assertIsNone(b._list_page)
        b._hide_overlay()

    def test_device_list_momentary(self):
        b = self.make()  # default: momentary
        b.on_sysex(report(15, 0))
        self.midi.sysex.clear()
        b.on_sysex(key_press(shift_key=15))  # hold SHIFT + push 16
        self.assertEqual(self.overlays()[0][:20], " 1 polysy1 2 polysy2")
        self.assertIn(rm.overlay_show(True), self.midi.sysex)
        b.on_sysex(key_press(shift_key=15, pressed=False))  # let go
        self.assertEqual(self.midi.sysex[-1], rm.overlay_show(False))
        self.assertIsNone(b._list_page)
        # letting go of SHIFT first also closes it
        b.on_sysex(key_press(shift_key=15))
        self.midi.sysex.clear()
        b.on_sysex(bytes([*rm.HEADER, 0x4E, 0x26, 0x11, 0x4E, 0x2E, 0x10, 0xF7]))
        self.assertEqual(self.midi.sysex, [rm.overlay_show(False)])
        # a release with no list open sends nothing
        self.midi.sysex.clear()
        b.on_sysex(key_press(shift_key=15, pressed=False))
        self.assertEqual(self.midi.sysex, [])

    def test_device_list_momentary_pages_flip(self):
        from mock_runner import make_instance
        tree = {"CONTENTS": {str(i): make_instance(i, f"dev{i}", [("p", 0, 0.5)]) for i in range(10)}}
        self.midi = FakeMidi()
        b = Bridge(cfg(layout_txt=os.path.join(self.tmp.name, "l.txt"), backup_syx="none",
                       device_list_page_seconds=0.05), midi=self.midi)
        b.update_from_params(parse_params(tree))
        b.on_sysex(report(15, 0))
        self.midi.sysex.clear()
        b.on_sysex(key_press(shift_key=15))
        time.sleep(0.08)
        ov = self.overlays()
        self.assertTrue(ov[0].startswith(" 1 dev0"))
        self.assertTrue(ov[1].startswith(" 9 dev8"))
        b.on_sysex(key_press(shift_key=15, pressed=False))
        n = len(self.overlays())
        time.sleep(0.12)
        self.assertEqual(len(self.overlays()), n)  # stopped flipping after release

    def test_value_popup_on_turn(self):
        b = self.make()
        b.on_sysex(report(15, 0))
        self.midi.sysex.clear()
        b.on_cc(0, 17, 127)  # group 1 encoder 2 = cutoff, turned to max
        ov = self.overlays()
        self.assertEqual(len(ov), 1)
        rows = [ov[0][i:i + 20].rstrip() for i in range(0, 80, 20)]
        self.assertEqual(rows, ["Cutoff", "20000", "############### 100%", "polysynth"])
        self.assertIn(rm.overlay_show(True), self.midi.sysex)
        # the runner reports the exact value back -> pop-up shows it
        time.sleep(0.1)
        b.on_osc("/rnbo/inst/0/params/cutoff", 1234.4)
        rows = [self.overlays()[-1][i:i + 20].rstrip() for i in range(0, 80, 20)]
        self.assertEqual(rows, ["Cutoff", "1234", "############### 100%", "polysynth"])
        # the follow-up only rewrote rows 2-3 (40 characters starting at position 20)
        last = self.overlay_writes()[-1]
        self.assertEqual(last[10:13], bytes([0x4A, 0x21, 0x14]))
        self.assertEqual(len(last), 13 + 40 * 3 + 1)

    def test_value_popup_enum_and_throttle(self):
        b = self.make()
        b.on_sysex(report(15, 0))
        self.midi.sysex.clear()
        b.on_cc(0, 21, 127)  # wave (enum) -> "square"
        b.on_cc(0, 21, 0)    # immediately again: throttled, shown a moment later
        self.assertEqual(len(self.overlays()), 1)
        self.assertEqual(self.overlays()[0][20:40].rstrip(), "square")
        time.sleep(0.15)
        self.assertEqual(len(self.overlays()), 2)
        self.assertEqual(self.overlays()[1][20:40].rstrip(), "sine")
        self.assertEqual(self.overlays()[1][:20].rstrip(), "wave")  # name still on screen

    def test_value_popup_not_for_runner_changes_or_when_off(self):
        b = self.make()
        b.on_sysex(report(15, 0))
        self.midi.sysex.clear()
        b.on_osc("/rnbo/inst/0/params/cutoff", 500.0)  # value changed elsewhere, nobody turning
        self.assertEqual(self.overlays(), [])
        self.assertEqual(b.raw["0/cutoff"], 500.0)
        b2 = self.make(value_popup=False)
        self.midi.sysex.clear()
        b2.on_cc(0, 17, 64)
        self.assertEqual(self.overlays(), [])

    def test_value_popup_waits_for_device_list(self):
        b = self.make()
        b.on_sysex(report(15, 0))
        b.on_sysex(key_press(shift_key=15))  # holding the device list
        self.midi.sysex.clear()
        b.on_cc(0, 17, 64)
        self.assertEqual(self.overlays(), [])
        b._hide_overlay()

    def test_value_popup_units(self):
        from mock_runner import make_instance
        tree = {"CONTENTS": {"0": make_instance(0, "synth", [
            ("cutoff", 0, 1000.0, 20.0, 20000.0, "Cutoff", None, 0, None, "Hz"),
            ("attack", 1, 10.0, 0.0, 1000.0, "", None, 0, None, None, '{"unit": "ms"}'),
            ("drive", 2, 0.5),
            ("wave", 3, 1, 0, 1, "", None, 3, ["sine", "saw", "square"], "Hz"),
        ])}}
        params = parse_params(tree)
        self.assertEqual([p.unit for p in params], ["Hz", "ms", "", "Hz"])
        self.midi = FakeMidi()
        b = Bridge(cfg(layout_txt=os.path.join(self.tmp.name, "l.txt"), backup_syx="none",
                       units={"drive": "dB"}), midi=self.midi)
        b.update_from_params(params)
        lines = lambda i: b.value_popup_lines(b.layout.slots[i], None)
        self.assertEqual(lines(0)[1], "1000 Hz")
        self.assertEqual(lines(1)[1], "10 ms")
        self.assertEqual(lines(2)[1], "0.500 dB")  # from config
        self.assertEqual(lines(3)[1], "saw")  # enums never get a unit

    def test_format_value(self):
        f = Bridge.format_value
        self.assertEqual([f(3.0), f(0.5), f(12.345), f(123.45), f(4321.6), f("saw"), f(None)],
                         ["3", "0.500", "12.35", "123.5", "4322", "saw", ""])

    def test_live_names_off(self):
        self.make(live_names=False, notify_graph_change=False)
        self.assertEqual(self.midi.sysex, [])


class FakeEC4(FakeMidi):
    """Answers state requests like an EC4 on setup 3, group 1."""

    def __init__(self, on_sysex):
        super().__init__()
        self.on_sysex = on_sysex
        self.connected = False

    def ensure_connected(self):
        self.connected = True
        return True

    def close(self):
        pass

    def send_sysex(self, data):
        super().send_sysex(data)
        if data == rm.REQUEST_INFO:
            self.on_sysex(report(2, 0))


class TestDisplayCommandTests(unittest.TestCase):
    def test_display_check(self):
        import contextlib
        import io

        import ec4bridge
        holder = {}

        def fake_open(c, **kw):
            holder["ec4"] = FakeEC4(kw["on_sysex"])
            return holder["ec4"]

        orig_open, orig_sleep = ec4bridge.open_midi, ec4bridge.time.sleep
        ec4bridge.open_midi = fake_open
        ec4bridge.time.sleep = lambda s: None
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = ec4bridge.cmd_test_display(cfg(), argparse_ns())
        finally:
            ec4bridge.open_midi, ec4bridge.time.sleep = orig_open, orig_sleep
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("EC4 reports setup 3, group 1", text)
        self.assertIn("writes names only while it's on setup 16", text)
        sent = holder["ec4"].sysex
        self.assertIn(rm.overlay_show(True), sent)
        self.assertIn(rm.overlay_show(False), sent)
        pages = [m for m in sent if m[7:10] == bytes([0x4E, 0x22, 0x10])]
        self.assertEqual(len(pages), 2)  # test names, then the real ones back
        self.assertTrue(LiveDisplayTests.text(pages[0]).startswith("T01 T02 T03 T04 "))
        self.assertFalse(any(m[7:9] == bytes([0x4E, 0x28]) for m in sent))  # never asks to switch setup


def argparse_ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


class OutQueueTests(unittest.TestCase):
    def test_coalesce(self):
        from outqueue import coalesce
        t1 = ("sysex", rm.overlay_text(["a"]))
        t2 = ("sysex", rm.overlay_text(["b"]))
        part = ("sysex", rm.write_text(rm.DISPLAY_OVERLAY, 20, "x" * 40))
        show, hide = ("sysex", rm.overlay_show(True)), ("sysex", rm.overlay_show(False))
        names = ("sysex", rm.names_page(["n"]))
        req = ("sysex", rm.REQUEST_INFO)
        items = [("cc", 0, 16, 1), t1, show, ("cc", 0, 16, 5), ("cc", 1, 16, 9), req, t2, part,
                 hide, show, names, req]
        out = coalesce(items)
        self.assertEqual(out, [("cc", 0, 16, 5), ("cc", 1, 16, 9), req, t2, part, show, names, req])

    def test_queue_writes_in_background(self):
        from outqueue import OutQueue
        written = []
        gate = threading.Event()

        def slow_cc(ch, cc, v):
            gate.wait(1)  # the EC4 is slow to take data
            written.append((ch, cc, v))

        q = OutQueue(slow_cc, lambda d: written.append(d))
        t0 = time.monotonic()
        for v in range(50):
            q.cc(0, 16, v)  # never blocks the caller
        self.assertLess(time.monotonic() - t0, 0.1)
        gate.set()
        self.assertTrue(q.flush(2))
        self.assertEqual(written[-1], (0, 16, 49))  # newest value always arrives
        self.assertLess(len(written), 50)  # stale ones were dropped
        q.close()


class SlowEC4:
    """Writes go to an OutQueue; the 'EC4' replies to each display message after `delay`."""

    def __init__(self, delay=0.08, reply=True):
        from outqueue import OutQueue
        self.delay, self.reply = delay, reply
        self.sent, self.sysex = [], []
        self.connected = True
        self.q = OutQueue(lambda ch, cc, v: self.sent.append((ch, cc, v)), self._write, ack_timeout=0.3)

    def _write(self, data):
        self.sysex.append((time.monotonic(), bytes(data)))
        if self.reply and data[7:9] == bytes([0x4E, 0x22]):
            threading.Timer(self.delay, self.q.ack).start()

    def send_ccs(self, msgs, pause=0):
        for m in msgs:
            self.q.cc(*m)

    def send_sysex(self, data):
        self.q.sysex(data)

    def display_text(self, d, o, t):
        self.q.set_text(d, o, t)

    def overlay_visible(self, v):
        self.q.set_visible(v)

    def invalidate_display(self, d=None, visibility=False):
        self.q.invalidate(d, visibility)


class DisplayPacingTests(unittest.TestCase):
    def test_one_message_in_flight_and_latest_state_wins(self):
        ec4 = SlowEC4(delay=0.1)
        for i in range(30):  # the bridge updates the pop-up 30 times in quick succession
            ec4.display_text(rm.DISPLAY_OVERLAY, 20, f"value {i:<14}")
        self.assertTrue(ec4.q.flush(3))
        msgs = [m for _, m in ec4.sysex]
        self.assertLessEqual(len(msgs), 3)  # not 30: intermediate states were skipped
        screen = [" "] * 80
        for m in msgs:  # replay the writes onto a screen
            i = 10
            while m[i] == 0x4A:
                pos = (m[i + 1] - 0x20) * 16 + (m[i + 2] - 0x10)
                i += 3
                while m[i] == 0x4D:
                    screen[pos] = chr(((m[i + 1] & 0xF) << 4) | (m[i + 2] & 0xF))
                    pos += 1
                    i += 3
        self.assertEqual("".join(screen[20:40]), "value 29".ljust(20))
        ec4.q.close()

    def test_only_changed_characters_are_sent(self):
        ec4 = SlowEC4(delay=0.0)
        ec4.display_text(rm.DISPLAY_OVERLAY, 0, rm.overlay_rows(["Cutoff", "1000 Hz", "#####", "synth"]))
        ec4.q.flush(2)
        ec4.display_text(rm.DISPLAY_OVERLAY, 0, rm.overlay_rows(["Cutoff", "1010 Hz", "#####", "synth"]))
        ec4.q.flush(2)
        last = ec4.sysex[-1][1]
        self.assertEqual(last, rm.write_runs(3, [(22, "1")]))  # one character
        ec4.q.invalidate(rm.DISPLAY_OVERLAY)
        ec4.display_text(rm.DISPLAY_OVERLAY, 0, rm.overlay_rows(["Cutoff", "1010 Hz", "#####", "synth"]))
        ec4.q.flush(2)
        self.assertEqual(len(ec4.sysex[-1][1]), 13 + 80 * 3 + 1)  # full rewrite after invalidate
        ec4.q.close()

    def test_visibility_sent_only_on_change(self):
        ec4 = SlowEC4(delay=0.0)
        for _ in range(5):
            ec4.overlay_visible(True)
        ec4.q.flush(2)
        ec4.overlay_visible(True)
        ec4.q.flush(2)
        ec4.overlay_visible(False)
        ec4.q.flush(2)
        self.assertEqual([m for _, m in ec4.sysex], [rm.overlay_show(True), rm.overlay_show(False)])
        ec4.q.close()

    def test_paced_by_timeout_without_reply(self):
        ec4 = SlowEC4(reply=False)
        ec4.q.ack_timeout = 0.1
        ec4.display_text(rm.DISPLAY_NAMES, 0, "a" * 64)
        ec4.display_text(rm.DISPLAY_OVERLAY, 0, "b" * 80)
        ec4.overlay_visible(True)
        self.assertTrue(ec4.q.flush(2))
        times = [t for t, _ in ec4.sysex]
        self.assertEqual(len(times), 3)
        self.assertGreaterEqual(times[1] - times[0], 0.09)
        self.assertGreaterEqual(times[2] - times[1], 0.09)
        ec4.q.close()

    def test_ccs_are_not_held_up_by_display(self):
        ec4 = SlowEC4(delay=0.5)
        ec4.display_text(rm.DISPLAY_OVERLAY, 0, "x" * 80)
        time.sleep(0.05)  # display message in flight, waiting for the EC4
        ec4.send_ccs([(0, 16, 5)])
        time.sleep(0.05)
        self.assertEqual(ec4.sent, [(0, 16, 5)])
        ec4.q.close()

    def test_fast_turning_with_popup_stays_within_ec4_pace(self):
        ec4 = SlowEC4(delay=0.08)
        b = Bridge(cfg(layout_txt=os.devnull, backup_syx="none"), midi=ec4,
                   osc_send=lambda a, v: None)
        b.update_from_params(parse_params(default_tree()))
        ec4.q.flush(3)
        b.on_sysex(report(15, 0))
        ec4.q.flush(3)
        start = len(ec4.sysex)
        t0 = time.monotonic()
        v = 0
        while time.monotonic() - t0 < 1.0:  # turn cutoff fast for one second
            v = (v + 1) % 128
            b.on_cc(0, 17, v)
            time.sleep(0.005)
        ec4.q.flush(3)
        display_msgs = len(ec4.sysex) - start
        self.assertLessEqual(display_msgs, 16)  # ~12/s at most, never a backlog
        b._hide_overlay()
        ec4.q.close()


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
                self.assertIn("Group  1 [pol1]", r.stdout)
                self.assertIn("env/attack", r.stdout)
                r = subprocess.run([sys.executable, os.path.join(ROOT, "ec4bridge.py"), "-c", conf, "make-syx"],
                                   capture_output=True, text=True, timeout=20)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                with open(os.path.join(d, "ec4-layout.syx"), "rb") as f:
                    out = sx.parse_dump(f.read())
                names = sx.read_names(out, 15)
                self.assertEqual(names["groups"][:3], ["pol1", "pol2", "Dely"])
                self.assertEqual(names["encoders"][2][:3], ["----", "----", "----"])  # live names
                self.assertEqual(set(sum(names["encoders"], [])), {"----"})
                b0 = sx._group_base(15, 0)
                self.assertEqual(out.memory[b0 + 112 + 4], 0x64)  # push 5 -> group 5
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
