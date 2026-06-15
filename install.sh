#!/usr/bin/env bash
# autopa installer — run ON the printer SBC from the repo root:
#
#   cd ~/autopa && ./install.sh
#
# Idempotent: safe to re-run after every update. It:
#   * links the Klipper extra into ~/klipper/klippy/extras/autopa
#   * installs numpy into Klipper's venv if missing — autopa's analysis needs
#     it and stock Klipper does not ship it
#   * registers autopa with Moonraker's update manager, so updates are one click
#     in the Fluidd/Mainsail UI (it git-pulls and restarts Klipper, which also
#     means the new extra is actually reloaded — a plain RESTART would not)
#   * patches nginx to serve the web UI (a marker-delimited block, validated
#     with `nginx -t` and rolled back on failure — the same managed-section
#     pattern tools like OctoEverywhere use for moonraker.conf)
# It never restarts Klipper itself — do that when *you* are ready.
#
#   --no-nginx         skip the nginx (web UI) step
#   --no-moonraker     skip the Moonraker update-manager step
#   KLIPPER=<dir>      klipper checkout    (default ~/klipper)
#   KLIPPER_ENV=<dir>  klipper python venv (default ~/klippy-env)
#   CONFIG_DIR=<dir>   printer config dir  (default ~/printer_data/config)
#   NGINX_SITE=<file>  patch this site file instead of auto-detecting
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER="${KLIPPER:-$HOME/klipper}"
KLIPPER_ENV="${KLIPPER_ENV:-$HOME/klippy-env}"
EXTRAS="$KLIPPER/klippy/extras/autopa"
CONFIG_DIR="${CONFIG_DIR:-$HOME/printer_data/config}"
MOONRAKER_CONF="$CONFIG_DIR/moonraker.conf"

NO_NGINX=0
NO_MOONRAKER=0
for opt in "$@"; do
  case "$opt" in
    --no-nginx) NO_NGINX=1 ;;
    --no-moonraker) NO_MOONRAKER=1 ;;
    *) echo "unknown option: $opt" >&2; exit 2 ;;
  esac
done

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

# -- 2. numpy: required by the analysis, not shipped by stock Klipper ---------
# Klipper installs input-shaper users' numpy the same way (into klippy-env);
# autopa needs it for every fit. Stream pip's progress -- a from-source build
# on a slow SBC can take minutes.
PY="$KLIPPER_ENV/bin/python"
PIP="$KLIPPER_ENV/bin/pip"
if [ ! -x "$PY" ]; then
  echo "warning: klipper venv not found at $KLIPPER_ENV (override KLIPPER_ENV=...)"
  echo "         install numpy yourself: <venv>/bin/pip install numpy"
elif "$PY" -c 'import numpy' 2>/dev/null; then
  echo "ok: numpy already present in $KLIPPER_ENV"
else
  echo "checking numpy in $KLIPPER_ENV ... not found -- installing"
  echo "  ($PIP install numpy; may take a few minutes on an SBC)"
  "$PIP" install numpy
  echo "ok: numpy installed"
fi

# -- 3. Web UI: prebuilt static files, served by the existing nginx ----------
if [ -d "$REPO/web/dist" ]; then
  echo "ok: web UI at $REPO/web/dist"
else
  echo "note: web/dist missing -- the UI is built on a workstation"
  echo "      (cd web && bun run build), not on the printer. The g-code"
  echo "      commands work without it."
fi

# -- 4. nginx: insert/refresh the managed location block ----------------------
print_nginx_snippet() {
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
    print_nginx_snippet
    return
  fi
  site="$(readlink -f "$site")"
  if [ -t 0 ]; then
    read -r -p "nginx: add the /autopa/ locations to $site? [Y/n] " a
    case "$a" in [nN]*) print_nginx_snippet; return ;; esac
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
    print_nginx_snippet
  fi
}

if [ "$NO_NGINX" = 1 ]; then
  print_nginx_snippet
else
  patch_nginx
fi

# -- 5. Moonraker: register the update manager --------------------------------
# One-click updates from the Fluidd/Mainsail UI. managed_services: klipper makes
# Moonraker restart Klipper after a pull, so the reloaded extra actually takes
# effect (a plain RESTART does not reload extras).
ORIGIN="$(git -C "$REPO" config --get remote.origin.url 2>/dev/null \
          || echo 'https://github.com/G0BL1N/autopa.git')"
MOON_BLOCK="# >>> autopa >>> (managed by autopa install.sh -- do not edit)
[update_manager autopa]
type: git_repo
path: $REPO
origin: $ORIGIN
primary_branch: main
managed_services: klipper
# <<< autopa <<<"

print_moonraker_snippet() {
  echo
  echo "Add to moonraker.conf and restart Moonraker:"
  echo
  echo "$MOON_BLOCK"
  echo
  echo "    sudo systemctl restart moonraker"
}

patch_moonraker() {
  local tmp
  if [ ! -f "$MOONRAKER_CONF" ]; then
    echo "moonraker: $MOONRAKER_CONF not found (override CONFIG_DIR=...)"
    print_moonraker_snippet
    return
  fi
  if [ -t 0 ]; then
    read -r -p "moonraker: register autopa's update manager in $MOONRAKER_CONF? [Y/n] " a
    case "$a" in [nN]*) print_moonraker_snippet; return ;; esac
  fi
  tmp="$(mktemp)"
  # drop any previous managed block, then re-append -- re-running converges
  { sed '/>>> autopa >>>/,/<<< autopa <<</d' "$MOONRAKER_CONF"
    printf '\n%s\n' "$MOON_BLOCK"
  } > "$tmp"
  cp "$MOONRAKER_CONF" "$MOONRAKER_CONF.autopa-bak"
  mv "$tmp" "$MOONRAKER_CONF"
  echo "ok: moonraker update-manager registered ($MOONRAKER_CONF; backup .autopa-bak)"
  echo "    restart Moonraker to pick it up: sudo systemctl restart moonraker"
}

if [ "$NO_MOONRAKER" = 1 ]; then
  print_moonraker_snippet
else
  patch_moonraker
fi

# -- 6. remaining one-time steps ----------------------------------------------
if ! grep -qs '^\[autopa\]' "$CONFIG_DIR"/*.cfg 2>/dev/null; then
  echo
  echo "TODO: add an [autopa] section to printer.cfg -- see CONFIG.md"
fi

echo
echo "Restart Klipper to (re)load the extra:  sudo systemctl restart klipper"
echo "UI: http://<printer>/autopa/"
