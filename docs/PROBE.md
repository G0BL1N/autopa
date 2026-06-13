# ALPS load cell as a Z probe

The Mellow Fly ALPSv6 we flash for autopa is first and foremost a **nozzle-contact Z
probe** — a native Klipper [`[load_cell_probe]`](https://www.klipper3d.org/Config_Reference.html#load_cell_probe).
The same force signal also feeds autopa's pressure-advance tuning. For flashing and
wiring see [ALPS.md](ALPS.md); for parameter definitions see
[Klipper's Config Reference](https://www.klipper3d.org/Config_Reference.html).

The nozzle taps the bed; each tap re-tares and descends until the force crosses
`trigger_force`. The values below give single-tap repeatability on the order of a few
microsteps (≈0.005–0.008 mm here) at the bed center, looser at compliant corners.

## Drop-in config

Non-default values only; everything else stays at Klipper defaults.

```ini
[load_cell_probe]
# ... sensor_type / pins (see ALPS.md) ...
counts_per_gram: 138             # property of the ALPS module — reusable as-is (see notes)
# reference_tare_counts: <set once by LOAD_CELL_CALIBRATE on your machine>
trigger_force: 200
speed: 0.5
lift_speed: 5

[stepper_z]
endstop_pin: probe:z_virtual_endstop
second_homing_speed: 0.5         # fine homing tap at probe speed
homing_retract_dist: 1.0

[safe_z_home]
z_hop: 2

[bed_mesh]
horizontal_move_z: 2
[screws_tilt_adjust]
horizontal_move_z: 2
```

## Notes

- **`counts_per_gram: 138` is reusable.** It is a property of the ALPS load cell module
  (calibrated off-printer against a known weight), not of your machine — adopt it directly.
  Because `trigger_force` is measured in grams through it, the `trigger_force: 200` value
  transfers directly too. `reference_tare_counts` *is* per-build: run `LOAD_CELL_CALIBRATE`
  once to set yours.
- **`speed: 0.5` mm/s.** Probe repeatability improves as the descent slows, flattening at
  ~0.5 mm/s; slower only costs time.
- **`trigger_force: 200`.** Reliable and outlier-free; lower values triggered early at the
  start of the move.
- **Homing inherits the probe.** Set `second_homing_speed` to the probe `speed` (0.5) so
  G28 is as precise as a probe and `z_offset` stays consistent; a small
  `homing_retract_dist` keeps the fine tap quick.
- **Coast low.** With a flat sheet, `z_hop` / `horizontal_move_z` ≈ 2 mm keep the slow
  descent short, so homing / mesh / screws stay fast.
- **Probe at 140 °C with a clean nozzle** — bed-safe, no ooze.
- **Validate at the corners, not just the center.** Compliant points (bed corners) are the
  worst case for triggering and read noisier than the center; confirm `SCREWS_TILT_CALCULATE`
  and `BED_MESH_CALIBRATE` complete there.

## Don't use the drift filter

Klipper's `drift_filter_cutoff_frequency` (a high-pass; needs SciPy) looks attractive for a
load cell, but it conflicts with slow probing. A high-pass passes only *fast-changing*
force, while a slow descent produces a slow force ramp — especially at compliant points
like bed corners, where the bed flexes and force builds gradually. There the filter
attenuates the real contact force below the threshold and the probe drives into the bed
**without triggering** (hundreds of grams of real force present, no trigger). Even a very
low 0.1 Hz cutoff only borderline worked at slow speed. Slow speed alone gives the
precision, so we leave the filter off — triggering stays reliable everywhere and there's no
SciPy dependency.

## z_offset

The trigger point shifts slightly with `speed`, so set `z_offset` at your final speed:
calibrate with `PROBE_CALIBRATE` + paper, or leave it `0.0` and dial the first layer by
feel — for a nozzle-contact probe the trigger already sits at ~true zero.

---

**See also:** [README](../README.md) · [Flashing the ALPS load cell](ALPS.md) ·
[Calibration methods](CALIBRATION.md)
