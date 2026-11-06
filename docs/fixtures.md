# Fixtures and provenance

## Development workflows

`data/dev-workflow/` and `data/mini-dev-workflow/` come from
[YoloFS/perf-eval](https://github.com/YoloFS/perf-eval) at `bc32d08`
(2026-08-28); their last changes upstream are `de6f95f` and `afe112a`. The kernel
patchset is Amir Goldstein's overlayfs `ovl_file` refactor, merged in Linux v6.13.

Exception: `data/dev-workflow/tinyconfig-overlayfs.config` is regenerated on the
pinned base commit (`c2c54b5f34f6^`, Linux 6.12.0-rc7) with
`make ARCH=x86 tinyconfig`, `CONFIG_64BIT=y`, `CONFIG_OVERLAY_FS=y`, then
`make ARCH=x86 olddefconfig`. It is a 64-bit x86 config and differs from the
upstream file. The series and command files are unchanged from upstream.

Both workflows inherit the build environment: `ARCH` is not forced and `CC` is
not promoted to a make argument. A fixed config input does not guarantee an
identical final kernel config; see [dev workflow](bench-dev-workflow.md).

The mini source repository is rebuilt deterministically from committed commands.
Update the bundle and `series.json` together:

```sh
uv run python tools/gen_mini_source_repo.py --force
```

## npm-install

`data/pnpm-bench*` comes from [pnpm/pnpm](https://github.com/pnpm/pnpm) at
`5bd38b19` (2026-11-03). The npm lockfile was generated from the same manifest
with `npm install --package-lock-only --legacy-peer-deps --ignore-scripts --cache <tmp>`;
it is not converted from the pnpm lockfile.

## SQLite

`kvtest` and `sqlite-speedtest` build pinned official SQLite sources; version and
hashes are recorded in results. See [sqlite-speedtest](bench-sqlite-speedtest.md)
and [kvtest](bench-kvtest-openclaw.md).

## Licensing

YoloFS/perf-eval has no LICENSE file; confirm permission before redistributing
its fixtures. Linux source is GPL-2.0.
