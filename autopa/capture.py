# autopa - saved-capture management (label + delete)
#
# Copyright (C) 2026  autopa contributors
#
# This file may be distributed under the terms of the GNU AGPLv3 (or later)
# license.
#
# AUTOPA_ANNOTATE writes operator labels onto a saved capture; AUTOPA_DELETE
# removes one. The captures themselves are produced by the decay/sweep methods.
import os, json


class CaptureMixin:
    def _register_capture_commands(self):
        self.gcode.register_command('AUTOPA_ANNOTATE', self.cmd_AUTOPA_ANNOTATE,
                                    desc=self.cmd_AUTOPA_ANNOTATE_help)
        self.gcode.register_command('AUTOPA_DELETE', self.cmd_AUTOPA_DELETE,
                                    desc=self.cmd_AUTOPA_DELETE_help)

    cmd_AUTOPA_ANNOTATE_help = ("Add/overwrite the operator labels on a saved "
        "capture. CAPTURE=latest|<filename>|<path> (default latest); set any of "
        "MATERIAL BRAND HOTEND NOTES. Samples and results are left untouched.")
    def cmd_AUTOPA_ANNOTATE(self, gcmd):
        import numpy as np
        path = self._resolve_capture(gcmd, gcmd.get('CAPTURE', 'latest'))
        fields = {k: v for k, v in self._operator_meta(gcmd).items()
                  if v is not None}
        if not fields:
            raise gcmd.error("autopa annotate: nothing to set; pass one or more "
                             "of MATERIAL BRAND HOTEND NOTES")
        data = np.load(path, allow_pickle=False)
        meta = json.loads(str(data['meta']))
        stats = json.loads(str(data['stats'])) if 'stats' in data.files else {}
        meta.update(fields)
        np.savez(path, samples=data['samples'], meta=json.dumps(meta),
                 stats=json.dumps(stats))
        # keep the cached UI captures index in step with the rewritten labels
        base = os.path.basename(path)
        for entry in self._captures_index:
            if entry.get('file') == base:
                entry.update(fields)
                break
        gcmd.respond_info("autopa: annotated %s -> %s" % (
            os.path.basename(path),
            ", ".join("%s=%s" % (k, v) for k, v in fields.items())))

    cmd_AUTOPA_DELETE_help = ("Delete a saved capture file. "
        "CAPTURE=<filename>|<path> is required ('latest' is deliberately not a "
        "default here). Irreversible.")
    def cmd_AUTOPA_DELETE(self, gcmd):
        capture = gcmd.get('CAPTURE')
        path = self._resolve_capture(gcmd, capture)
        os.remove(path)
        base = os.path.basename(path)
        self._captures_index = [e for e in self._captures_index
                            if e.get('file') != base]
        gcmd.respond_info("autopa: deleted %s" % base)
