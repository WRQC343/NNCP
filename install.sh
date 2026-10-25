#!/bin/bash
#
# install.sh - installer for the GMRS-Link Nextion Node Control Panel
#              on a HamVOIP / AllStar node (Raspberry Pi)      gmrs-link.com
#
#   bash install.sh             install, or reinstall / change settings
#   bash install.sh uninstall   remove it
#
# Usually started by get.sh (the one-line GitHub installer). Run as root.

ROOT="${ROOT:-}"                       # only used for testing
BIN="$ROOT/usr/local/sbin/nextion_node.py"
CONF="$ROOT/etc/nextion-node.json"
UNIT="$ROOT/etc/systemd/system/nextion-node.service"
BOOTCFG="$ROOT/boot/config.txt"
CMDLINE="$ROOT/boot/cmdline.txt"
HERE="$(cd "$(dirname "$0")" && pwd)"
RECONN_FLAG="/etc/asterisk/local/ReConn/~ReConn_disabled"   # exists = ReConn disabled
GPS_FLAG="/tmp/GPS.ENABLED"                                  # exists = GPS enabled
NEED_REBOOT=0

say()  { echo -e "\n\033[1;36m==> $*\033[0m"; }
ok()   { echo -e "    \033[32m$*\033[0m"; }
warn() { echo -e "    \033[33m$*\033[0m"; }
die()  { echo -e "\n\033[31mERROR: $*\033[0m\n"; exit 1; }

sc() {   # systemctl, skipped when testing
    if [ -n "$ROOT" ]; then echo "    (test) systemctl $*"; else systemctl "$@" 2>/dev/null; fi
}

# Answers come from the keyboard even when this script was started through a
# pipe (curl ... | bash), where standard input is the script itself.
IN=/dev/stdin
if [ ! -t 0 ] && ( : < /dev/tty ) 2>/dev/null; then IN=/dev/tty; fi

ask() {  # ask "Question" default -> $REPLY
    local q="$1" def="$2"
    if [ -n "$def" ]; then read -r -p "    $q [$def]: " REPLY < "$IN"
    else read -r -p "    $q: " REPLY < "$IN"; fi
    REPLY="${REPLY:-$def}"
}

isyes() { [[ "$1" =~ ^[Yy] ]]; }
digits() { echo "$1" | tr -cd '0-9' | cut -c1-7; }
label()  { echo "$1" | tr -cd 'A-Za-z0-9 ._/-' | cut -c1-10; }
ver()    { grep -m1 '^VERSION = ' "$1" 2>/dev/null | sed 's/^VERSION = "\(.*\)"/\1/'; }

# ------------------------------------------------------------------ uninstall
if [ "$1" = "uninstall" ]; then
    [ -n "$ROOT" ] || [ "$(id -u)" = "0" ] || die "Run this as root."
    say "Removing Nextion panel"
    sc disable --now nextion-node
    rm -f "$UNIT" "$BIN"
    sc daemon-reload
    ok "Service and program removed."
    warn "Left in place: /etc/nextion-node.json and the serial-port changes in /boot."
    warn "Backups of the /boot files end in .nextion-bak if you want to restore them."
    exit 0
fi

# ------------------------------------------------------------------ checks
VERSION="$(ver "$HERE/nextion_node.py")"
echo
echo "  GMRS-Link Nextion Node Control Panel - installer   (version ${VERSION:-unknown})"
echo "  gmrs-link.com"
echo "  ------------------------------------------------------------------------"

[ -n "$ROOT" ] || [ "$(id -u)" = "0" ] || die "Run this as root (HamVOIP logs you in as root)."
[ -f "$HERE/nextion_node.py" ] || die "nextion_node.py not found next to install.sh. Copy the whole folder to the node."
[ -n "$VERSION" ] || die "The nextion_node.py in this folder is an old copy. Copy the folder from the new download."

