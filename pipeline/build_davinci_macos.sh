#!/usr/bin/env bash
# Build Davinci 3.01 + its dv_3_01 library on macOS (Apple Silicon, native arm64) into a user
# prefix. This is the recipe that worked on the Mac mini on 2026-11-16; the Linux equivalent is
# build_davinci.sh.
#
#   bash build_davinci_macos.sh            # needs the `davinci` conda env (created below if missing)
#   source ~/local/davinci/etc/davinci.env
#
# What differs from Linux:
#   - Apple clang instead of gcc, with the same permissive flags for the K&R-era sources.
#   - Davinci's config.guess/config.sub predate arm64 Macs ("-apple-darwin" -> configure fails);
#     they are replaced with automake's current copies.
#   - Dependencies come from an osx-arm64 conda env `davinci`; ISIS (x86_64, Rosetta) is a
#     separate env and is only used at runtime for gdalinfo/gdal_translate (DV_GDAL_PATH).
#   - ASU's SVN server drops connections on large checkouts, so every svn step retries.
#   - The davinci ISIS3 I/O module (iomod_isis3, needed by thm_pre/post_process.dv) is NOT
#     built: its build scripts target ISIS 3.3-3.5. Chain B (v2 RDR) does not need it.
set -euo pipefail

VERSION="${DV_VERSION:-3_01}"
PREFIX="${DV_PREFIX:-$HOME/local/davinci}"
SRC_ROOT="${DV_SRC_ROOT:-$HOME/src/davinci}"
DEPS_ENV="${DV_DEPS_ENV:-davinci}"
ISIS_ENV_BIN="${DV_GDAL_PATH:-$HOME/anaconda3/envs/isis/bin}"
SVN_BASE="https://oss.mars.asu.edu/svn/davinci"
SVN_OPTS="--non-interactive --trust-server-cert -q"

source "$(conda info --base)/etc/profile.d/conda.sh"
if ! conda env list | awk '{print $1}' | grep -qx "$DEPS_ENV"; then
    conda create -y -n "$DEPS_ENV" -c conda-forge --solver=libmamba subversion autoconf automake \
        libtool pkg-config bison flex make readline ncurses "hdf5=1.14" libpng zlib libxml2 cfitsio libcurl xz
fi
conda activate "$DEPS_ENV"
P="$CONDA_PREFIX"

retry() { for i in $(seq 1 15); do "$@" && return 0; echo "  retry $i: $*" >&2; sleep 20; done; return 1; }

mkdir -p "$SRC_ROOT" "$PREFIX"
cd "$SRC_ROOT"
echo "==> davinci core dv_${VERSION} (+ externals: iomedley, library trunk, iomod_isis3)"
[ -d davinci_core/.svn ] || svn co $SVN_OPTS "$SVN_BASE/davinci/tags/dv_${VERSION}" davinci_core || true
retry sh -c "svn cleanup davinci_core; svn up $SVN_OPTS davinci_core"
echo "==> davinci library dv_${VERSION}"
[ -d davinci_library/.svn ] || svn co $SVN_OPTS "$SVN_BASE/davinci_library/tags/dv_${VERSION}" davinci_library || true
retry sh -c "svn cleanup davinci_library; svn up $SVN_OPTS davinci_library"

echo "==> refresh config.guess / config.sub (arm64 Darwin)"
cd "$SRC_ROOT/davinci_core"
NEW=$(ls -d "$P"/share/automake-*/ | tail -1)
find . -path '*/.svn' -prune -o \( -name config.guess -o -name config.sub \) -print | while read -r f; do
    cp "$NEW/$(basename "$f")" "$f"
done

echo "==> configure"
./configure --prefix="$PREFIX" --with-hdf5="$P" --with-readline="$P" --with-cfitsio="$P" --with-libxml2="$P" \
    CC=clang CXX=clang++ \
    CPPFLAGS="-I$P/include -I$P/include/libxml2 -DNDEBUG" \
    LDFLAGS="-L$P/lib -Wl,-rpath,$P/lib" \
    CFLAGS="-std=gnu89 -O2 -fcommon -Wno-error -Wno-implicit-int -Wno-implicit-function-declaration -Wno-incompatible-pointer-types -Wno-int-conversion -Wno-return-type -Wno-deprecated-non-prototype -Wno-incompatible-function-pointer-types" \
    CXXFLAGS="-O2 -std=gnu++11"

echo "==> build (single job: parallel make races libdavinci.la against the davinci link)"
make
make install

echo "==> install library dv_${VERSION} over the trunk copy (matches the WSL setup)"
LIBDIR="$PREFIX/share/davinci/library"
mkdir -p "$LIBDIR"
cp -a "$SRC_ROOT/davinci_library/." "$LIBDIR/"
rm -rf "$LIBDIR/.svn"

echo "==> env file"
mkdir -p "$PREFIX/etc"
cat > "$PREFIX/etc/davinci.env" <<EOF
# Davinci ${VERSION/_/.} (native arm64 build). Source this, or let themis_pipeline.py source it
# (THEMIS_DAVINCI_ENV overrides the path).
export DV_HOME="$PREFIX/share/davinci"
export DV_LIB="$LIBDIR"
export DV_SCRIPT_FILES="$LIBDIR/script_files"
export DV_MOD_PATH="$PREFIX/lib/davinci"
export DV_OS="mac"
export DV_VIEWER="\${DV_VIEWER:-open}"
# gdalinfo/gdal_translate for read_geo(): the ISIS env's GDAL, not MacPorts /opt/local.
export DV_GDAL_PATH="\${DV_GDAL_PATH:-$ISIS_ENV_BIN}"
export PATH="$PREFIX/bin:$LIBDIR/bin:\$PATH"
EOF
echo "==> done: source $PREFIX/etc/davinci.env; davinci -v"
