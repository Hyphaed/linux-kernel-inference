from __future__ import annotations
import json
import shutil
import sys
from pathlib import Path

from ..util import log
from ..util.run import run
from ..util.checksum import sha256_file

NAME = "package"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_KXDB_CONFIG = _REPO_ROOT / "kernel-research" / "kxdb_tool" / "config.py"

# Security-relevant symbols from kxdb config.py — duplicated here so the
# snapshot works even without kernel-research on the Python path.
_DEFAULT_SYMBOLS = [
    "prepare_kernel_cred",
    "commit_creds",
    "find_task_by_vpid",
    "switch_task_namespaces",
    "__x64_sys_fork",
    "msleep",
    "sock_def_write_space",
    "__sk_destruct",
    "init_nsproxy",
    "anon_pipe_buf_ops",
]


def _load_kxdb_symbols() -> list[str]:
    """Load symbol list from kernel-research/kxdb_tool/config.py if present."""
    if not _KXDB_CONFIG.exists():
        return _DEFAULT_SYMBOLS
    try:
        spec = __import__("importlib.util", fromlist=["spec_from_file_location", "module_from_spec"])
        loader = spec.spec_from_file_location("kxdb_config", str(_KXDB_CONFIG))
        mod = spec.module_from_spec(loader)
        loader.loader.exec_module(mod)
        return list(mod.symbols)
    except Exception:
        return _DEFAULT_SYMBOLS


def _kxdb_snapshot(ctx, vmlinux: Path) -> None:
    """Extract security-relevant symbol addresses from vmlinux using nm."""
    symbols_to_find = _load_kxdb_symbols()
    r = run(["nm", "-n", str(vmlinux)], check=False, force=True)
    if not r.ok():
        log.warn(f"nm failed on {vmlinux.name} — skipping kxdb symbol snapshot")
        return

    found: dict[str, str] = {}
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            addr, _stype, sym_name = parts
            if sym_name in symbols_to_find:
                found[sym_name] = f"0x{addr}"

    out_dir = ctx.repo_root / "out" / "kxdb"
    out_dir.mkdir(parents=True, exist_ok=True)
    pkgver = getattr(ctx, "kernel_pkgver", "unknown") or "unknown"
    snapshot = {
        "kernel_pkgver": pkgver,
        "flavour": ctx.profile.flavour if ctx.profile else "unknown",
        "vmlinux": str(vmlinux),
        "symbols": found,
        "missing_symbols": [s for s in symbols_to_find if s not in found],
    }
    out_file = out_dir / f"symbols-{pkgver}.json"
    out_file.write_text(json.dumps(snapshot, indent=2))
    pct = int(100 * len(found) / max(len(symbols_to_find), 1))
    log.ok(f"kxdb snapshot → {out_file.name} ({len(found)}/{len(symbols_to_find)} symbols, {pct}%)")
    if snapshot["missing_symbols"]:
        log.warn(f"  missing: {', '.join(snapshot['missing_symbols'][:6])}")


def run_phase(ctx) -> Path:
    log.banner("Phase 7/9 — Collect packages + manifest + kxdb snapshot")
    # dict.fromkeys preserves order while dropping duplicate paths — a
    # duplicate would otherwise crash here (move, then unlink-on-move of an
    # already-moved file) rather than just produce a redundant manifest entry.
    debs = list(dict.fromkeys(ctx.built_debs or []))
    out = ctx.repo_root / "out" / "debs"
    out.mkdir(parents=True, exist_ok=True)

    if ctx.dry_run and not debs:
        log.info("(dry-run) no .debs to package")
        return out / "manifest.json"

    _DEB_DESCRIPTIONS = {
        "linux-image":         "kernel image (vmlinuz + .ko modules) — this is what GRUB boots",
        "linux-headers":       "kernel headers — DKMS uses these to build nvidia/vmware/greenboost modules",
        "linux-libc-dev":      "UAPI headers for userspace compilation; not the compiler itself",
        "linux-hyphaed-tools": "perf, bpftool, cpupower, turbostat, x86_energy_perf_policy — built from this kernel source",
    }

    moved: list[dict] = []
    for d in debs:
        dst = out / d.name
        if not d.exists() and dst.exists():
            # Resuming a partially-completed package phase (e.g. it crashed
            # partway through on an earlier run) — this one's already moved.
            log.info(f"  {dst.name} already in {out.name}/ — skipping")
        elif not d.exists() and not dst.exists():
            # Missing on both sides — not resumable, just genuinely gone
            # (e.g. lost to an earlier crash mid-move). Don't let one lost
            # package block collecting the others; surface it loudly instead.
            log.err(f"  {d.name} not found at {d} or {dst} — skipping, re-run `build` to regenerate it")
            continue
        else:
            if dst.exists():
                dst.unlink()
            shutil.move(str(d), dst)
        sha = sha256_file(dst)
        moved.append({"file": dst.name, "sha256": sha, "size": dst.stat().st_size})
        desc = next(
            (v for k, v in _DEB_DESCRIPTIONS.items() if dst.name.startswith(k)), ""
        )
        suffix = f"  [dim]— {desc}[/dim]" if desc else ""
        log.console.print(f"  [bold]•[/bold] {dst.name}  [dim]sha256={sha[:16]}…[/dim]{suffix}")

    manifest = {
        "kernel_pkgver": ctx.kernel_pkgver,
        "flavour": ctx.profile.flavour,
        "tag": ctx.profile.tag(),
        "packages": moved,
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    log.ok(f"manifest → {manifest_path.name}")
    log.console.print(
        "  [dim]sha256 + size for every .deb; consumed by `hyphaed install` "
        "to verify packages haven't been corrupted between build and install.[/dim]"
    )
    ctx.packaged_debs = [out / m["file"] for m in moved]

    # kxdb symbol snapshot — uses vmlinux from build tree if present.
    vmlinux = (ctx.source_dir / "vmlinux") if ctx.source_dir else None
    if vmlinux and vmlinux.exists():
        log.info("── kxdb symbol snapshot ─────────────────────────────────")
        _kxdb_snapshot(ctx, vmlinux)
        log.console.print(
            "  [dim]symbol→address table for security-sensitive kernel functions "
            "(commit_creds, prepare_kernel_cred, …); used by kxdb to detect "
            "in-memory kernel patches and DKOM attacks post-reboot.[/dim]"
        )
    else:
        log.info("vmlinux not found in source dir — skipping kxdb snapshot (run after a full build)")

    # System.map — symbol-to-address table, used by crash debuggers and eBPF tools
    if ctx.source_dir:
        sysmap = ctx.source_dir / "System.map"
        if sysmap.exists():
            pkgver = getattr(ctx, "kernel_pkgver", None) or "unknown"
            dst = out / f"System.map-{pkgver}"
            shutil.copy2(sysmap, dst)
            log.ok(f"System.map → {dst.name}")
            log.console.print(
                "  [dim]symbol→virtual-address map for every kernel symbol; "
                "used by crash(8), kgdb, /proc/kallsyms cross-checks, and eBPF "
                "programs that look up function addresses at load time.[/dim]"
            )

    return manifest_path
