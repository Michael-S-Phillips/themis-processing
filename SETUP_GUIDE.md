# THEMIS Processing — Setup Guide

This documents **how the pipeline at `/mnt/themis-processing/` was built up from a blank Ubuntu 22.04 WSL2 environment.** It captures the install commands, the dead ends we hit, and the three nontrivial bugs that delayed the atmospheric-correction step. If you ever need to reproduce this on a fresh box, or if part of it breaks, this is the reference.

For day-to-day usage, see `USER_GUIDE.md`. For the abbreviated overview, see `CLAUDE.md`.

## Starting state vs. target

| | Original JHU/APL setup (per `Get themis processing up and running.docx`) | Current setup |
| --- | --- | --- |
| OS | RHEL 7 VM | Ubuntu 22.04 (WSL2) |
| ISIS | 3.4, tarball from `/project/crism/users/phillms1/isis3` | 9.0.0, conda-forge / usgs-astrogeology |
| Davinci | 2.27, RHEL RPM | 3.01, built from SVN |
| Mineral / atm libs | `/project/crism/users/phillms1/.../PDART_mineral_library.hdf` etc. | `/mnt/themis-processing/atm_steps/` (the user copied them in) |
| Sudo | Free root | Password-required; everything user-prefix |
| Workspace storage | Local | SMB share at `\\gigas\Mars_GIS\themis-processing` mounted at `/mnt/themis-processing/` |

## Environment constraints (read first)

Two facts about this WSL2 box shaped every install decision:

1. **`sudo` requires a password.** No `apt install`, no `dpkg -i`, no system-wide package operations could be automated. **Everything had to live in user-prefix locations** — conda envs under `~/miniconda3/envs/`, Davinci under `~/local/davinci/`, SPICE kernels under `~/isis_data/`.
2. **`/mnt/themis-processing` is an SMB share.** `chmod` returns "Operation not permitted." Cannot mark scripts executable; always invoke via `bash script.sh` or `python3 script.py`. Also: read/write to this share is 5–10× slower than to `/`, so put builds, SPICE kernels, and the conda package cache on local disk (`~`), not on the share.

## Part 1 — ISIS install

### What we tried first that didn't work

`conda create -n isis -c conda-forge isis` fails with `PackagesNotFoundError: isis`. ISIS isn't on conda-forge directly — it's on the USGS-Astrogeology channel.

`conda create -n isis -c conda-forge -c usgs-astrogeology isis -y` runs but the classic solver in `conda 23.7.2` hit OOM (~4 GB RAM) after ~13 minutes solving the dependency graph and got SIGKILLed. The solver has trouble with ISIS's broad C++ dependency tree.

### What worked

Install the libmamba solver first:

```bash
~/miniconda3/bin/conda install -n base -c conda-forge conda-libmamba-solver -y
```

Then ask conda to use it explicitly. The same install command finishes in ~2 minutes with libmamba:

```bash
~/miniconda3/bin/conda create -n isis -c conda-forge -c usgs-astrogeology --solver=libmamba \
    isis rasterio pillow -y
```

Adding `rasterio` and `pillow` at create time gives us the Python DCS fallback for free.

### Activation hooks

ISIS reads two env vars at startup: `ISISROOT` (= the conda env prefix) and `ISISDATA` (where SPICE kernels live). The conda activate.d scripts that ship with the ISIS package set GDAL but not the ISIS vars themselves. Adding our own activation hook:

```bash
mkdir -p ~/miniconda3/envs/isis/etc/conda/activate.d/
cat > ~/miniconda3/envs/isis/etc/conda/activate.d/isis-vars.sh <<'EOF'
export ISISROOT="$CONDA_PREFIX"
export ISISDATA="$HOME/isis_data"
export ALESPICEROOT="$HOME/isis_data"
EOF
```

Now `conda activate isis` puts `thm2isis`, `spiceinit`, `cam2map` etc. on `PATH` with all env vars set.

### SPICE kernels

ISIS doesn't ship the per-mission SPICE kernels. They live on a USGS S3 bucket and are pulled by `downloadIsisData`. The full ISIS data set is ~500 GB, but for THEMIS on Mars Odyssey we only need two collections (`base` + `odyssey`) — together ~28 GB:

```bash
mkdir -p ~/isis_data
conda activate isis
downloadIsisData base $ISISDATA       # ~19 GB
downloadIsisData odyssey $ISISDATA    # ~9 GB
```

These ran in parallel via `rclone` under the hood — the UA network finished both in ~3 minutes. If `spiceinit` later complains about a missing `kernels.????.db`, the relevant collection didn't complete; just re-run that one `downloadIsisData` line.

## Part 2 — Davinci install

### What we tried first that didn't work

**The Ubuntu .deb** at `http://davinci.asu.edu/index.php?title=Download_For_Ubuntu` is gated behind a form. Curling the URL returns the form HTML, not the binary. Submitting the form via curl `-F` got a 411 / "Invalid Request" response — automation couldn't pass the form successfully.

**`apt-cache search davinci`** returns nothing. Davinci is not in Ubuntu's package archive.

**`alien` on the RHEL `.rpm`** — would need `sudo` to install the resulting .deb. Off the table.

### What worked: build from SVN

The Davinci source is in a public SVN at `https://oss.mars.asu.edu/svn/davinci/` with two repos (core + library) under `/tags/dv_3_01/`. There's no signup gate on the SVN.

Install build tools (svn + compilers + autotools) in conda base:

```bash
conda install -n base -c conda-forge --solver=libmamba \
    subversion gcc_linux-64 gxx_linux-64 make autoconf automake libtool pkg-config bison flex -y
```

Plus dev libraries for HDF5/HDF4/CFITSIO (needed for HDF mineral lib reading and FITS handoff):

```bash
conda install -n base -c conda-forge --solver=libmamba hdf5 hdf4 cfitsio readline libxml2 libpng zlib -y
```

Check out both Davinci repos:

```bash
mkdir -p ~/src/davinci && cd ~/src/davinci
svn co --non-interactive --trust-server-cert \
    https://oss.mars.asu.edu/svn/davinci/davinci/tags/dv_3_01 davinci_core
svn co --non-interactive --trust-server-cert \
    https://oss.mars.asu.edu/svn/davinci/davinci_library/tags/dv_3_01 davinci_library
```

### Configure + build (the bug-fix flags matter)

Davinci 3.0.1 source is ~2019-vintage C; it has K&R-style function signatures, implicit `int` returns, and other patterns that **gcc 13+ rejects by default**. The conda-installed toolchain here is gcc 15.2. Without permissive flags the build fails on `iomedley/io_png.c` (implicit declaration of `png_set_expand_gray_1_2_4_to_8`) and `modules/thm/mod_thm.c` (`return type defaults to 'int'`).

The flags that worked:

```bash
cd ~/src/davinci/davinci_core
make clean
./configure --prefix=$HOME/local/davinci --enable-static-deps=no \
    CPPFLAGS="-I$CONDA_PREFIX/include -I$CONDA_PREFIX/include/cfitsio -DNDEBUG -D_FORTIFY_SOURCE=2 -O2" \
    LDFLAGS="-L$CONDA_PREFIX/lib -Wl,-rpath,$CONDA_PREFIX/lib" \
    LIBS="-lpng -lz" \
    CFLAGS="-std=gnu89 -Wno-error -Wno-implicit-int -Wno-implicit-function-declaration \
            -Wno-incompatible-pointer-types -Wno-int-conversion -Wno-return-mismatch -fcommon \
            -march=nocona -mtune=haswell -ftree-vectorize -fPIC -fstack-protector-strong \
            -fno-plt -O2 -ffunction-sections -pipe"
```