PY=""
for p in python3 python2 python; do
    if command -v "$p" >/dev/null 2>&1; then PY="$(command -v "$p")"; break; fi
done
[ -n "$PY" ] || die "No Python found on this node."
ok "Using $PY ($("$PY" --version 2>&1))"

[ -n "$ROOT" ] || command -v asterisk >/dev/null 2>&1 || warn "asterisk command not found - is this an AllStar node?"

# Stop a running copy so it lets go of the serial port while we work
sc stop nextion-node

# ------------------------------------------------------------------ fresh or keep
say "Settings"
FRESH=1
if [ -f "$CONF" ]; then
    warn "This node already has panel settings (/etc/nextion-node.json)."
    echo "      1) Start fresh - answer all the questions again (old settings are backed up)"
    echo "      2) Keep my settings - reinstall the program, and check weather, Extras and badges"
    ask "Choose 1 or 2" "1"
    if [ "$REPLY" = "2" ]; then
        FRESH=0
    else
        cp "$CONF" "$CONF.bak"
        rm -f "$CONF"
        ok "Old settings saved as /etc/nextion-node.json.bak"
    fi
fi

if [ "$FRESH" = "0" ]; then
    PORT="$(grep -o '"serial_port": *"[^"]*"' "$CONF" | sed 's/.*: *"\(.*\)"/\1/')"
    ok "Keeping existing settings (serial port ${PORT:-/dev/ttyAMA0})."
    PORT="${PORT:-/dev/ttyAMA0}"
else
    say "Your node"
    NODE=""
    while [ -z "$NODE" ]; do
        ask "Your node number" ""
        NODE="$(digits "$REPLY")"
        [ -n "$NODE" ] || warn "Please enter digits only."
    done
    ask "Header text, e.g. your call sign (Enter for \"NODE $NODE\")" ""
    TITLE="$(label "$REPLY")"

    say "Screen connection"
    echo "      1) USB-to-TTL adapter         (/dev/ttyUSB0)"
    echo "      2) Pi GPIO pins 8 and 10      (/dev/ttyAMA0)"
    ask "How is the screen connected? 1 or 2" "2"
    if [ "$REPLY" = "1" ]; then PORT="/dev/ttyUSB0"; else PORT="/dev/ttyAMA0"; fi
    BAUD=9600                          # the GMRS-Link screen file runs at 9600

    say "Screen options"
    ask "Minutes before the screen saver turns the screen off (0 = never)" "10"
    SLEEP_MIN="$(digits "$REPLY")"; SLEEP_MIN="${SLEEP_MIN:-10}"
    ask "Turn the screen on when someone keys up? (y/n)" "y"
    if isyes "$REPLY"; then WAKE=true; else WAKE=false; fi
    ask "Brightness, 10-100" "100"
    BRIGHT="$(digits "$REPLY")"; BRIGHT="${BRIGHT:-100}"
    [ "$BRIGHT" -gt 100 ] 2>/dev/null && BRIGHT=100
    [ "$BRIGHT" -lt 10 ] 2>/dev/null && BRIGHT=10
    ask "Use a 24-hour clock? (y/n)" "n"
    if isyes "$REPLY"; then CLOCK=true; else CLOCK=false; fi

    say "Quick-connect buttons"
    echo "    Six buttons on the main screen. Label = text on the button (up to 10 characters)."
    echo "    Press Enter at the node number to leave a button empty."
    FAVS=""
    for i in 1 2 3 4 5 6; do
        ask "Button $i node number" ""
        FN="$(digits "$REPLY")"
        FL=""
        if [ -n "$FN" ]; then
            ask "Button $i label" "$FN"
            FL="$(label "$REPLY")"
        fi
        [ -n "$FAVS" ] && FAVS="$FAVS,"
        FAVS="$FAVS
    {\"label\": \"$FL\", \"node\": \"$FN\"}"
    done

    mkdir -p "$(dirname "$CONF")"
    cat > "$CONF" <<EOF
{
  "node": "$NODE",
  "title": "$TITLE",
  "serial_port": "$PORT",
  "baud": $BAUD,
  "asterisk": "/usr/sbin/asterisk",
  "poll_seconds": 3,
  "keyed_poll_seconds": 1,
  "sleep_seconds": $((SLEEP_MIN * 60)),
  "wake_on_key": $WAKE,
  "brightness": $BRIGHT,
  "clock_24h": $CLOCK,
  "favorites": [$FAVS
  ]
}
EOF
    ok "Saved /etc/nextion-node.json"
fi

# ------------------------------------------------------------------ weather
say "Weather"
CUR_ZIP="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("weather_zip",""))' "$CONF" 2>/dev/null)"
CUR_UNITS="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("units","F"))' "$CONF" 2>/dev/null)"

WSCRIPT=""
for w in /usr/local/sbin/weather.sh /usr/local/sbin/supermon/weather.sh; do
    if [ -x "$ROOT$w" ]; then WSCRIPT="$w"; break; fi
done

echo "    The header shows the same weather as Supermon, and tapping it opens a"
echo "    3-day forecast. Enter your ZIP code, or - to turn weather off."
ask "ZIP code" "$CUR_ZIP"
if [ "$REPLY" = "-" ]; then
    WZIP=""
else
    WZIP="$(echo "$REPLY" | tr -cd '0-9A-Za-z-' | cut -c1-12)"
fi
ask "Units, F or C" "${CUR_UNITS:-F}"
if [[ "$REPLY" =~ ^[Cc] ]]; then UNITS=C; else UNITS=F; fi

if [ -n "$WZIP" ]; then
    if [ -n "$WSCRIPT" ]; then
        WOUT="$("$ROOT$WSCRIPT" "$WZIP" v 2>/dev/null | tail -1)"
        if [ -n "$WOUT" ] && ! echo "$WOUT" | grep -qi "no report\|usage"; then
            ok "Supermon weather ($WSCRIPT): $WOUT"
        else
            warn "$WSCRIPT gave no report - the screen will use Open-Meteo instead."
        fi
    else
        warn "No Supermon weather.sh found - the screen will use Open-Meteo."
    fi
    GEO="$(PYTHONIOENCODING=ascii:replace "$PY" "$HERE/nextion_node.py" --geocode "$WZIP" 2>/dev/null | tail -1)"
    if [ -n "$GEO" ] && [ "$GEO" != "NOTFOUND" ]; then
        ok "Forecast location: ${GEO##*|}"
    else
        warn "Could not look up $WZIP for the forecast right now (no internet yet?)."
        warn "The panel keeps retrying every 2 minutes."
    fi
fi

"$PY" - "$CONF" "$WZIP" "$UNITS" "${WSCRIPT:-/usr/local/sbin/weather.sh}" <<'PYEOF'
import json, sys
path, zipcode, units, script = sys.argv[1:5]
with open(path) as f:
    cfg = json.load(f)
cfg["weather_zip"] = zipcode
cfg["units"] = units
cfg["weather_script"] = script
cfg["forecast"] = cfg.get("forecast", True)
cfg["weather_source"] = "auto" if zipcode else "off"
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF
if [ -n "$WZIP" ]; then ok "Weather set to $WZIP ($UNITS)"; else ok "Weather turned off"; fi

# ------------------------------------------------------------------ extras page
say "Extras page (ReConn and GPS Tracking)"
echo "    The DTMF command each button sends. Press Enter to use the one shown."
echo "    Type - to leave a button unset."
EXTRA_ARGS=()
for pair in "reconn_on:ReConn Enable" "reconn_off:ReConn Disable" \
            "gps_on:GPS Tracking Enable" "gps_off:GPS Tracking Disable" \
            "gps_update:GPS Tracking Update"; do
    KEY="${pair%%:*}"; NAME="${pair#*:}"
    CUR="$("$PY" -c 'import json,sys; print((json.load(open(sys.argv[1])).get("extras_dtmf") or {}).get(sys.argv[2],""))' "$CONF" "$KEY" 2>/dev/null)"
    if [ -z "$CUR" ]; then
        case "$KEY" in
            reconn_on) CUR="*917" ;; reconn_off) CUR="*918" ;;
            gps_on) CUR="*A50" ;; gps_off) CUR="*A51" ;; gps_update) CUR="*A52" ;;
        esac
    fi
    ask "$NAME DTMF" "$CUR"
    if [ "$REPLY" = "-" ]; then CODE=""; else CODE="$(echo "$REPLY" | tr 'abcd' 'ABCD' | tr -cd '0-9ABCD*#' | cut -c1-24)"; fi
    EXTRA_ARGS+=("$KEY=$CODE")
