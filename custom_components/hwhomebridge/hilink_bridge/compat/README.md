# HAOS compatibility libraries

These ARM64 libraries allow the glibc-linked HiLink SDK to run inside the
musl-based Home Assistant Core container used by HAOS. `libhilink_bridge.so`
has an ELF runpath of `$ORIGIN/compat`, so no host or container packages need
to be installed.

The files were taken from Alpine Linux v3.24 packages:

- `gcompat` 1.1.0-r4 (NCSA)
- `libucontext` 1.5.1-r0 (ISC)
- `musl-obstack` 1.2.3-r2 (LGPL-2.1-or-later)

This bundle targets ARM64 only, matching the bundled HiLink SDK binary.
