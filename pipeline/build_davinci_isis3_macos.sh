#!/usr/bin/env bash
# Build an x86_64 Davinci 3.01 plus the davinci ISIS3 I/O module (isis3.dvio) ported to ISIS 9,
# for the runbook chain (A. D. Rogers' thm_pre_process.dv / thm_post_process.dv, which need
# insmod("isis3")). Recipe that worked on the Mac mini on 2026-11-17.
#
#   bash build_davinci_isis3_macos.sh     # needs: conda envs `isis` (ISIS 9, osx-64) and `davinci`
#                                         # (build tools), and the arm64 build's source tree
#   themis_pipeline.py picks it up via THEMIS_DAVINCI_RUNBOOK_ENV
#   (default ~/local/davinci-x86/etc/davinci.env) for the two runbook steps only.
#
# Why a second davinci: the module is loaded into the davinci process, so it must have the same
# architecture as both davinci and libisis. ISIS 9 is published for osx-64 only (osx-arm64 starts
# at ISIS 10), so this build is x86_64 and runs under Rosetta. Davinci links the `isis` env's own
# HDF5/readline/cfitsio/Qt so only one copy of each library is loaded.
#
# Module port (iomod_isis3/iomod_isis3.9.C, from davinci trunk iomod_isis3.5.C):
#   - History: History(name) + Cube::read(Blob&) -> Cube::readHistory(name);
#              Cube::write(History&) -> Cube::write(History&, name)
#   - Table:   Table(name) + Cube::read(Blob&) -> Cube::readTable(name)
#   - fixed the 3.5 reader's SignedWord fall-through (16-bit cubes were read as float)
#   - pixel types other than UnsignedByte/SignedWord/Real -> float from the double buffer
#   - INT64 (not in davinci 3.01) -> USHORT
# Verified: load_pds/write(type=isis3) round trip of a projected THEMIS lev2 cube is exact, and
# ISIS 9 `stats` on the davinci-written cube matches the original.
set -euo pipefail

PREFIX="${DV_X86_PREFIX:-$HOME/local/davinci-x86}"
SRC_ARM="${DV_SRC_ROOT:-$HOME/src/davinci}/davinci_core"      # from build_davinci_macos.sh
BUILD="${DV_X86_SRC:-$HOME/src/davinci-x86}"
HERE="$(cd "$(dirname "$0")" && pwd)"

source "$(conda info --base)/etc/profile.d/conda.sh"
P="$(conda run -n isis printenv CONDA_PREFIX)"                # x86_64 libs + ISIS 9
conda activate davinci                                         # autotools, make, bison, flex

echo "==> copy source tree (no build products)"
mkdir -p "$BUILD"
rsync -a --delete --exclude='.svn' "$SRC_ARM/" "$BUILD/davinci_core/"
cd "$BUILD/davinci_core"
find . \( -name '*.o' -o -name '*.lo' -o -name '*.la' -o -name '*.a' -o -name '*.so' -o -name '*.dylib' -o -name '*.dvio' \) -delete
find . -name .libs -type d -prune -exec rm -rf {} +
rm -f davinci config.status config.log config.h

echo "==> configure (x86_64, deps from the isis env)"
arch -x86_64 /bin/bash -c "./configure --prefix=$PREFIX --build=x86_64-apple-darwin --host=x86_64-apple-darwin \
  --with-hdf5=$P --with-readline=$P --with-cfitsio=$P --with-libxml2=$P \
  CC='clang -arch x86_64' CXX='clang++ -arch x86_64' \
  CPPFLAGS='-I$P/include -I$P/include/libxml2 -DNDEBUG' \
  LDFLAGS='-arch x86_64 -L$P/lib -Wl,-rpath,$P/lib' PKG_CONFIG_PATH=$P/lib/pkgconfig \
  CFLAGS='-std=gnu89 -O2 -fcommon -Wno-error -Wno-implicit-int -Wno-implicit-function-declaration -Wno-incompatible-pointer-types -Wno-int-conversion -Wno-return-type -Wno-deprecated-non-prototype -Wno-incompatible-function-pointer-types' \
  CXXFLAGS='-O2 -std=gnu++11'"

echo "==> build (single job) + install"
arch -x86_64 /bin/bash -c "make && make install"

echo "==> isis3.dvio (ISIS 9)"
D="$BUILD/davinci_core"
clang++ -arch x86_64 -std=c++17 -fPIC -O2 -w -DHAVE_CONFIG_H -I"$D" -I"$D/libltdl" -I"$D/iomedley" \
  -I"$P/include/isis" -I"$P/include" -I"$P/include/qt" -I"$P/include/qt/QtCore" -I"$P/include/qt/QtXml" \
  -c "$HERE/iomod_isis3/iomod_isis3.9.C" -o "$BUILD/iomod_isis3.9.o"
mkdir -p "$PREFIX/lib/davinci"
clang++ -arch x86_64 -bundle -undefined dynamic_lookup -o "$PREFIX/lib/davinci/isis3.dvio" \
  "$BUILD/iomod_isis3.9.o" -L"$P/lib" -lisis -lQt5Core -lQt5Xml -Wl,-rpath,"$P/lib"

echo "==> library dv_3_01 (same as the arm64 install)"
rsync -a "$HOME/local/davinci/share/davinci/library/" "$PREFIX/share/davinci/library/"

echo "==> env file"
mkdir -p "$PREFIX/etc"
cat > "$PREFIX/etc/davinci.env" <<EOF
# x86_64 davinci (Rosetta) with the ISIS 9 isis3 I/O module, for the runbook chain (C).
# Built by build_davinci_isis3_macos.sh. Source after \`conda activate isis\` (needs ISISROOT).
export DV_HOME="$PREFIX/share/davinci"
export DV_LIB="$PREFIX/share/davinci/library"
export DV_SCRIPT_FILES="$PREFIX/share/davinci/library/script_files"
export DV_MOD_PATH="$PREFIX/lib/davinci"
export DV_OS="mac"
export DV_VIEWER="\${DV_VIEWER:-open}"
export DV_GDAL_PATH="\${DV_GDAL_PATH:-$P/bin}"
export PATH="$PREFIX/bin:$PREFIX/share/davinci/library/bin:\$PATH"
EOF
echo "==> done. Test: conda activate isis; source $PREFIX/etc/davinci.env; echo 'insmod(\"isis3\"); lsmod(\"isis3\")' | davinci -q"
