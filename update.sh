#!/bin/bash
#
# update.sh - installs the nextion_node.py from this folder and restarts the panel.
# Keeps your settings. Usually started by get.sh. To run it by hand:
#
#   cd /root/nextion-hamvoip
#   bash update.sh
#
# GMRS-Link Nextion Node Control Panel - gmrs-link.com

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/nextion_node.py"
DST="/usr/local/sbin/nextion_node.py"
CONF="/etc/nextion-node.json"

IN=/dev/stdin
if [ ! -t 0 ] && ( : < /dev/tty ) 2>/dev/null; then IN=/dev/tty; fi

[ "$(id -u)" = "0" ] || { echo "Run this as root."; exit 1; }
[ -f "$SRC" ] || { echo "nextion_node.py not found in $HERE"; exit 1; }
[ -f "$DST" ] || { echo "The panel isn't installed yet - run  bash install.sh  instead."; exit 1; }

# read the version from the file text - never run an old copy to ask it
ver() { grep -m1 '^VERSION = ' "$1" 2>/dev/null | sed 's/^VERSION = "\(.*\)"/\1/'; }
NEW="$(ver "$SRC")"
OLD="$(ver "$DST")"
[ -n "$NEW" ] || { echo "The nextion_node.py in $HERE has no version number - download it again."; exit 1; }

echo
echo "  GMRS-Link Nextion Node Control Panel - update"
echo "  Installed version: ${OLD:-unknown}"
echo "  New version:       $NEW"

# What's new in this version, from CHANGELOG.md
if [ -f "$HERE/CHANGELOG.md" ] && [ "$OLD" != "$NEW" ]; then
    NOTES="$(awk -v v="## $NEW" '$0 ~ "^"v"( |$)" {on=1; next} /^## / {on=0} on' "$HERE/CHANGELOG.md")"
    if [ -n "$NOTES" ]; then
        echo
        echo "  What's new in $NEW:"
        echo "$NOTES" | sed '/^[[:space:]]*$/d; s/^/    /'
    fi
fi
echo

cp "$SRC" "$DST" && chmod 755 "$DST"
if [ "$(ver "$DST")" != "$NEW" ]; then
    echo "Copy failed - $DST is still not version $NEW."
    exit 1
fi
echo "  Installed version $NEW"

if [ -f "$CONF" ] && grep -q '"sleep_seconds": *0[,}[:space:]]' "$CONF"; then
    echo
    echo "  The screen saver is currently off (sleep_seconds is 0)."
    read -r -p "  Turn it back on with 10 minutes? (y/n) [y]: " A < "$IN"
    if [[ ! "$A" =~ ^[Nn] ]]; then
        sed -i 's/"sleep_seconds": *0\([,}[:space:]]\)/"sleep_seconds": 600\1/' "$CONF"
        echo "  Screen saver set to 10 minutes."
    fi
fi

systemctl restart nextion-node
sleep 4
echo
journalctl -u nextion-node -n 6 --no-pager -o cat | sed 's/^/    /'
echo
if systemctl is-active --quiet nextion-node; then
    echo -e "\033[1;32m  Done - the panel is running version $NEW.\033[0m"
else
    echo -e "\033[31m  The panel did not start. Send the lines above to gmrs-link.com for help.\033[0m"
fi
echo