done
"$PY" - "$CONF" "${EXTRA_ARGS[@]}" <<'PYEOF2'
import json, sys
path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)
codes = cfg.get("extras_dtmf") or {}
for item in sys.argv[2:]:
    key, code = item.split("=", 1)
    codes[key] = code
cfg["extras_dtmf"] = codes
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF2
ok "Extras codes saved"

say "ReConn and GPS status badges"
echo "    Green/red badges on the main screen, read from the programs' flag files:"
echo "      ReConn: $RECONN_FLAG  (exists = disabled)"
echo "      GPS:    $GPS_FLAG  (exists = enabled)"
CUR_R="$("$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])).get("status_files"); print("" if d is None else ((d.get("reconn") or {}).get("path") or "-"))' "$CONF" 2>/dev/null)"
CUR_G="$("$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])).get("status_files"); print("" if d is None else ((d.get("gps") or {}).get("path") or "-"))' "$CONF" 2>/dev/null)"
DEF_R="y"; [ "$CUR_R" = "-" ] && DEF_R="n"
[ -z "$CUR_R" ] && [ ! -d "$ROOT$(dirname "$RECONN_FLAG")" ] && DEF_R="n"
DEF_G="y"; [ "$CUR_G" = "-" ] && DEF_G="n"
ask "Show the ReConn badge? (y/n)" "$DEF_R"
if isyes "$REPLY"; then R_PATH="$RECONN_FLAG"; else R_PATH=""; fi
ask "Show the GPS badge? (y/n)" "$DEF_G"
if isyes "$REPLY"; then G_PATH="$GPS_FLAG"; else G_PATH=""; fi
for pair in "ReConn:$R_PATH:disabled" "GPS Tracking:$G_PATH:enabled"; do
    NAME="${pair%%:*}"; REST="${pair#*:}"; FPATH="${REST%%:*}"; MEANS="${REST##*:}"
    [ -n "$FPATH" ] || continue
    if [ -e "$ROOT$FPATH" ]; then STATE="$MEANS"
    else [ "$MEANS" = "enabled" ] && STATE="disabled" || STATE="enabled"; fi
    ok "$NAME badge will show $(echo "$STATE" | tr a-z A-Z) right now"