Configure summary should show `HDF5: Yes`, `FITS: Yes`, `Readline: Yes`, `Module Support: Yes`. (libisis stays "No" — Davinci would need to link against the ISIS C++ API, which has drifted between ISIS 3.4 and ISIS 9; the FITS bridge via `isis2fits` is the workaround if Davinci ever needs to read a cube it can't open natively. Turns out `read_geo()` reads ISIS cubes via GDAL anyway, so libisis is not actually required.)

`LIBS="-lpng -lz"` matters: Davinci's bundled iomedley uses libpng but the auto-generated link command doesn't include it, so the final binary fails to link with undefined `png_*` references. Forcing it via LIBS works around this.

Now build — **`make -j8` has a race**: the davinci binary tries to link against `libdavinci.la` before that library finishes building. Safer:

```bash
make libdavinci.la   # build the library first
make -j8             # then the rest in parallel
make install
```

This produces `~/local/davinci/bin/davinci` plus the bundled library tree at `~/local/davinci/share/davinci/library/`. The library tree includes the THEMIS-specific reference data we need (more on this below).

This is captured in `pipeline/build_davinci.sh` so you can re-run it on a fresh box.

### Environment file

The runbook expects a bunch of `DV_*` env vars. Wrote one env file that the python pipeline auto-sources at `~/local/davinci/etc/davinci.env`:

```bash
export DV_HOME="$HOME/local/davinci/share/davinci"
export DV_LIB="$DV_HOME/library"
export DV_SCRIPT_FILES="$DV_LIB/script_files"
export DV_OS="linux"
export DV_VIEWER="${DV_VIEWER:-display}"
export PATH="$HOME/local/davinci/bin:$DV_LIB/bin:$PATH"

# critical for read_geo / shape2georaster
export DV_GDAL_PATH="${DV_GDAL_PATH:-$HOME/miniconda3/envs/isis/bin}"
```

`DV_GDAL_PATH` is the non-obvious one. Davinci's `read_geo()` and `shape2georaster()` (defined in `map_projection.dvrc`) shell out to `gdalinfo` and `gdal_translate` at `$DV_GDAL_PATH/`. The default is `/usr/bin/`, but we have no system gdal — we point it at the ISIS conda env's GDAL, which is a recent and consistent build.

### The "library" — public, not proprietary

The runbook's references to `/project/crism/users/phillms1/.../themis_atm_support/PDART_mineral_library.hdf` and similar paths gave the impression those were JHU/APL-internal files. **They're not.** The Davinci library SVN at `davinci_library/tags/dv_3_01/script_files/themis_atm_support/` ships:

- `TES_atmlib_73channel.hdf` — the 73-channel TES atmospheric library
- `THEMIS_ATM_mineral_library.hdf` — the THEMIS spectral mineral library
- `temp_rad_v4` (in the parent `instrument_parameters/` directory) — the brightness-temperature lookup table

`make install` deposits all of these at `~/local/davinci/share/davinci/library/script_files/`. The `themis_atm.dvrc` script that defines `themis_tes_overlay` and `themis_atm_corr` is also public.

This is important to know if you're ever trying to reproduce the runbook's atm correction without access to JHU/APL filesystems: you don't need their files. The user-supplied files in `/mnt/themis-processing/atm_steps/` (PDART_mineral_library.hdf, TES_atmlib.hdf, themis_tes_overlay.dav, themis_correct.dav) are Dr Rogers' specific variants with slightly different parameters and filenames — same algorithm.

## Part 3 — The atmospheric-correction integration

Once Davinci was built and the atm files were in place, calling `themis_correct` looked easy on paper. It took three iterations of debugging to actually produce a non-zero output cube. Each bug presents the same symptom — `<id>_tes.hdf` is ~1 MB instead of ~30 MB and `cor.surf` is all zero — but has a different root cause.

### Bug 1: `PROJ_DATA` env var (silent failure)

`shape2georaster` reprojects the WKT polygons of each TES footprint from longlat to the cube's sinusoidal projection by shelling out to `ogr2ogr`. `ogr2ogr` needs the PROJ data files (`proj.db`) to look up the SRS definitions. The path is set by `PROJ_DATA` (newer PROJ) and/or `PROJ_LIB` (older PROJ).

The ISIS conda env sets `PROJ_DATA=$CONDA_PREFIX/share/proj` through its activate.d hooks. But if you invoke `davinci` directly from a shell that has only `source ~/local/davinci/etc/davinci.env` sourced — no conda activate — `PROJ_DATA` is unset, `ogr2ogr` errors with `proj_create: Open of .../share/proj failed`, `shape2georaster` falls back to burning the entire output raster (since the CSV with the reprojected geometry was never produced), and every per-iteration mask ends up uniformly 1 instead of just the polygon interior.

**Fix:** always run the python wrapper inside an activated `isis` conda env. The wrapper's env-merge step then propagates `PROJ_DATA` into the subprocess that spawns davinci.

**Diagnose:** in the symptomatic case, `~/miniconda3/envs/isis/bin/gdalinfo -mm <a rasterized mask>.tif` would report `Computed Min/Max=255.000,255.000` — the burn value, applied to every pixel — even though the polygon should be a tiny ~30 × 30 pixel region.

### Bug 2: Wrong radiance scale → emissivity = 0

ISIS `thm2isis` produces calibrated THEMIS IR radiance, but in **scaled units that are ~10⁵ × the W m⁻² sr⁻¹ µm⁻¹ that `temp_rad_v4` was built for**. The values out of `cam2map` are ~80–200 (clearly not raw DN — `thm2isis` already applies the gain/offset from the EDR label), but `temp_rad_v4` at 280 K shows band-9 radiance around 6×10⁻⁴ — six orders of magnitude smaller.

`thm.themis_emissivity` does an interior `rad2tb` lookup against `temp_rad_v4`. With our cube values 10⁵× too high, every cur_val is far beyond the lookup's max-temperature entry (380 K), the binary search lands at the edge, and the function silently emits zero emissivity.

You can confirm the scale empirically by feeding a clean Mars-surface sub-region into `thm.rad2tb` with several test divisors. Dividing by 1e5 gives brightness temperatures of 272–329 K — typical Mars equatorial daytime range.

**Fix:** the python pipeline runs `algebra A=1e-5` on the lev2 cube before invoking the Davinci side, writing a scaled cube at `<id>.cub` for `themis_correct` to consume:

```bash
algebra from=Ixxxxxx.lev2.cub to=Ixxxxxx.cub operator=unary A=0.00001
```

After scaling, the cube values are in the 0.001–0.0015 W m⁻² sr⁻¹ µm⁻¹ range that the lookup table covers.

**Diagnose:** `stats from=Ixxxxxx.cub` should report `Min` ≈ 0.001, `Max` ≈ 0.002. If you see 80+, scaling didn't run.

### Bug 3: Negative `miny` from out-of-bounds `tes.maxlat`

`themis_correct` does:

```
miny = 1
maxy = image.h
if HasValue(get_struct(tes,"maxlat")) miny = int(round(proj_geocoord(0//tes.maxlat, geostruct)[2]))
if HasValue(get_struct(tes,"minlat")) maxy = int(round(proj_geocoord(0//tes.minlat, geostruct)[2]))
...
train_data = mask.data[,miny:maxy,1] * image.data[,miny:maxy,]
```

The intent is to crop processing to a latitude band. But `proj_geocoord(0, lat, geostruct)` returns a **pixel y-coordinate** based on the input lat — and Davinci has no clamp.

If `tes.maxlat=0` and the actual image extent is lat=-12.3..-0.18, then projection puts lat=0 at pixel y = -109 (above the top of the image). The slice `image.data[,-109:7194,]` degenerates to an empty array. Subsequent `mask × image`, `themis_emissivity`, `avg`, accumulation — all operate on empty arrays. No errors, just silent no-ops. The output cube stays all zero, `mask_cont` stays all zero, the final `surf_spec / mask_cont` is 0/0, and the saved `surf` is 0..0.

**Fix:** the wrapper `pipeline/themis_atm_correct.dv` strips `minlat` / `maxlat` from the tes struct after `themis_tes_overlay` returns:

```
if (HasValue(get_struct(t,"minlat")) != 0) { remove_struct(t, "minlat") }
if (HasValue(get_struct(t,"maxlat")) != 0) { remove_struct(t, "maxlat") }
```

This makes `themis_correct` fall back to `miny=1, maxy=image.h` and operate on the full image. The TES catalog still filtered to our requested lat band; we're just preventing `themis_correct` from re-applying that filter via a potentially-out-of-bounds pixel coordinate.

**Diagnose:** add `printf("miny=%i maxy=%i\n", miny, maxy)` to your local copy of `themis_correct.dav` right after the `proj_geocoord` lines. If `miny` is negative or `maxy > image.h`, this bug is active.

### Why these three bugs were the most expensive part

Each bug produced **exactly the same external symptom** — a 1 MB tes.hdf with `surf` all zeros and `atm` all infinities. Diagnosing them required reading the Davinci C source for `thm.themis_emissivity` and `thm.rad2tb`, manually examining the temp_rad_v4 lookup table, instrumenting the per-iteration loop with explicit printf debugging, and finally tracing the projection arithmetic through `proj_geocoord`. The pipeline as it stands now has them all guarded against, but the diagnostic recipe in the troubleshooting section of `USER_GUIDE.md` covers the case where one regresses.

## Part 4 — File layout

```
~/                                   # local disk (~ 1 TB free)
├── miniconda3/envs/isis/            # ISIS 9.0.0 + Python deps (~4 GB)
├── isis_data/                       # SPICE kernels (~28 GB; base + odyssey)
├── local/davinci/                   # Davinci 3.01 user-prefix install (~600 MB)
│   ├── bin/davinci
│   ├── share/davinci/library/       # bundled library + script_files
│   └── etc/davinci.env              # source this to put DV_* on env
└── src/davinci/                     # SVN checkout (~4 GB; can delete after install)

/mnt/themis-processing/              # SMB share — NOT executable, slow
├── CLAUDE.md                        # short-form architecture/install notes
├── USER_GUIDE.md                    # day-to-day usage
├── SETUP_GUIDE.md                   # this file
├── Get themis processing up and running.docx
├── pipeline/
│   ├── themis_pipeline.py           # main CLI
│   ├── themis_dcs.dv                # Davinci script for raw-radiance DCS
│   ├── themis_atm_correct.dv        # Davinci script for atm correction (with the strip-lat-bounds fix)
│   ├── build_davinci.sh             # reproducible Davinci build
│   ├── dcs_python.py                # Python DCS fallback (rasterio + numpy)
│   ├── extract_spectra.py           # raw spectra plotting
│   └── extract_corrected_spectra.py # raw-vs-corrected spectra comparison
├── atm_steps/                       # user-supplied Dr Rogers files
│   ├── PDART_mineral_library.hdf
│   ├── TES_atmlib.hdf
│   ├── themis_tes_overlay.dav
│   └── themis_correct.dav
├── test_data/
│   └── I01269007EDR.QUB             # sample THEMIS IR EDR from PDS
└── work_full/                       # validated end-to-end outputs
```

## Reproducing this on a fresh box (cheat sheet)

```bash
# 1. ISIS via conda + libmamba solver
conda install -n base -c conda-forge conda-libmamba-solver -y
conda create -n isis -c conda-forge -c usgs-astrogeology --solver=libmamba \
    isis rasterio pillow matplotlib -y
mkdir -p ~/miniconda3/envs/isis/etc/conda/activate.d
cat > ~/miniconda3/envs/isis/etc/conda/activate.d/isis-vars.sh <<'EOF'
export ISISROOT="$CONDA_PREFIX"
export ISISDATA="$HOME/isis_data"
export ALESPICEROOT="$HOME/isis_data"
EOF
mkdir -p ~/isis_data
conda activate isis
downloadIsisData base $ISISDATA
downloadIsisData odyssey $ISISDATA

# 2. Davinci build tools
conda install -n base -c conda-forge --solver=libmamba \
    subversion gcc_linux-64 gxx_linux-64 make autoconf automake libtool pkg-config bison flex \
    hdf5 hdf4 cfitsio readline libxml2 libpng zlib -y

# 3. Davinci build (uses our gnu89 + permissive flags)
conda activate base
bash /mnt/themis-processing/pipeline/build_davinci.sh

# 4. Davinci env file
cat > ~/local/davinci/etc/davinci.env <<'EOF'
export DV_HOME="$HOME/local/davinci/share/davinci"
export DV_LIB="$DV_HOME/library"
export DV_SCRIPT_FILES="$DV_LIB/script_files"
export DV_OS="linux"
export DV_VIEWER="${DV_VIEWER:-display}"
export PATH="$HOME/local/davinci/bin:$DV_LIB/bin:$PATH"
export DV_GDAL_PATH="${DV_GDAL_PATH:-$HOME/miniconda3/envs/isis/bin}"
EOF

# 5. Smoke test
conda activate isis
python /mnt/themis-processing/pipeline/themis_pipeline.py \
    /mnt/themis-processing/test_data/I01269007EDR.QUB \
    --workdir /tmp/themis_smoke --skip-davinci
```

If the smoke test produces `/tmp/themis_smoke/I01269007.lev2.png` showing the Tharsis-region grayscale strip, the basic chain is wired up. Add `--with-atm-correction --atm-minlat=-12 --atm-maxlat=-1` for the full ~3-hour atmospheric-correction run.
