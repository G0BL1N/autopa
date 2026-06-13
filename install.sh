#!/usr/bin/env bash
# autopa installer — run ON the printer SBC from the repo root:
#
#   cd ~/autopa && ./install.sh
#
# Idempotent: safe to re-run after every update. It links the Klipper extra
# and patches nginx (a marker-delimited block, validated with `nginx -t` and
# rolled back on failure — the same managed-section pattern tools like
# OctoEverywhere use for moonraker.conf). It never restarts Klipper itself —
# do that when *you* are ready.
#
#   --no-nginx        skip the nginx step entirely
#   NGINX_SITE=<file> patch this site file instead of auto-detecting
#   KLIPPER=<dir>     klipper checkout (default ~/klipper)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER="${KLIPPER:-$HOME/klipper}"
EXTRAS="$KLIPPER/klippy/extras/autopa"
CONFIG_DIR="${CONFIG_DIR:-$HOME/printer_data/config}"
NGINX_BLOCK="    # >>> autopa >>> (managed by autopa install.sh -- do not edit)
    location = /autopa {
        return 301 /autopa/;
    }
    location /autopa/captures/ {
        alias $HOME/printer_data/autopa/captures/;
    }
    location /autopa/ {
        alias $REPO/web/dist/;
        try_files \$uri \$uri/ /autopa/index.html;
    }
    # <<< autopa <<<"

if [ ! -d "$KLIPPER/klippy/extras" ]; then
  echo "error: klipper not found at $KLIPPER (override with KLIPPER=...)" >&2
  exit 1
fi

# -- 1. Klipper extra: symlink the package out of this repo -------------------
if [ -d "$EXTRAS" ] && [ ! -L "$EXTRAS" ]; then
  echo "replacing copied $EXTRAS with a symlink"
  rm -rf "$EXTRAS"
fi
ln -sfn "$REPO/autopa" "$EXTRAS"
echo "ok: $EXTRAS -> $REPO/autopa"

# -- 2. Web UI: prebuilt static files, served by the existing nginx ----------
if [ -d "$REPO/web/dist" ]; then
  echo "ok: web UI at $REPO/web/dist"
else
  echo "note: web/dist missing -- the UI is built on a workstation"
  echo "      (cd web && bun run build), not on the printer. The g-code"
  echo "      commands work without it."
fi

# -- 3. nginx: insert/refresh the managed location block ----------------------
print_snippet() {
  echo
  echo "Add to the existing nginx server block (e.g. fluidd's) and reload:"
  echo
  echo "$NGINX_BLOCK"
  echo
  echo "    sudo nginx -t && sudo systemctl reload nginx"
}

find_site() {
  if [ -n "${NGINX_SITE:-}" ]; then
    echo "$NGINX_SITE"
    return
  fi
  # the site that proxies Moonraker's websocket is the one fronting fluidd
  grep -ls 'location /websocket' \
    /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf 2>/dev/null | head -1
}

patch_nginx() {
  local site tmp
  site="$(find_site)"
  if [ -z "$site" ] || [ ! -f "$site" ]; then
    echo "nginx: no site with a /websocket proxy found -- manual setup:"
    print_snippet
    return
  fi
  site="$(readlink -f "$site")"
  if [ -t 0 ]; then
    read -r -p "nginx: add the /autopa/ locations to $site? [Y/n] " a
    case "$a" in [nN]*) print_snippet; return ;; esac
  fi
  tmp="$(mktemp)"
  # drop any previous managed block, then re-insert before the server block's
  # final closing brace -- re-running always converges on the current paths
  sed '/>>> autopa >>>/,/<<< autopa <<</d' "$site" | awk -v block="$NGINX_BLOCK" '
    { lines[NR] = $0 }
    END {
      last = 0
      for (i = 1; i <= NR; i++)
        if (lines[i] ~ /^[[:space:]]*}[[:space:]]*$/) last = i
      for (i = 1; i <= NR; i++) {
        if (i == last) print block
        print lines[i]
      }
    }' > "$tmp"
  sudo cp "$site" "$site.autopa-bak"
  sudo cp "$tmp" "$site"
  rm -f "$tmp"
  if sudo nginx -t; then
    sudo systemctl reload nginx
    echo "ok: nginx patched ($site; backup at $site.autopa-bak)"
  else
    sudo cp "$site.autopa-bak" "$site"
    echo "nginx: validation FAILED -- $site restored from backup. Manual setup:"
    print_snippet
  fi
}

if [ "${1:-}" = "--no-nginx" ]; then
  print_snippet
else
  patch_nginx
fi

# -- 4. remaining one-time steps ----------------------------------------------
if ! grep -qs '^\[autopa\]' "$CONFIG_DIR"/*.cfg 2>/dev/null; then
  echo
  echo "TODO: add [autopa] to printer.cfg (options: see autopa.cfg)"
fi

echo
echo "Restart Klipper to (re)load the extra:  sudo systemctl restart klipper"
echo "UI: http://<printer>/autopa/"
