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
- Names are shortened to the EC4's 4 characters (`cutoff` → `cutf`, `resonance` → `resn`,
  `osc2Level` → `os2L`), keeping the capitalization they have in RNBO.
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

`send-layout` programs the RNBO setup on the EC4 (MIDI channels, CCs, display style, and names
as a fallback). You need it **once**, and again only if you change `ec4_setup`, `resolution`,
`cc_base`, `encoder_mode` or `display`. Names after that are updated live (see below).

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

The bridge writes parameter names straight onto the EC4's display, without the Receive menu:

- **When a new graph loads**, the names for the group you're on change within ~2 seconds, and a
  short message ("RNBO graph loaded" plus the instances and their group numbers) pops up.
- **When you switch groups** on the EC4, it tells the bridge, which writes that group's names.
- **When you switch to one of your other setups**, the bridge pauses: those encoders don't
  touch RNBO and the bridge doesn't send them values. Coming back resends values and names.

The names page shows names; if you've pressed BAR or NUM to show values, press NAME to go back.

To check that your EC4 accepts live text, run `venv/bin/python ec4bridge.py test-display`: it
asks the EC4 which setup it's on and shows a test message on its display for 4 seconds.

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
| `include` / `exclude` | `[]` | regexes matched against `inst/param-id` and the display name |
| `names` | `{}` | 4-char overrides, keyed by `"0/cutoff"` or just `"cutoff"` |
| `group_names` | `{}` | group name per instance index, e.g. `{"0": "Syn"}` |
| `name_case` | `keep` | capitalization of shortened names: `keep` (as written in RNBO), `title` (`Cutf`), `upper` (`CUTF`), `lower` (`cutf`). Names you type in `names`/`group_names` are always used exactly as typed |
| `live_names` | `true` | write names onto the EC4 display live (firmware 2.0+) |
| `notify_graph_change` | `true` | pop up a short message on the EC4 when a new graph loads |
| `notify_seconds` | 2.5 | how long that message stays |
| `live_names_refresh` | 0 | rewrite the names every N seconds (only if the EC4 ever shows stale names) |
| `strip_prefixes` | `[]` | prefixes removed from instance and parameter names before shortening, e.g. `["j."]` turns `j.reverb` into `revr` |
| `feedback_holdoff_ms` | 250 | don't echo a value back to an encoder you're turning |
| `poll_interval` | 2.0 | seconds between OSCQuery scans (patch changes, missed values) |

**14-bit mode** uses the EC4's 14-bit CC type (CC 16–31 plus LSB on CC 48–63) for smooth
filter sweeps. It is untested on hardware. Try `encoder_mode: "Acc3"` with it, or a
"large step" mode, so a full sweep doesn't take many turns.

## Troubleshooting

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
