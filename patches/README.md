# Patches

Vendored kernel patches applied on top of Ubuntu's source tree.

## Layout

- `series` — ordered list of patches to `git am --3way` (quilt format).
- `VENDOR.lock` — local source URLs + sha256 + kind for each patch in `series`.
- `0001-*.patch` … `0013-*.patch` — ready-to-apply patches populated by `fetch.py`.
- `fetch.py` — fetcher + verifier.

## Kinds

| kind | URL form | Source |
|---|---|---|
| `local-file` | `file:///abs/path` | Copy bytes from a file in a local git checkout |
| `git-commit` | `git+file:///abs/repo@<sha>` | `git format-patch -1 --no-signature` of one commit |

All sources are local. No network access is required.

## First-time setup / pinning shas

After editing `VENDOR.lock` (new entry or changed source), run:

```bash
python patches/fetch.py --discover
```

This fetches each entry, prints the computed sha256, and saves the patch.
Copy the printed sha values back into `VENDOR.lock`, replacing `PINNED`.

Normal fetch (verifies against pinned shas):

```bash
python patches/fetch.py
# or
make fetch-patches
```

## Adding a patch

1. Append a line to `series` with the local patch filename.
2. Append a line to `VENDOR.lock` with URL + `PINNED` + kind.
3. Run `python patches/fetch.py --discover` to compute the sha256, then replace `PINNED`.
4. Add `Forward-Port-Notes:` and optional `Conflict-Marker:` trailers to the patch header.

## Patch header conventions

```
Forward-Port-Notes: kernel/sched/fair.c, kernel/sched/core.c
Conflict-Marker: bore
```

`Forward-Port-Notes` lists paths the patch touches (used at rebase time).
`Conflict-Marker` enables mutex detection (e.g. `bore` vs `prjc`).
