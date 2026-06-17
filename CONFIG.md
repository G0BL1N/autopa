# Configuring autopa

autopa is a Klipper extra. After `install.sh` has linked it in, add a single
`[autopa]` section to your `printer.cfg` and restart Klipper:

```bash
sudo systemctl restart klipper     # a plain RESTART does NOT reload extras
```

The bare section is enough — everything below is optional.

```ini
[autopa]
```

## Requirements

- A **load cell**: either a standalone
  [`[load_cell]`](https://www.klipper3d.org/Config_Reference.html#load_cell) or
  a [`[load_cell_probe]`](https://www.klipper3d.org/Config_Reference.html#load_cell_probe),
  calibrated with `LOAD_CELL_CALIBRATE` so it streams force. (The analysis works
  from raw ADC counts and treats grams as approximate, but the cell still has to
  be streaming.)
- **numpy** in Klipper's Python venv. `install.sh` installs it for you; stock
  Klipper doesn't ship it. To do it by hand: `~/klippy-env/bin/pip install numpy`.

## `[autopa]` options

| option | default | meaning |
| --- | --- | --- |
| `profile_path` | `~/printer_data/autopa/profiles.json` | where per-`(material, temperature)` PA profiles are persisted |
| `capture_dir` | `~/printer_data/autopa/captures` | where raw captures (`.npz`) are saved for offline analysis/replay |
| `save_captures` | `True` | save a capture for every calibration run |
| `hotend` | _(unset)_ | free-text hotend/nozzle description recorded in every saved capture's metadata. Hardware rarely changes, so set it once here; `AUTOPA_ANNOTATE HOTEND=...` still overrides it per-capture |

```ini
[autopa]
#profile_path: ~/printer_data/autopa/profiles.json
#capture_dir: ~/printer_data/autopa/captures
#save_captures: True
#hotend: Volcano, brass heatblock, CHC steel nozzle
```

## Sweep needs `max_extrude_cross_section` raised

The **Sweep** method couples a tiny axis "wobble" with each extrusion leg so
Klipper actually applies pressure advance (it only does so on moves that also
move X or Y). A large extrusion over a tiny axis move is a high extrude ratio,
which trips Klipper's `max_extrude_cross_section` guard in `[extruder]`.

`AUTOPA_SWEEP` pre-checks this and aborts with the exact value to set before it
changes anything, so you don't have to compute it up front — but to set it ahead
of time, `200` comfortably covers the default Sweep parameters:

```ini
[extruder]
max_extrude_cross_section: 200
```

The Klipper default is `4 × nozzle_diameter²` (≈ `0.64` mm² for a 0.4 mm nozzle).
Raising it only relaxes Klipper's safety check, not your prints. Note you cannot
*disable* the guard — Klipper requires the value to be greater than 0, so set a
large number rather than `0`. **Decay** does not need this.

## Using it in a print

All four workflows key off your slicer's `MATERIAL` + `TEMP`. The web UI is the
easiest way in; these g-code forms are for automation from your slicer or
macros.

**1. Recall a stored profile** (errors if none exists):

```gcode
AUTOPA_APPLY MATERIAL=PLA TEMP=220        ; add BRAND=... to narrow it
```

**2. Calibrate only when no profile exists, else recall.** `ELSE` names *your*
macro; it runs (with `MATERIAL`/`TEMP`/`BRAND` forwarded) only on a miss.
Movement, method, and nozzle cleaning are entirely up to you:

```gcode
AUTOPA_APPLY MATERIAL=PLA TEMP=220 ELSE=AUTOPA_CALIBRATE
```

```ini
[gcode_macro AUTOPA_CALIBRATE]
gcode:
    G1 X.. Y.. F6000                              ; move to a bin / safe spot
    AUTOPA_SWEEP                                  ; or AUTOPA_DECAY (experimental)
    # ... your nozzle wipe / purge here ...
    AUTOPA_SET MATERIAL={params.MATERIAL} TEMP={params.TEMP} PA={printer.extruder.pressure_advance}
```

**3. Calibrate every print** (no profiles): put `AUTOPA_SWEEP` (or
`AUTOPA_DECAY`) in your pre-print routine after the nozzle is hot and parked. It
measures, applies the PA live, and prints a paste-able line.

**4. No automation**: run `AUTOPA_SWEEP`/`AUTOPA_DECAY` once and paste the
printed `SET_PRESSURE_ADVANCE` line into your slicer's filament start g-code.

The method and tuning of `AUTOPA_SWEEP` / `AUTOPA_DECAY` are documented in
[docs/CALIBRATION.md](docs/CALIBRATION.md). Stored values are keyed per
`(material[+brand], temperature)`, so a re-print never re-calibrates.

## Command reference

| command | what it does |
| --- | --- |
| `AUTOPA_SWEEP` | calibrate by sweeping K (recommended); applies live + prints a paste line |
| `AUTOPA_DECAY` | calibrate from post-stop melt decay (experimental); applies live + prints a paste line |
| `AUTOPA_APPLY MATERIAL= TEMP= [BRAND=] [ELSE=]` | recall and apply a stored profile (or run `ELSE` on a miss) |
| `AUTOPA_SET MATERIAL= TEMP= PA= [BRAND=]` | store a PA value |
| `AUTOPA_FORGET MATERIAL= TEMP= [BRAND=]` | delete a stored profile |
| `AUTOPA_LIST` | list stored profiles |
| `AUTOPA_ANNOTATE [CAPTURE=] [MATERIAL=] [BRAND=] [HOTEND=] [NOTES=]` | label a saved capture |
| `AUTOPA_DELETE CAPTURE=` | delete a saved capture |

## Updating

If `install.sh` registered autopa with Moonraker's update manager, updates show
up in the **Fluidd/Mainsail "Update Manager"** — one click pulls and restarts
Klipper. Otherwise, update by hand:

```bash
cd ~/autopa && git pull && ./install.sh
sudo systemctl restart klipper
```