done
"$PY" - "$CONF" "$R_PATH" "$G_PATH" <<'PYEOF2'
import json, sys
path, rpath, gpath = sys.argv[1:4]
with open(path) as f:
    cfg = json.load(f)
cfg["status_files"] = {"reconn": {"path": rpath, "present_means": "disabled"},
                       "gps": {"path": gpath, "present_means": "enabled"}}
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF2
ok "Status badges saved"

# ------------------------------------------------------------------ program + service
say "Installing the panel program"
mkdir -p "$(dirname "$BIN")" "$(dirname "$UNIT")"
cp "$HERE/nextion_node.py" "$BIN"
chmod 755 "$BIN"
[ "$(ver "$BIN")" = "$VERSION" ] || die "Copying the program to /usr/local/sbin failed."
ok "Installed version $VERSION to /usr/local/sbin/nextion_node.py"

cat > "$UNIT" <<EOF
[Unit]
Description=Nextion touchscreen control panel for AllStar node
After=network-online.target asterisk.service
Wants=network-online.target asterisk.service

[Service]
Type=simple
ExecStart=$PY /usr/local/sbin/nextion_node.py
Environment=PYTHONIOENCODING=utf-8
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
ok "Created the nextion-node service (starts after the network and Asterisk)"

# ------------------------------------------------------------------ GPIO serial setup
if [ "$PORT" = "/dev/ttyAMA0" ] || [ "$PORT" = "/dev/serial0" ]; then
    say "Setting up the GPIO serial port"
    if [ ! -f "$BOOTCFG" ] || [ ! -f "$CMDLINE" ]; then
        warn "Could not find /boot/config.txt and /boot/cmdline.txt."
        warn "Make sure /boot is mounted, then run this installer again."
    else
        [ -f "$BOOTCFG.nextion-bak" ] || cp "$BOOTCFG" "$BOOTCFG.nextion-bak"
        [ -f "$CMDLINE.nextion-bak" ] || cp "$CMDLINE" "$CMDLINE.nextion-bak"

        if ! grep -q '^enable_uart=1' "$BOOTCFG"; then
            sed -i '/^enable_uart=/d' "$BOOTCFG"
            printf '\n# Nextion screen on GPIO serial\nenable_uart=1\n' >> "$BOOTCFG"
            NEED_REBOOT=1
        fi
        if ! grep -qE '^dtoverlay=(pi3-)?disable-bt' "$BOOTCFG"; then
            echo 'dtoverlay=pi3-disable-bt' >> "$BOOTCFG"
            NEED_REBOOT=1
        fi
        ok "config.txt: serial port on, Bluetooth moved off the GPIO pins"

        if grep -qE 'console=(serial0|ttyAMA0|ttyS0)' "$CMDLINE"; then
            sed -i -E 's/ ?console=(serial0|ttyAMA0|ttyS0),[0-9]+//g' "$CMDLINE"
            NEED_REBOOT=1
            ok "cmdline.txt: removed the serial login console"
        else
            ok "cmdline.txt: no serial console to remove"
        fi
        ok "Original files backed up as config.txt.nextion-bak and cmdline.txt.nextion-bak"

        sc disable --now hciuart
        sc disable --now serial-getty@ttyAMA0.service
        sc disable --now serial-getty@ttyS0.service
    fi
