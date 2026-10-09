# EC4 ↔ RNBO bridge

Controls every parameter of the patchers loaded in the RNBO runner on a Raspberry Pi from a
Faderfox EC4, with the parameter names on the EC4's display and values kept in sync both ways.

```
EC4 encoder ──CC──▶ ec4bridge ──OSC /rnbo/inst/N/params/<id>/normalized──▶ RNBO runner
EC4 display ◀─CC── ec4bridge ◀── OSC listener + OSCQuery polling ───────── RNBO runner
EC4 names   ◀── live SysEx display text (automatic, firmware 2.0+)
EC4 setup   ◀── SysEx setup dump (once: `send-layout`)
```

## How parameters are laid out

- The bridge reads the runner's OSCQuery tree (`http://<pi>:5678/rnbo/inst`) and lists every visible
  parameter of every instance, in RNBO's *display order*, then parameter index.
- Parameters fill one EC4 **setup** (default: setup 16), 16 per **group**, so the 16 groups are
  your pages (256 parameters max). Each instance starts on a new group.
- Encoder *e* in group *g* sends **CC (16 + e − 1) on MIDI channel g**. The scheme never
  changes, so encoders keep working after you load a different patcher, even before the names
  are updated.
- Names are shortened to the EC4's 4 characters, keeping the capitalization they have in RNBO.
  One word: its first letters without most vowels (`cutoff` → `cutf`, `resonance` → `resn`).
  Several words (spaces, camelCase or under_scores): the first word's letters plus a capital
  initial for each following word (`foo bar` / `fooBar` → `fooB`, `filterEnvAmount` → `fiEA`,
  `osc2Level` → `os2L`).
  Group names come from the instance name (`pol1`, `pol2` when an instance
  spans two groups). You can override any of them in `config.json`.

`python3 ec4bridge.py list` prints the current layout. While the bridge runs it also writes the
layout to `layout.txt`.

## Requirements

- Raspberry Pi running the RNBO runner, version 1.3 or newer.
- In the runner's audio settings, **MIDI system = `seq`** (the default). With `raw`, JACK holds the
  EC4 exclusively and the bridge can't open it. (The bridge does the SysEx itself, so the runner
  doesn't need raw MIDI.)
- EC4 firmware **2.0+** for names (live and via `send-layout`). Control works on any firmware if
  you program the setup by hand using the scheme above.
- Python 3.9+ (Raspberry Pi OS Bookworm has 3.11).

## Install (on the Pi)

```bash
cd ~
# copy this folder to /home/pi/ec4-rnbo-bridge (scp, USB stick, git…)
cd ec4-rnbo-bridge
sudo apt install -y python3-venv libasound2
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config.example.json config.json      # then edit; delete the example names/excludes you don't want
venv/bin/python ec4bridge.py list       # should print your parameters
```

### Check the Graph view

The runner can wire MIDI inputs straight into an instance. If the EC4 is connected to an
instance's MIDI input in the web interface's Graph view, remove that connection. Otherwise the
patch gets the EC4's CCs too, which is double control and can trigger any MIDI mappings you
made earlier. The bridge's own MIDI ports are hidden from JACK, so they never show up there.

## First-time EC4 setup

`send-layout` programs the RNBO setup on the EC4 (MIDI channels, CCs, display style, group
names). You need it **once**, and again only if you change `ec4_setup`, `resolution`, `cc_base`,
`encoder_mode` or `display`, or want new **group** names stored. Encoder names after that are
updated live (see below).

The EC4 only accepts a dump of **all 16 setups** at once. So the bridge first saves your current
setups, then sends them back unchanged except for the one it uses:

```bash
venv/bin/python ec4bridge.py capture-backup   # on the EC4: Send menu → "Send all setups", hold the encoder
venv/bin/python ec4bridge.py send-layout      # on the EC4: Receive menu ("Work in progress"), then press Enter
```

Afterwards, select setup 16 (or whichever you configured) on the EC4.

`send-layout` overwrites every setup with the backup. If you changed other setups on the EC4
since the backup, use `send-layout --fresh-backup`, which captures a new backup first.
`make-syx` only writes `ec4-layout.syx` without sending it, so you can also send it from the
Faderfox web editor.

