#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Installs OpenROAD and the Sky130HD platform files into a user-local prefix.
#
# There is no root on this machine and OpenROAD is not packaged in Ubuntu's
# archive, so instead of `apt install` we extract the official prebuilt
# Ubuntu 22.04 .deb into $OR_PREFIX with `dpkg-deb -x` and put a launcher on
# PATH that points the dynamic loader at the extracted libraries. Nothing is
# written outside $HOME, so undoing the whole thing is `rm -rf ~/opt/openroad
# ~/opt/orfs ~/.local/bin/openroad`.
#
# Usage:  ./scripts/setup_openroad.sh
# ---------------------------------------------------------------------------
set -euo pipefail

OR_PREFIX="${OR_PREFIX:-$HOME/opt/openroad}"
ORFS_DIR="${ORFS_DIR:-$HOME/Desktop/OpenROAD-flow-scripts}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
DL_DIR="${DL_DIR:-$HOME/opt/dl}"

OR_DEB_URL="https://github.com/Precision-Innovations/OpenROAD/releases/download/2024-12-14/openroad_2.0-17598-ga008522d8_amd64-ubuntu-22.04.deb"

# The .deb links against Qt5, tclreadline and a few small libraries that are
# not installed here. They are all in Ubuntu's archive, so `apt-get download`
# (which needs no root, it just fetches into the cwd) plus `dpkg-deb -x` into
# the same prefix satisfies the loader.
RUNTIME_DEPS=(
    libqt5charts5 libqt5widgets5t64 libqt5gui5t64 libqt5core5t64
    tcl-tclreadline libdouble-conversion3 libmd4c0 libpcre2-16-0
)

echo "==> Installing OpenROAD into $OR_PREFIX"
mkdir -p "$DL_DIR/deps" "$OR_PREFIX" "$BIN_DIR"

if [ ! -f "$DL_DIR/openroad.deb" ]; then
    echo "    downloading OpenROAD .deb (~55 MB)"
    curl -fsSL -o "$DL_DIR/openroad.deb" "$OR_DEB_URL"
fi
dpkg-deb -x "$DL_DIR/openroad.deb" "$OR_PREFIX"

echo "==> Fetching runtime libraries"
(cd "$DL_DIR/deps" && apt-get download "${RUNTIME_DEPS[@]}" >/dev/null)
for deb in "$DL_DIR"/deps/*.deb; do
    dpkg-deb -x "$deb" "$OR_PREFIX"
done

echo "==> Writing launcher to $BIN_DIR/openroad"
cat > "$BIN_DIR/openroad" <<EOF
#!/bin/sh
# OpenROAD launcher: the binary was extracted from the Ubuntu 22.04 .deb into
# a user-local prefix (no root), so its shared libraries -- Qt5, tclreadline,
# or-tools -- are not on the default loader path. Point the loader at them,
# then exec the real binary.
OR_PREFIX="$OR_PREFIX"
LD_LIBRARY_PATH="\$OR_PREFIX/usr/lib/x86_64-linux-gnu:\$OR_PREFIX/usr/lib:\$OR_PREFIX/opt/or-tools/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export LD_LIBRARY_PATH

# Qt hunts for its platform plugins (including the offscreen one the headless
# layout render needs) at the path baked in at build time, which does not
# exist under this relocated prefix. Without this it aborts with GUI-0077
# "could not create platform integration" instead of returning an error.
QT_PLUGIN_PATH="\$OR_PREFIX/usr/lib/x86_64-linux-gnu/qt5/plugins\${QT_PLUGIN_PATH:+:\$QT_PLUGIN_PATH}"
QT_QPA_PLATFORM_PLUGIN_PATH="\$OR_PREFIX/usr/lib/x86_64-linux-gnu/qt5/plugins/platforms"
export QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH

exec "\$OR_PREFIX/usr/bin/openroad" "\$@"
EOF
chmod +x "$BIN_DIR/openroad"

# The Sky130HD platform bundle (tech LEF, merged std-cell LEF, Liberty, plus
# the track/tapcell/PDN/RC tcl fragments the flow sources) lives in
# OpenROAD-flow-scripts. A blobless sparse checkout pulls only that ~20 MB
# directory instead of the multi-gigabyte full repo.
#
# sky130hs is in the pattern list for one file: sky130hd's OpenRCX extraction
# rules are a symlink into that sibling platform, so checking out sky130hd
# alone leaves rcx_patterns.rules dangling and parasitic extraction fails.
# --no-cone is what allows naming that single file rather than the whole
# platform directory.
echo "==> Using existing OpenROAD-flow-scripts at $ORFS_DIR"

if [ ! -d "$ORFS_DIR/.git" ]; then
    echo "ERROR: OpenROAD-flow-scripts repository not found:"
    echo "       $ORFS_DIR"
    echo
    echo "Expected repository:"
    echo "       $HOME/Desktop/OpenROAD-flow-scripts"
    exit 1
fi

if [ ! -f "$ORFS_DIR/flow/platforms/sky130hd/sky130hd.lyt" ]; then
    echo "ERROR: Sky130HD platform not found."
    exit 1
fi

if [ ! -f "$ORFS_DIR/flow/platforms/sky130hd/gds/sky130_fd_sc_hd.gds" ]; then
    echo "ERROR: Sky130HD standard-cell GDS not found."
    exit 1
fi

if [ ! -f "$ORFS_DIR/flow/util/def2stream.py" ]; then
    echo "ERROR: def2stream.py not found."
    exit 1
fi

if [ ! -f "$ORFS_DIR/flow/util/generate_klayout_tech.py" ]; then
    echo "ERROR: generate_klayout_tech.py not found."
    exit 1
fi

echo "Sky130HD platform verified."