fi

# ------------------------------------------------------------------ enable + start
say "Starting the panel"
sc daemon-reload
sc enable nextion-node
ok "The panel will start automatically at every boot."

if [ "$NEED_REBOOT" = "1" ]; then
    echo
    echo -e "\033[1;33m  A reboot is needed to switch on the serial port.\033[0m"
    echo "  Type:  reboot"
    echo "  After the node restarts, the screen fills in by itself within a minute."
else
    sc restart nextion-node
    if [ -z "$ROOT" ]; then
        sleep 5
        echo
        journalctl -u nextion-node -n 6 --no-pager -o cat 2>/dev/null | sed 's/^/    /'
        echo
        if systemctl is-active --quiet nextion-node; then
            echo -e "\033[1;32m  Done! Panel version $VERSION is running.\033[0m"
        else
            echo -e "\033[31m  The panel did not start - send the lines above to whoever is helping you.\033[0m"
        fi
    fi
fi

cat <<EOF

  If the screen stays on "Waiting for the node to start..." after the reboot:
    Power off and swap the two data wires at Pi pins 8 and 10.

  Useful commands:
    journalctl -u nextion-node -f          watch what the panel is doing
    nano /etc/nextion-node.json            change settings, then:
    systemctl restart nextion-node
    cd /root/nextion-hamvoip
    bash install.sh                        change your answers (choose 2 to keep the rest)
    bash install.sh uninstall              remove the panel

  To update later, run the same one-line command you installed with.
  Help and updates: gmrs-link.com

EOF