## Live names and graph changes (firmware 2.0+)

The bridge writes parameter names straight onto the EC4's display, without the Receive menu.
This only works on encoders whose stored name is exactly `----` (EC4 manual: "Set encoder names
to '----' else the script can't write the names"). With `live_names` on, `send-layout` stores
`----` for every encoder of the RNBO setup, so **run `send-layout` once after turning live names
on** (or after updating from a version without live names).

- **When a new graph loads**, the names for the group you're on change within ~2 seconds, and a
  short message ("RNBO graph loaded" plus the instances and their group numbers) pops up.
- **When you switch groups** on the EC4, it tells the bridge, which writes that group's names
  and pops up the instance's full name (and "page 2 of 3" if it spans several groups).
- **Group names** (the 4×4 matrix shown while choosing a group) can't be written live; they come
  from the last `send-layout`. The pop-up is there to make up for that. If your graphs differ a
  lot, `"group_title_style": "number"` stores `G01`…`G16`, which never go out of date.
- **When you switch to one of your other setups**, the bridge pauses: those encoders don't
  touch RNBO and the bridge doesn't send them values. Coming back resends values and names.

The names page shows names; if you've pressed BAR or NUM to show values, press NAME to go back.

**While you turn an encoder**, a pop-up shows what the 4-letter name can't:

```
Filter Cutoff
1240 Hz
##########.....  67%
polysynth
```

The value is RNBO's real value, as reported back by the runner (enums show their label), with
its unit: set it on the parameter in your patch (`param cutoff @unit Hz`), or in `units` in
`config.json`. It
appears as soon as the knob pauses (the EC4 can't send knob data while it's drawing, so
nothing is drawn while you turn) and disappears 1.5 s later (`value_popup`,
`value_popup_mode`, `value_popup_seconds`).

To check, run `venv/bin/python ec4bridge.py test-display` with the EC4 on the RNBO setup. It asks
the EC4 which setup it's on, shows a test message for 4 seconds, then writes `T01`…`T16` as encoder
names for 4 seconds. If the test names don't appear, the stored names aren't `----` yet: run
`send-layout`.

### Device list and jumping to a group

- **Hold SHIFT + push encoder 16** to see a list of the loaded devices with their group numbers,
  e.g. ` 1 Synth1  2 Synth2` / ` 3 Delay`. It disappears when you let go. With more than 8 groups
  in use, the pages flip every `device_list_page_seconds` while you hold.
  (`"device_list_mode": "toggle"` makes it press-to-open instead: press again for the next page,
  once more to close.)
- **Push encoder N** to jump to group N. This uses the EC4's own "Grp" push-button type, which
  `send-layout` programs into the RNBO setup (`push_jumps_to_group`), so run `send-layout` once.
  After the jump, the new group's names appear and the pop-up shows which device you're on.

If SHIFT + push doesn't bring up the list on your EC4, set `"device_list_key": "user1"` and
use user key 1 instead (hold FUNC and press encoder 1).

## Run it as a service

```bash
sudo cp ec4bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ec4bridge
journalctl -u ec4bridge -f          # shows connections and the layout whenever it changes
```

Edit `User=` and the paths in the service file if you don't use `/home/pi/ec4-rnbo-bridge`.

## Day-to-day use

- Turn encoders, switch groups to page through parameters. Changes made elsewhere (web UI,
  presets, OSC, the patch itself) move the EC4's values too.
- **After loading a different patcher or set**, encoders control the new parameters and the
  display shows the new names automatically. No `send-layout` needed.
- Unplugging or replugging the EC4, or restarting the runner, is handled automatically.

## Configuration (`config.json`)

| key | default | meaning |
|---|---|---|
| `runner_host` | `127.0.0.1` | runner address (the bridge can also run on another machine) |
| `oscquery_port` / `osc_port` | 5678 / 1234 | runner ports |
| `listen_port` | 9123 | UDP port the runner sends value changes to |
| `midi_port` | `EC4` | text to match in the ALSA port name |
| `ec4_setup` | 16 | EC4 setup (1–16) used for RNBO |
| `setup_name` | `RNBO` | setup name shown on the EC4 |
| `resolution` | `7bit` | `7bit` (128 steps) or `14bit` (see below) |
| `display` | `100` (7-bit), `1000` (14-bit) | how the EC4 shows values. 7-bit: `127`, `100`, `+-63`, `+-50`, `onoff`, `off`. 14-bit: `1000`, `+-500`, `9999`, `off` |
| `encoder_mode` | `Acc1` | EC4 acceleration: `Acc0`–`Acc3`, `Div2/4/8`, `LSp2/4/6` |
| `cc_base` | 16 | first CC number (CCs 16–31) |
| `new_group_per_instance` | true | start each instance on a fresh group |
| `hide_devices` | `[]` | devices (RNBO instances) to leave off the EC4 entirely. Digits = instance number (`"2"`); anything else matches the device name, ignoring case and `strip_prefixes` (`"reverb"`, `"^mixer$"`). `list` shows what's hidden |
| `include` / `exclude` | `[]` | regexes matched against `inst/param-id` and the display name |
| `names` | `{}` | 4-char overrides, keyed by `"0/cutoff"` or just `"cutoff"` |
| `group_names` | `{}` | group name per instance index, e.g. `{"0": "Syn"}` |
| `group_title_style` | `instance` | group titles stored on the EC4: `instance` (from the instance name, e.g. `pol1`; uses `group_names`), `number` (`G01`…`G16`, right for any graph; `group_names` is ignored), `blank` |
| `name_case` | `keep` | capitalization of shortened names: `keep` (as written in RNBO), `title` (`Cutf`), `upper` (`CUTF`), `lower` (`cutf`). Names you type in `names`/`group_names` are always used exactly as typed |
| `live_names` | `true` | write names onto the EC4 display live (firmware 2.0+) |
| `notify_graph_change` | `true` | pop up a short message on the EC4 when a new graph loads |
| `notify_seconds` | 2.5 | how long that message stays |
| `push_jumps_to_group` | `true` | encoder N's push button jumps to group N (needs `send-layout`); `false` = pushes off |
| `device_list_key` | `shift+16` | key that shows the device list: `shift+1`…`shift+16`, `user1`…`user4` (FUNC + encoder 1/5/9/13), or `off` |
| `device_list_mode` | `momentary` | `momentary`: list shows while held; `toggle`: press to open, again to page/close |
| `device_list_page_seconds` | 2 | momentary mode: how fast pages flip while held (more than 8 groups) |
| `device_list_seconds` | 8 | toggle mode: how long the list stays up; momentary mode: safety timeout |
| `notify_group_change` | `true` | pop up the instance name when you switch groups |
| `value_popup` | `true` | while you turn an encoder, show its full name, value, a level bar and the device |
| `units` | `{}` | units shown after values in the pop-up, keyed like `names` (`{"0/cutoff": "Hz", "attack": "ms"}`); overrides the unit from RNBO |
| `value_popup_mode` | `rest` | `rest`: draw the pop-up once the knob pauses (`value_popup_rest_ms`, 120); `live`: also redraw while turning, at most every `value_popup_interval_ms` (250). The EC4 stops sending knob data while it draws, so `live` makes knobs less smooth |
| `osc_send_interval_ms` | 15 | at most one change per parameter is sent to the runner in this time (the newest value; the last value of a turn is never lost). 0 = send every step |
| `display_quiet_ms` | 400 | while a knob moves and until it has been still this long, nothing is sent to the EC4 (values of other parameters, pop-ups, names); it catches up afterwards. The EC4 stops reading its knobs while it handles incoming data, so this keeps turns smooth |
| `value_popup_seconds` | 1.5 | how long that stays after you stop turning |
| `zero_unused` | `true` | set every encoder without a parameter to 0 (on a new graph, at start-up and when you return to the RNBO setup), so no values are left over from the previous graph |
| `live_names_refresh` | 0 | rewrite the names every N seconds (only if the EC4 ever shows stale names) |
| `strip_prefixes` | `[]` | prefixes removed from instance and parameter names before shortening, e.g. `["j."]` turns `j.reverb` into `revr` |
| `feedback_holdoff_ms` | 1000 | after you turn an encoder, ignore the runner's reports for it this long, so a late report can't snap the knob back |
| `poll_interval` | 2.0 | seconds between quick checks for a graph change (set name and device names only; cheap for the runner) |
| `full_refresh_interval` | 0 | also re-read the whole graph every N seconds (0 = only when it changed). Reading a big graph can keep the runner busy for seconds, which makes the sound stutter |

**14-bit mode** uses the EC4's 14-bit CC type (CC 16–31 plus LSB on CC 48–63) for smooth
filter sweeps. It is untested on hardware. Try `encoder_mode: "Acc3"` with it, or a
"large step" mode, so a full sweep doesn't take many turns.

## Troubleshooting

- **Knobs lag, jump or skip steps**:
  1. In the RNBO web interface, make sure the EC4 isn't also controlling the patch directly:
     remove its connection to instances in the Graph view and delete any MIDI mappings that use
     its CCs (MIDI Mappings view). Two paths to the same parameter fight each other.
  2. Run `venv/bin/python ec4bridge.py monitor` and turn a knob slowly. Steps of more than 1
     mean the EC4 itself is skipping: that's acceleration, set `"encoder_mode": "Acc0"` and run
     `send-layout`. For finer control use `"resolution": "14bit"` (plus `send-layout`).
  3. Make sure `feedback_holdoff_ms` is at least 1000 (older example configs had 250).
  4. Close the RNBO web interface in your browser while playing; it adds load on the Pi.
  5. Record a trace while the problem happens: run `venv/bin/python ec4bridge.py trace` (with the
     service running) and turn knobs for 20 seconds. It times every stage of each turn separately:
     the EC4 sending, the bridge passing it on, the runner applying it. The summary then says
     which stage stalls, and whether something other than the bridge is also changing the
     parameters. The full timeline is saved as `trace-<date>.txt` in this folder. Without the
     command: `sudo systemctl kill -s USR1 ec4bridge`, then `journalctl -u ec4bridge -n 30`.
     `trace_seconds` (default 20) sets the length.
  6. If the trace shows the runner taking seconds to report changes back, check whether it freezes
     on its own: stop the bridge (`sudo systemctl stop ec4bridge`), close the web interface, and run
     `venv/bin/python ec4bridge.py runner-check` (60 s; `--seconds N` to change). It asks the runner
     for one small value every 0.25 s and prints every answer slower than 200 ms, with the time.

- **"Receive error" on the EC4 during `send-layout`**: something else reached the EC4 in the middle
  of the dump. `send-layout` and `capture-backup` pause a running bridge service automatically
  (they create `ec4bridge.pause`, and the log shows "setup dump in progress"). If it still happens:
  stop the service first (`sudo systemctl stop ec4bridge`), make sure nothing else is sending to
  the EC4's MIDI in socket, and slow the transfer with `"sysex_page_pause_ms": 20`. A rejected dump
  leaves your setups as they were or partly written; running `send-layout` again restores all of
  them from the backup.

- `No MIDI port matching 'EC4'`: run `aconnect -l` and put part of the EC4's name in `midi_port`.
- `Could not reach the RNBO runner`: check `curl http://127.0.0.1:5678/rnbo/inst`.
- Encoders do nothing: make sure the EC4 is on the RNBO setup, and check the log for "EC4 connected".
  If you programmed the setup by hand, it needs CC absolute, channel = group number, CC 16–31.
- Values jump when you grab an encoder: the bridge sends values on startup and on every layout
  change. Switching to the setup after the bridge started is fine, because the EC4 keeps the values.

## Tests

`python3 -m unittest discover -s tests` runs the tests against a mock runner. They cover the dump
format, layout, mapping and feedback logic, OSC over UDP, and the CLI. The ALSA MIDI layer needs
real hardware and isn't covered.

Dump format from the open-source Faderfox editor:
https://github.com/privatepublic-de/faderfox-editor (MIT). The runner OSC paths come from
https://github.com/Cycling74/rnbo.oscquery.runner.
The live display and setup/group report commands come from the DrivenByMoss Bitwig extension
(https://github.com/git-moss/DrivenByMoss, LGPLv3).
