# autopa - per-(material, temperature) pressure-advance profile store
#
# Copyright (C) 2026  autopa contributors
#
# This file may be distributed under the terms of the GNU AGPLv3 (or later)
# license.
#
# Calibrated PA is persisted per (material, temperature) pair so a re-print
# never needs re-calibration. Storage is a JSON file keyed "<MATERIAL>@<TEMP>".
import os, json


# Profiles are keyed "<MATERIAL>@<TEMP>", e.g. "PLA@220". Material is the
# slicer-compatible token (PLA, PETG); BRAND is an optional refinement that
# follows the material in the key, e.g. "PLA PRUSAMENT@220" -- material-first
# so an alphabetical profile list groups by material, and the same
# (material, brand) pair recombines to the same key wherever it is supplied.
def _profile_key(material, temp, brand=None):
    name = str(material).strip().upper()
    if brand and str(brand).strip():
        name = "%s %s" % (name, str(brand).strip().upper())
    return "%s@%d" % (name, int(round(float(temp))))


class ProfileMixin:
    def _register_profile_commands(self):
        self.gcode.register_command('AUTOPA_LIST', self.cmd_AUTOPA_LIST,
                                    desc=self.cmd_AUTOPA_LIST_help)
        self.gcode.register_command('AUTOPA_APPLY', self.cmd_AUTOPA_APPLY,
                                    desc=self.cmd_AUTOPA_APPLY_help)
        self.gcode.register_command('AUTOPA_SET', self.cmd_AUTOPA_SET,
                                    desc=self.cmd_AUTOPA_SET_help)
        self.gcode.register_command('AUTOPA_FORGET', self.cmd_AUTOPA_FORGET,
                                    desc=self.cmd_AUTOPA_FORGET_help)

    def _load_profiles(self):
        try:
            with open(self.profile_path) as f:
                return json.load(f)
        except (IOError, OSError, ValueError):
            return {}

    def _save_profiles(self):
        d = os.path.dirname(self.profile_path)
        if d and not os.path.exists(d):
            os.makedirs(d)
        tmp = self.profile_path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self._profiles, f, indent=2, sort_keys=True)
        os.replace(tmp, self.profile_path)

    cmd_AUTOPA_SET_help = ("Store a pressure-advance value: "
                           "AUTOPA_SET MATERIAL=PLA TEMP=220 PA=0.034 "
                           "[BRAND=Prusament]")
    def cmd_AUTOPA_SET(self, gcmd):
        material = gcmd.get('MATERIAL')
        brand = gcmd.get('BRAND', None)
        temp = gcmd.get_float('TEMP')
        pa = gcmd.get_float('PA', minval=0.)
        key = _profile_key(material, temp, brand)
        import time as _t
        self._profiles[key] = {'pa': pa, 'material': material.upper(),
                               'brand': brand.strip() if brand else None,
                               'temp': int(round(temp)),
                               'updated': _t.strftime("%Y-%m-%d %H:%M:%S"),
                               'source': 'manual'}
        self._save_profiles()
        gcmd.respond_info("autopa: stored %s -> PA %.4f" % (key, pa))

    cmd_AUTOPA_APPLY_help = ("Apply a stored pressure-advance value: "
                             "AUTOPA_APPLY MATERIAL=PLA TEMP=220 "
                             "[BRAND=Prusament] [ELSE=<macro>]. ELSE names a "
                             "macro to run (with MATERIAL/TEMP/BRAND forwarded) "
                             "when no profile exists, instead of erroring -- the "
                             "calibrate-only-if-missing hook.")
    def cmd_AUTOPA_APPLY(self, gcmd):
        material = gcmd.get('MATERIAL')
        brand = gcmd.get('BRAND', None)
        temp = gcmd.get_float('TEMP')
        key = _profile_key(material, temp, brand)
        prof = self._profiles.get(key)
        if prof is None:
            # ELSE: hand off to the operator's own macro (move-to-bin, calibrate
            # with whatever method, clean, AUTOPA_SET) instead of aborting -- the
            # "calibrate only when I don't already have a profile" workflow.
            # autopa stays method-agnostic: it just forwards the labels and calls.
            else_macro = gcmd.get('ELSE', None)
            if else_macro:
                fwd = "%s MATERIAL=%s TEMP=%s" % (
                    else_macro, material, gcmd.get('TEMP'))
                if brand:
                    fwd += " BRAND=%s" % brand
                gcmd.respond_info("autopa: no profile for %s -- running ELSE "
                                  "macro %s" % (key, else_macro))
                self.gcode.run_script_from_command(fwd)
                return
            raise gcmd.error("autopa: no profile for %s (have: %s)"
                             % (key, ", ".join(sorted(self._profiles)) or "none"))
        pa = prof['pa']
        self.gcode.run_script_from_command(
            "SET_PRESSURE_ADVANCE ADVANCE=%.4f" % pa)
        gcmd.respond_info("autopa: applied %s -> PA %.4f" % (key, pa))

    cmd_AUTOPA_FORGET_help = ("Delete a stored profile: "
                              "AUTOPA_FORGET MATERIAL=PLA TEMP=220 "
                              "[BRAND=Prusament]")
    def cmd_AUTOPA_FORGET(self, gcmd):
        key = _profile_key(gcmd.get('MATERIAL'), gcmd.get_float('TEMP'),
                           gcmd.get('BRAND', None))
        if self._profiles.pop(key, None) is None:
            raise gcmd.error("autopa: no profile for %s" % key)
        self._save_profiles()
        gcmd.respond_info("autopa: forgot %s" % key)

    cmd_AUTOPA_LIST_help = "List stored pressure-advance profiles"
    def cmd_AUTOPA_LIST(self, gcmd):
        if not self._profiles:
            gcmd.respond_info("autopa: no stored profiles")
            return
        lines = ["autopa profiles:"]
        for key in sorted(self._profiles):
            p = self._profiles[key]
            lines.append("  %-14s PA %.4f  (%s, %s)"
                         % (key, p['pa'], p.get('source', '?'),
                            p.get('updated', '?')))
        gcmd.respond_info("\n".join(lines))
