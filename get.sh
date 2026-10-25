#!/bin/bash
#
# get.sh - one-line installer / updater for the GMRS-Link Nextion Node Control Panel
#
#   curl -sSL https://raw.githubusercontent.com/__GITHUB_REPO__/main/get.sh | bash
#
# Downloads the latest version from GitHub into /root/nextion-hamvoip, then runs
# the installer on a new node or the updater on a node that already has the panel.
# gmrs-link.com

REPO="${NEXTION_REPO:-__GITHUB_REPO__}"
BRANCH="${NEXTION_BRANCH:-main}"
URL="${NEXTION_URL:-https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz}"
DEST="${NEXTION_DEST:-/root/nextion-hamvoip}"
TEST="${NEXTION_TEST:-}"               # only used for testing

c_ok()   { echo -e "  \033[32m$*\033[0m"; }
c_warn() { echo -e "  \033[33m$*\033[0m"; }
die()    { echo -e "\n  \033[31mERROR: $*\033[0m\n"; exit 1; }

# Keyboard input even though this script arrives through a pipe
IN=/dev/stdin
if [ ! -t 0 ] && ( : < /dev/tty ) 2>/dev/null; then IN=/dev/tty; fi

echo
echo "  ============================================================"
echo "     GMRS-Link Nextion Node Control Panel        gmrs-link.com"
echo "  ============================================================"
echo

[ -n "$TEST" ] || [ "$(id -u)" = "0" ] || die "Run this as root (HamVOIP logs you in as root)."
command -v tar >/dev/null 2>&1 || die "tar is not installed on this node."

TMP="$(mktemp -d 2>/dev/null || echo /tmp/nextion-get.$$)"
mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

echo "  Downloading the latest version..."
if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 2 --max-time 120 "$URL" -o "$TMP/panel.tar.gz"
    GOT=$?
elif command -v wget >/dev/null 2>&1; then
    wget -q -T 120 -O "$TMP/panel.tar.gz" "$URL"
    GOT=$?
else
    die "Neither curl nor wget is installed on this node."
fi
if [ "$GOT" != "0" ] || [ ! -s "$TMP/panel.tar.gz" ]; then
    echo
    c_warn "The download failed. Check that the node is on the internet:"
    c_warn "    ping -c 2 github.com"
    c_warn "If it is, you can install from a PC instead: download the ZIP from"
    c_warn "    https://github.com/$REPO"
    c_warn "unzip it, copy the folder to /root/nextion-hamvoip on the node with"
    c_warn "WinSCP (port 222), then run:  cd /root/nextion-hamvoip && bash install.sh"
    die "Could not download $URL"
fi

tar -xzf "$TMP/panel.tar.gz" -C "$TMP" 2>/dev/null || die "The download was damaged - run the command again."
SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
[ -f "$SRC/nextion_node.py" ] && [ -f "$SRC/install.sh" ] || die "The download doesn't contain the panel files."

# Scripts edited on Windows may have Windows line endings - bash can't run those
for f in "$SRC"/*.sh "$SRC"/*.py "$SRC"/*.service; do
    [ -f "$f" ] && sed -i 's/\r$//' "$f"
done

VERSION="$(grep -m1 '^VERSION = ' "$SRC/nextion_node.py" | sed 's/^VERSION = "\(.*\)"/\1/')"
c_ok "Downloaded version ${VERSION:-unknown}"

# Replace the old copy of the folder, keeping one backup
if [ -d "$DEST" ]; then
    rm -rf "$DEST.old"
    mv "$DEST" "$DEST.old"
fi
mkdir -p "$DEST"
cp -r "$SRC"/. "$DEST"/
chmod +x "$DEST"/*.sh
c_ok "Files saved in $DEST"

cd "$DEST" || die "Could not open $DEST"
if [ -n "$TEST" ]; then
    INSTALLED="$TEST_INSTALLED"
elif [ -f /usr/local/sbin/nextion_node.py ] && [ -f /etc/nextion-node.json ]; then
    INSTALLED=1
fi

if [ -n "$INSTALLED" ]; then
    echo
    echo "  The panel is already installed on this node."
    echo "    1) Update to version ${VERSION:-new} and keep my settings"
    echo "    2) Run the installer to change my settings"
    read -r -p "  Choose 1 or 2 [1]: " CH < "$IN"
    if [ "$CH" = "2" ]; then
        [ -n "$TEST" ] && { echo "  (test) would run install.sh"; exit 0; }
        exec bash "$DEST/install.sh" < "$IN"
    else
        [ -n "$TEST" ] && { echo "  (test) would run update.sh"; exit 0; }
        exec bash "$DEST/update.sh" < "$IN"
    fi
else
    [ -n "$TEST" ] && { echo "  (test) would run install.sh"; exit 0; }
    exec bash "$DEST/install.sh" < "$IN"
fi
