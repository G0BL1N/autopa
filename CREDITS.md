# Credits & licensing

## Licensing

Released under the **GNU Affero General Public License v3.0 (or later)** — see
[LICENSE](LICENSE).
In short, the AGPL is the GPL plus a network clause (§13): if you distribute a modified
autopa, or run one as a network service, make your source available to its users.

## What it builds on

- **[Klipper](https://www.klipper3d.org/)** (GPLv3) — the platform; autopa is a Klipper
  `extras` module that uses Klipper's load-cell capture, motion, and pressure-advance
  machinery — everything runs inside Klipper on the printer's SBC.

- **[PrusaPATuner](https://github.com/CNCKitchen/PrusaPATuner)** by **CNCKitchen**
  (Stefan Hermann), AGPL-3.0 — the origin of the **Sweep** method's algorithm. Its in-air
  slow/fast square-wave measurement and step-response analysis are ported here (numpy-only,
  trimmed to the primary step-response estimator); see
  [docs/CALIBRATION.md](docs/CALIBRATION.md).

- **[Snapmaker U1 / `u1-klipper`](https://github.com/Snapmaker/u1-klipper)** and
  **[markniu's `bd_pressure`](https://github.com/markniu/bd_pressure)** — credited *via*
  PrusaPATuner, which combines U1's in-air square-wave motion geometry with bd_pressure's
  step-response analysis. Both reach autopa indirectly, through PrusaPATuner.

## Special thanks

- **[Dmitry Butyugin (dmbutyugin)](https://github.com/dmbutyugin)** — for Klipper's
  `load_cell` / `load_cell_probe` stack and the ALPS support
  ([Klipper contributions](https://github.com/Klipper3d/klipper/commits?author=dmbutyugin))
  that autopa is built directly on.

## Built with Claude Code

Designed and written with [Claude Code](https://claude.com/claude-code), Anthropic's CLI
coding agent.
