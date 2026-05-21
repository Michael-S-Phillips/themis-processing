#!/usr/bin/env bash
# Build Davinci from ASU's SVN tag into a user-prefix install (no sudo needed).
# Pulls both the core (davinci) and library (davinci_library) modules.
#
# Outputs:
#   $PREFIX/bin/davinci
#   $PREFIX/share/davinci/library/*
#
# Activate the conda env that has subversion + gcc + autoconf first, e.g.:
#   conda activate base   # if you added build tools there
# Then:
#   ./build_davinci.sh
# After install, source the env file:
#   . $PREFIX/etc/davinci.env

set -euo pipefail

VERSION="${DV_VERSION:-3_01}"
PREFIX="${DV_PREFIX:-$HOME/local/davinci}"
SRC_ROOT="${DV_SRC_ROOT:-$HOME/src/davinci}"
SVN_BASE="https://oss.mars.asu.edu/svn/davinci"

mkdir -p "$SRC_ROOT" "$PREFIX"

if ! command -v svn >/dev/null; then
    echo "ERROR: svn not found. Install with: conda install -n base -c conda-forge subversion" >&2
    exit 1
fi
if ! command -v gcc >/dev/null; then
    echo "ERROR: gcc not found. Install with: conda install -n base -c conda-forge gcc_linux-64 gxx_linux-64 make autoconf automake libtool pkg-config bison flex" >&2
    exit 1
fi

echo "==> Checking out davinci core dv_${VERSION}"
if [[ ! -d "$SRC_ROOT/davinci_core" ]]; then
    svn co --non-interactive --trust-server-cert "$SVN_BASE/davinci/tags/dv_${VERSION}" "$SRC_ROOT/davinci_core"
else
    svn up --non-interactive --trust-server-cert "$SRC_ROOT/davinci_core"
fi

echo "==> Checking out davinci library dv_${VERSION}"
if [[ ! -d "$SRC_ROOT/davinci_library" ]]; then
    svn co --non-interactive --trust-server-cert "$SVN_BASE/davinci_library/tags/dv_${VERSION}" "$SRC_ROOT/davinci_library"
else
    svn up --non-interactive --trust-server-cert "$SRC_ROOT/davinci_library"
fi

echo "==> Building davinci core"
cd "$SRC_ROOT/davinci_core"
if [[ ! -f configure ]]; then
    autoreconf -fvi
fi

# The Davinci 3.0.1 sources include K&R-style C and implicit declarations,
# which gcc 14+ rejects by default. Use gnu89 + permissive flags so the
# sources compile unchanged on a modern toolchain.
make clean >/dev/null 2>&1 || true
./configure --prefix="$PREFIX" --enable-static-deps=no \
    CPPFLAGS="-I$CONDA_PREFIX/include -I$CONDA_PREFIX/include/cfitsio -DNDEBUG -D_FORTIFY_SOURCE=2 -O2" \
    LDFLAGS="-L$CONDA_PREFIX/lib -Wl,-rpath,$CONDA_PREFIX/lib" \
    CFLAGS="-std=gnu89 -Wno-error -Wno-implicit-int -Wno-implicit-function-declaration -Wno-incompatible-pointer-types -Wno-int-conversion -Wno-return-mismatch -fcommon -march=nocona -mtune=haswell -ftree-vectorize -fPIC -fstack-protector-strong -fno-plt -O2 -ffunction-sections -pipe"

make -j"$(nproc)"
make install

echo "==> Installing davinci library"
LIBDIR="$PREFIX/share/davinci/library"
mkdir -p "$LIBDIR"
cp -a "$SRC_ROOT/davinci_library"/* "$LIBDIR/"

echo "==> Writing env file"
mkdir -p "$PREFIX/etc"
cat > "$PREFIX/etc/davinci.env" <<EOF
# Source this to put Davinci on PATH and configure runtime locations.
export DV_HOME="$PREFIX/share/davinci"
export DV_LIB="$LIBDIR"
export DV_SCRIPT_FILES="$LIBDIR/script_files"
export DV_OS="linux"
export DV_VIEWER="\${DV_VIEWER:-$(command -v xdg-open || command -v eog || echo display)}"
export PATH="$PREFIX/bin:$LIBDIR/bin:\$PATH"
EOF

echo "==> Done. Source $PREFIX/etc/davinci.env then run: davinci -v"
