from __future__ import annotations
import os
import re
import selectors
import shutil
import socket
import subprocess
from pathlib import Path
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text

from ..util import log
from ..util.run import run, run_sudo
from ..util.prompts import confirm
from ..version import kdeb_pkgversion, hyphaed_uname_r

# Tools built from kernel source and bundled into linux-hyphaed-tools-*.deb.
# Each entry: (display_name, build_cwd_relative_to_src, binary_relative_to_cwd,
#              extra_make_vars)
_TOOLS: list[tuple[str, str, str, list[str]]] = [
    ("perf",                  "tools/perf",                      "perf",                        ["WERROR=0", "NO_LIBPYTHON=1", "NO_LIBPERL=1"]),
    ("bpftool",               "tools/bpf/bpftool",               "bpftool",                     ["WERROR=0"]),
    ("cpupower",              "tools/power/cpupower",            "cpupower",                    []),
    ("turbostat",             "tools/power/x86/turbostat",       "turbostat",                   []),
    ("x86_energy_perf_policy","tools/power/x86/x86_energy_perf_policy","x86_energy_perf_policy",[]),
]

NAME = "build"


def _source_date_epoch(src: Path) -> int:
    """Stable build timestamp for reproducible .debs.

    The source tree's directory mtime changes every time a fragment is
    merged or .config is rewritten, so two builds from identical
    patches+config would still produce different SOURCE_DATE_EPOCH values
    and non-reproducible artifacts. The git baseline commit + patch series
    (gitops.seed_baseline / apply_series) give a real anchor instead:
    the author-date of the last applied patch's `git am` commit, which is
    fixed by the patch content, not by wall-clock build time.
    """
    if (src / ".git").exists():
        r = run(["git", "log", "-1", "--format=%at"], cwd=src, check=False, force=True)
        if r.ok() and r.stdout.strip().isdigit():
            return int(r.stdout.strip())
    return int(src.stat().st_mtime) if src.exists() else 0

_ERROR_RE = re.compile(
    r"^(.*?):\s*(?:error|fatal error|undefined reference|Error \d+|make.*\*\*\*"
    r"|No rule to make target)",
    re.IGNORECASE | re.MULTILINE,
)

# Kernel build step prefixes (kbuild V=0 format: "  CC      path/to/file.o")
_STEP_RE = re.compile(
    r"^\s+(CC(?:\s+\[M\])?|LD(?:\s+\[M\])?|AS(?:\s+\[M\])?|AR|OBJCOPY|OBJDUMP|"
    r"RANLIB|STRIP|HOSTCC|HOSTLD|GEN|DTC|RUSTC(?:\s+\[M\])?|BINDGEN)\s+(.+)$"
)
# Packaging-phase lines: the dh_* helpers and the dpkg-* tail.
#
# This already excluded dpkg-source, with the right reason — "its lines appear
# during early bindeb-pkg source extract, long before real packaging, and
# would falsely flip the packaging bar" — and then kept dpkg-buildpackage,
# which is line 4 of the log, six lines EARLIER than the dpkg-source it was
# guarding against. The rule was correct and applied to one of the two
# commands it applies to.
# Real packaging, i.e. the tail that runs AFTER compilation.
#
# `dpkg-buildpackage` and `dpkg-source` are deliberately NOT here. Measured on
# the 7.1.9 build log (61,047 lines): dpkg-buildpackage is line **4** and
# dpkg-source line **10** — `make bindeb-pkg` invokes dpkg-buildpackage first
# and *that* drives the compile. Matching them flipped `packaging = True`
# 0.006% into the build, which hid the compile bar and made the
# `if not packaging:` guard discard all **46,532** compile lines. The only bar
# with a meaningful total never advanced once, so the ETA was computed from a
# task covering a rounding error of the real work.
#
# The genuine boundary is dpkg-gencontrol/dpkg-deb at line 41,196 (67% in).
_PKG_RE = re.compile(
    r"^\s*(dpkg-deb\b|dpkg-gencontrol\b|dpkg-genchanges\b|dpkg-genbuildinfo\b|dh_)"
)
# Extracts the bare dh_* / dpkg-* command name from a packaging line
_PKG_CMD_RE = re.compile(r"^\s*(dh_\w+|dpkg-\w+)")
# kbuild verbs emitted during modules_install inside dpkg-buildpackage (high-volume;
# description-only updates — do not tick the packaging counter)
# `dpkg-deb: building package 'NAME' in '../FILE'.` — the single line emitted
# before dpkg-deb goes silent for minutes. On this box the -dbg package is a
# 7.7 GB staged tree compressed to ~1.4 GB by 25 cores; nothing is printed for
# the whole of it, so the UI has to say what is happening or the build reads as
# hung. Reported 2026-08-20: a bar sitting at "100% 14300/14300 ETA 0:00:00"
# while dpkg-deb ran at 2520% CPU.
_DEB_BUILD_RE = re.compile(
    r"^dpkg-deb: building package '([^']+)'(?: in '([^']+)')?"
)

# Module steps during the packaging tail.
#
# INSTALL and SIGN are kept — they ARE the module install — but only when the
# target is a .ko. Measured on the 7.1.9 log: 6,610 `INSTALL …/foo.ko`, 6,610
# `SIGN …/foo.ko`, 1 DEPMOD, zero `INSTALL [M]`, and **31** bare INSTALL lines
# that are tools/UAPI headers ("INSTALL .../libbpf//include/bpf/bpf.h").
# Those 31 land at log line 81, during the tools build; the real module steps
# start at 41,205, just after the packaging flip at 41,196.
#
# The .ko guard is therefore belt-and-braces now that _PKG_RE no longer flips
# `packaging` at line 4 — but it is cheap, and it is what makes the branch
# correct on its own terms rather than only in combination with another fix.
_PKG_KBUILD_RE = re.compile(
    r"^\s+(INSTALL\s+\[M\]|DEPMOD|(?:INSTALL|SIGN)(?=\s+\S+\.ko$))\s+(.+)$"
)

# Compilation steps for a full kernel tree. MEASURED, not guessed, and
# measured against _STEP_RE specifically — the counter that actually drives
# the bar — by replaying the real 7.1.9 build log
# (`out/logs/build-20260820-141842.log`, 61,047 lines): **33,266** matches.
#
# Counting with a looser grep gives 46,532, and using that would have made the
# bar read low for the whole build. An estimate has to be measured against the
# thing that increments it, not against a plausible-looking proxy.
#
# The old 22,000 was under by ~1.5x. _grow_total still extends this at
# runtime; it only sets the starting width, and a starting width that is wrong
# is an ETA that is wrong until the first grow.
_STEP_ESTIMATE = 34_000


class _SettledTimeRemainingColumn(TimeRemainingColumn):
    """TimeRemainingColumn that stays quiet until its own estimate settles.

    Rich derives remaining time from a sliding speed window. For the first
    seconds of a kernel build that window holds nothing but the Rust front of
    the build , BINDGEN, then RUSTC on rust/uapi.o and friends , which are the
    slowest and least parallel steps in the whole run. Extrapolating 34,000
    steps from them is what put "ETA 5:18:11" on screen, on the line directly
    below this file's own honest "estimated 10-25 min on this CPU". Measured on
    the 2026-08-21 build: 23 steps in the first 15 s, then 78.6 steps/s once
    the C files started, and the real remaining time was about six minutes.

    An operator reading two numbers that disagree 15-fold has to decide which
    one to believe, and the wrong choice is to kill a healthy build. So show
    "-:--:--" until enough of the run is behind us for the window to mean
    something , the same reasoning as the indeterminate packaging task, which
    already refuses to invent an ETA for a step whose size is unknown.
    """

    #: Below this fraction the sample is still dominated by the Rust prologue.
    SETTLE_FRACTION = 0.05
    #: ...and never guess from less wall time than this either, for the case
    #: where a warm ccache makes the early steps fly and the estimate comes out
    #: optimistic rather than pessimistic.
    SETTLE_SECONDS = 45.0

    def render(self, task):
        total = task.total
        if total and not task.finished:
            elapsed = task.elapsed or 0.0
            if ((task.completed / total) < self.SETTLE_FRACTION
                    or elapsed < self.SETTLE_SECONDS):
                return Text("-:--:--", style="progress.remaining")
        return super().render(task)

# Distinct dh_* / dpkg-* steps in the packaging tail. Measured: 6 distinct
# commands in that same log, not 18.
_PKG_STEP_ESTIMATE = 8
# Module-install steps. Measured: 6,610 INSTALL + 6,610 SIGN + 1 DEPMOD on the
# 7.1.9 build. The old value of 50 was under by ~260x, so the bar read "2/50"
# and its ETA described a total that had nothing to do with the work left.
_MODULES_ESTIMATE = 13_000


def _grow_total(total: int, completed: int, bump: int = 2_000, threshold: float = 0.95) -> int:
    """Return a grown total when completed is within `threshold` of total.

    Uses whichever is larger: fixed `bump` or 10 % of total, so the bar
    always has meaningful headroom left after the expansion.
    """
    if completed >= total * threshold:
        return max(total + bump, int(total * 1.10))
    return total


def _human_bytes(n: int) -> str:
    """Bytes as a short human string. Used for the only feedback available
    while dpkg-deb compresses in silence."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB"):
        if n < step or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} GB"


def _find_dpkg_deb_pid() -> int | None:
    """PID of an in-flight dpkg-deb, or None. Best-effort, never raises."""
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                with open(f"/proc/{entry.name}/comm") as fh:
                    if fh.read().strip() == "dpkg-deb":
                        return int(entry.name)
            except OSError:
                continue
    except OSError:
        pass
    return None


def _dpkg_deb_compressed_bytes(pid: int) -> int | None:
    """Compressed bytes accumulated so far by dpkg-deb, via its temp file.

    dpkg-deb streams the compressed data member into a DELETED temp file
    (/tmp/dpkg-deb.XXXXXX) and appends it to the .deb only at the very end.
    Measured 2026-08-20 on the 7.7 GB -dbg package: the output .deb sat at
    **0.2 MB after fifteen minutes** while the temp file was at **869 MB** and
    climbing toward a 1.39 GB final size. Watching the output file — the
    obvious thing to watch — would have reported approximately zero for the
    single longest step of the build.

    Takes the largest fd the process holds, which is that temp file by orders
    of magnitude. Best-effort: returns None on any error, and the caller falls
    back to showing elapsed time alone.
    """
    best = 0
    try:
        for fd in os.scandir(f"/proc/{pid}/fd"):
            try:
                size = os.stat(fd.path).st_size      # follows the fd, deleted or not
            except OSError:
                continue
            best = max(best, size)
    except OSError:
        return None
    return best or None


def _ensure_build_deps(src: Path) -> None:
    """Run dpkg-checkbuilddeps against the source tree's debian/control.
    If unmet deps are found, install them automatically via apt.
    """
    control = src / "debian" / "control"
    if not control.exists():
        return
    r = run(["dpkg-checkbuilddeps", str(control)], check=False, force=True)
    if r.ok():
        return
    stderr = r.stderr.strip()
    log.warn(f"unmet build dependencies: {stderr}")
    pkgs_raw = stderr.split("unmet build dependencies:")[-1].strip()
    pkgs = re.sub(r"\s*\([^)]*\)", "", pkgs_raw).split()
    if not pkgs:
        log.warn("could not parse unmet dep names — run: sudo apt install debhelper")
        return
    log.info(f"installing missing build deps: {' '.join(pkgs)}")
    run_sudo(["apt-get", "install", "-y", *pkgs])
    r2 = run(["dpkg-checkbuilddeps", str(control)], check=False, force=True)
    if not r2.ok():
        log.err(f"build deps still unmet after install: {r2.stderr.strip()}")
        raise RuntimeError("unmet kernel build dependencies")


def _build_tools_deb(src: Path, pkgver: str, flavour: str, jobs: int) -> Path | None:
    """Build kernel tools (perf, bpftool, cpupower, turbostat, …) and package
    them into linux-hyphaed-tools-{pkgver}_amd64.deb.

    Returns the .deb path on success, None if nothing could be built.
    Non-fatal: every individual tool failure is a warning, not an error.
    """
    log.banner("Building kernel tools (.deb)")
    staging = src.parent / f"linux-hyphaed-tools-{pkgver}_amd64"
    bin_dir = staging / "usr" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    built: list[str] = []
    for name, rel_cwd, binary_name, extra_vars in _TOOLS:
        tool_dir = src / rel_cwd
        if not tool_dir.exists():
            log.info(f"  {name}: source dir missing — skipping")
            continue
        log.info(f"  building {name} …")
        r = run(
            ["make", f"-j{jobs}", *extra_vars],
            cwd=tool_dir, check=False,
        )
        if not r.ok():
            log.warn(f"  {name}: build failed — skipping")
            continue
        binary = tool_dir / binary_name
        if not binary.exists():
            log.warn(f"  {name}: binary {binary_name} not found after build — skipping")
            continue
        shutil.copy2(binary, bin_dir / name)
        built.append(name)
        log.ok(f"  {name}: ok")

    if not built:
        log.warn("no kernel tools could be built — skipping tools package")
        shutil.rmtree(staging, ignore_errors=True)
        return None

    # Build DEBIAN/control
    debian_dir = staging / "DEBIAN"
    debian_dir.mkdir(parents=True, exist_ok=True)
    (debian_dir / "control").write_text(
        f"Package: linux-hyphaed-tools\n"
        f"Version: {pkgver}\n"
        f"Architecture: amd64\n"
        f"Maintainer: hyphaed <hyphaed@localhost>\n"
        f"Section: devel\n"
        f"Priority: optional\n"
        # cpupower/turbostat/x86_energy_perf_policy are also shipped straight
        # to /usr/bin by the distro's linux-tools-common — dpkg refuses to
        # install over a file owned by another package without this.
        # /usr/bin/perf specifically is owned by the SEPARATE linux-perf
        # package on this distro, not linux-tools-common — found live
        # 2026-08-07: a routine `apt upgrade` pulling in linux-perf
        # 7.0.0-29.29 (dependency of the stock linux-generic-hwe-26.04
        # metapackage, unrelated to hyphaed) failed with "trying to
        # overwrite '/usr/bin/perf', which is also in package
        # linux-hyphaed-tools" — same root cause, different distro package.
        f"Conflicts: linux-tools-common, linux-perf\n"
        f"Replaces: linux-tools-common, linux-perf\n"
        # Conflicts+Replaces alone isn't enough — found live 2026-07-24:
        # dpkg refused the replace with "cannot proceed with removal of
        # linux-tools-common: linux-tools-7.0.0-28 depends on
        # linux-tools-common". A version-specific linux-tools-<kver>
        # meta-package hard-depends on linux-tools-common directly, and
        # Replaces doesn't satisfy someone ELSE's dependency on the package
        # being replaced — only Provides does. This tells dpkg
        # linux-hyphaed-tools counts as satisfying that dependency too, so
        # the swap doesn't need --auto-deconfigure. Provides linux-perf too,
        # defensively, since linux-perf is likely depended on the same way
        # by a linux-tools-<kver> metapackage (not yet hit live, but the
        # failure mode would be identical).
        f"Provides: linux-tools-common, linux-perf\n"
        f"Description: Linux {flavour} kernel tools\n"
        f" perf, bpftool, cpupower, turbostat and x86_energy_perf_policy\n"
        f" built from the {flavour} kernel source tree.\n"
        f" Built tools: {', '.join(built)}\n"
    )

    deb_path = src.parent / f"linux-hyphaed-tools_{pkgver}_amd64.deb"
    r = run(
        ["fakeroot", "dpkg-deb", "--build", str(staging), str(deb_path)],
        check=False,
    )
    shutil.rmtree(staging, ignore_errors=True)
    if not r.ok():
        log.warn(f"dpkg-deb failed for tools package: {r.stderr[:300]}")
        return None

    log.ok(f"tools package: {deb_path.name}  ({', '.join(built)})")
    return deb_path


def _surface_build_error(log_file: Path) -> None:
    """Fish the first compiler/linker error out of the build log and print
    a small context window so the user doesn't need to tail it manually.
    """
    if not log_file.exists():
        return
    text = log_file.read_text(errors="replace")
    m = _ERROR_RE.search(text)
    if not m:
        tail = text.splitlines()[-40:]
        log.err("(no compiler error matched; showing last 40 lines of build log)")
        for line in tail:
            log.console.print(f"  {escape(line)}")
        return
    lines = text.splitlines()
    idx = 0
    pos = 0
    for i, ln in enumerate(lines):
        pos += len(ln) + 1
        if pos >= m.start():
            idx = i
            break
    start = max(0, idx - 5)
    end = min(len(lines), idx + 15)
    log.err(f"build failed — first error around line {idx} of {log_file.name}:")
    for i in range(start, end):
        prefix = " >> " if i == idx else "    "
        log.console.print(f"  {prefix}{escape(lines[i])}")


def _run_make_with_progress(
    cmd: list[str],
    cwd: Path,
    env: dict,
    log_file: Path,
) -> int:
    """Run make bindeb-pkg, streaming output to log_file while showing a
    Rich progress bar that advances on each compiled object.

    Returns the process exit code.
    """
    full_env = os.environ.copy()
    full_env.update(env)

    log.console.print(f"[muted]$[/muted] {' '.join(cmd)}")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TextColumn("·"),
        TimeElapsedColumn(),
        TextColumn("·  ETA"),
        _SettledTimeRemainingColumn(),
        console=log.console,
        expand=True,
        transient=False,
    ) as progress:
        compile_task = progress.add_task(
            "[cyan]compiling[/cyan]", total=_STEP_ESTIMATE
        )
        modules_task = progress.add_task(
            "[blue]building pkgs[/blue]", total=_MODULES_ESTIMATE, visible=False
        )
        pkg_task = progress.add_task(
            "[yellow]packaging[/yellow]", total=_PKG_STEP_ESTIMATE, visible=False
        )
        # total=None -> indeterminate: Rich pulses the bar and TimeRemaining
        # renders "-:--:--" instead of inventing an ETA for a step whose size
        # is genuinely unknown until it finishes.
        deb_task = progress.add_task(
            "[magenta]compressing[/magenta]", total=None, visible=False
        )

        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ, "out")
        sel.register(proc.stderr, selectors.EVENT_READ, "err")

        packaging = False
        installing_modules = False
        compiled = 0
        modules_count = 0
        seen_pkg_cmds: set[str] = set()
        deb_name: str | None = None     # package currently being compressed
        deb_path: Path | None = None    # its output file (stays tiny until the end)
        deb_pid: int | None = None      # the dpkg-deb doing the work

        with log_file.open("ab") as log_fh:
            log_fh.write(f"# $ {' '.join(cmd)}\n".encode())

            while sel.get_map():
                for key, _ in sel.select(timeout=0.5):
                    chunk = key.fileobj.read1(65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    log_fh.write(chunk)
                    log_fh.flush()

                    for raw_line in chunk.decode(errors="replace").splitlines():
                        # Compile step — only while packaging hasn't started yet
                        if not packaging:
                            step_m = _STEP_RE.match(raw_line)
                            if step_m:
                                verb = step_m.group(1).strip()
                                target = step_m.group(2).strip()
                                # Keep only the last 55 chars of the path for display
                                short = target[-55:] if len(target) > 55 else target
                                compiled += 1
                                task = progress.tasks[compile_task]
                                total = task.total or _STEP_ESTIMATE
                                new_total = _grow_total(total, compiled)
                                if new_total != total:
                                    progress.update(compile_task, total=new_total)
                                    total = new_total
                                # Clamp so MofNCompleteColumn never overflows
                                progress.update(
                                    compile_task,
                                    completed=min(compiled, total - 1),
                                    description=f"[cyan]{verb}[/cyan]  [dim]{escape(short)}[/dim]",
                                )

                        # dpkg-deb going quiet: name the package and start
                        # watching its output file grow. Checked before the
                        # generic packaging branch so the specific, useful
                        # message wins over "packaging  dpkg-deb".
                        deb_m = _DEB_BUILD_RE.match(raw_line.strip())
                        if deb_m:
                            deb_name = deb_m.group(1)
                            rel = deb_m.group(2)
                            deb_path = (Path(cwd) / rel).resolve() if rel else None
                            deb_pid = None          # resolved lazily on the next tick
                            packaging = True
                            progress.update(compile_task, visible=False)
                            progress.update(modules_task, visible=False)
                            progress.update(pkg_task, visible=False)
                            progress.update(
                                deb_task, visible=True, completed=0,
                                description=f"[magenta]compressing[/magenta]  "
                                            f"[dim]{escape(deb_name)}[/dim]",
                            )
                            installing_modules = False

                        # Packaging step — dpkg-buildpackage fires first, then
                        # modules_install (INSTALL/DEPMOD), then dh_* helpers
                        if _PKG_RE.match(raw_line):
                            # A chatty packaging step means the previous
                            # dpkg-deb finished: retire the compressing bar
                            # rather than leaving two bars on screen, one of
                            # them describing work that is over.
                            if deb_name is not None:
                                progress.update(deb_task, visible=False)
                                deb_path = deb_name = deb_pid = None
                            cmd_m = _PKG_CMD_RE.match(raw_line)
                            pkg_cmd = cmd_m.group(1) if cmd_m else raw_line.strip().split()[0]
                            if not packaging:
                                packaging = True
                                # Hide compile bar so it doesn't render twice at exit
                                progress.update(compile_task, visible=False)
                            # dh_* signals real packaging has started; retire modules bar
                            if pkg_cmd.startswith("dh_") and installing_modules:
                                installing_modules = False
                                progress.update(
                                    modules_task,
                                    completed=progress.tasks[modules_task].total,
                                    visible=False,
                                )
                                progress.update(pkg_task, visible=True)
                            elif not pkg_cmd.startswith("dh_") and not installing_modules:
                                # dpkg-buildpackage / dpkg-deb / make[1]: show packaging bar
                                progress.update(pkg_task, visible=True)
                            if pkg_cmd not in seen_pkg_cmds:
                                seen_pkg_cmds.add(pkg_cmd)
                                pkg_total = progress.tasks[pkg_task].total or _PKG_STEP_ESTIMATE
                                new_pkg_total = _grow_total(pkg_total, len(seen_pkg_cmds), bump=4)
                                if new_pkg_total != pkg_total:
                                    progress.update(pkg_task, total=new_pkg_total)
                                    pkg_total = new_pkg_total
                                progress.update(
                                    pkg_task,
                                    completed=min(len(seen_pkg_cmds), pkg_total - 1),
                                    description=f"[yellow]{pkg_cmd}[/yellow]",
                                )
                        elif packaging:
                            # kbuild INSTALL/DEPMOD lines during modules_install
                            kb_m = _PKG_KBUILD_RE.match(raw_line)
                            if kb_m:
                                verb = kb_m.group(1).strip()
                                target = kb_m.group(2).strip()
                                short = target[-50:] if len(target) > 50 else target
                                if not installing_modules:
                                    installing_modules = True
                                    # Hide the packaging bar while modules install
                                    progress.update(pkg_task, visible=False)
                                    progress.update(modules_task, visible=True)
                                modules_count += 1
                                mod_total = progress.tasks[modules_task].total or _MODULES_ESTIMATE
                                new_mod_total = _grow_total(mod_total, modules_count, bump=100)
                                if new_mod_total != mod_total:
                                    progress.update(modules_task, total=new_mod_total)
                                if verb == "DEPMOD":
                                    # DEPMOD is the final step of modules_install
                                    progress.update(
                                        modules_task,
                                        completed=progress.tasks[modules_task].total,
                                        description="[blue]building pkgs[/blue]  [dim]module index[/dim]",
                                    )
                                else:
                                    progress.update(
                                        modules_task,
                                        completed=modules_count,
                                        description=f"[blue]building pkgs[/blue]  [dim]staging {escape(short)}[/dim]",
                                    )

                # Runs on every tick INCLUDING the 0.5 s select timeouts, which
                # is the whole point: dpkg-deb prints one line and then says
                # nothing for minutes while it compresses. Without this the bar
                # is frozen and the build reads as hung.
                if deb_name is not None and progress.tasks[deb_task].visible:
                    if deb_pid is None or not Path(f"/proc/{deb_pid}").exists():
                        deb_pid = _find_dpkg_deb_pid()   # each package = new process
                    packed = _dpkg_deb_compressed_bytes(deb_pid) if deb_pid else None
                    detail = (f" — {_human_bytes(packed)} packed"
                              if packed else " — working")
                    progress.update(
                        deb_task,
                        description=f"[magenta]compressing[/magenta]  "
                                    f"[dim]{escape(deb_name)}{detail}[/dim]",
                    )

        proc.wait()

        progress.update(deb_task, visible=False)
        if packaging:
            progress.update(pkg_task, completed=progress.tasks[pkg_task].total)
            if installing_modules:
                # Build ended during modules_install (unusual — hide the partial bar)
                progress.update(modules_task, visible=False)
        else:
            # Build failed before packaging — show compile bar briefly then hide
            progress.update(
                compile_task,
                completed=progress.tasks[compile_task].total,
                visible=True,
            )

    return proc.returncode


def run_phase(ctx) -> list[Path]:
    log.banner("Phase 6/9 — Build kernel (.deb)")
    src = ctx.source_dir
    profile = ctx.profile

    pkgver = kdeb_pkgversion(profile)
    ctx.kernel_pkgver = pkgver
    log.info(f"package version: {pkgver}")
    log.info(f"flavour suffix:  -{profile.flavour}")
    log.info(f"final uname -r:  {hyphaed_uname_r(profile.running_kernel, profile.flavour, getattr(ctx, 'source_mode', 'ubuntu'))}")

    jobs = max(1, (os.cpu_count() or 1))
    source_date_epoch = _source_date_epoch(src)
    env = {
        "LOCALVERSION": f"-{profile.flavour}",
        "KDEB_PKGVERSION": pkgver,
        "KBUILD_BUILD_USER": profile.flavour,
        "KBUILD_BUILD_HOST": socket.gethostname(),
        "SOURCE_DATE_EPOCH": str(source_date_epoch),
        "KCFLAGS": "-pipe",
    }
    extra_make_args: list[str] = []
    if shutil.which("ccache"):
        ccache_masquerade = Path("/usr/lib/ccache")
        if (ccache_masquerade / "gcc").exists():
            # PATH-masquerade instead of `CC="ccache gcc"`: keeps CC/HOSTCC at
            # their kernel-default single-token value ("gcc") so ccache stays
            # transparent to callers that require exactly one executable path
            # in that variable — e.g. CONFIG_RUST's `scripts/generate_rust_target`
            # host tool forwards $(HOSTCC) straight into rustc's `-Clinker=`,
            # which cannot parse a two-word "ccache gcc" value and fails with
            # "multiple input filenames provided".
            env["PATH"] = f"{ccache_masquerade}:{os.environ.get('PATH', '')}"
            log.info("ccache detected — using PATH masquerade (/usr/lib/ccache) for faster rebuilds")
        else:
            cc = shutil.which("gcc") or "gcc"
            extra_make_args += [f"CC=ccache {cc}", f"HOSTCC=ccache {cc}"]
            log.info("ccache detected — using `CC=ccache gcc` for faster rebuilds")
        if not Path.home().joinpath(".ccache").exists() and not os.environ.get("CCACHE_DIR"):
            log.info("  tip: `ccache -M 25G` once to size the cache for a full kernel build")
    else:
        log.info("ccache not installed (optional; `sudo apt install ccache` for 5-10x faster rebuilds)")

    if not ctx.dry_run:
        _ensure_build_deps(src)

    if not ctx.dry_run and not ctx.non_interactive:
        if not confirm(
            f"start build with -j{jobs} (estimated 10-25 min on this CPU)?", default=True
        ):
            raise SystemExit(0)

    if ctx.dry_run:
        log.info(f"(dry-run) would run: make -j{jobs} bindeb-pkg")
        log.info(
            f"(dry-run) would emit linux-{{image,headers,libc-dev}}-…-{profile.flavour}_{pkgver}_amd64.deb"
            f" into {src.parent}"
        )
        ctx.built_debs = []
        return []

    log_file = log.log_dir() / f"build-{__import__('datetime').datetime.now():%Y%m%d-%H%M%S}.log"
    log.info(f"build log: {log_file}")

    cmd = ["make", "-j", str(jobs), *extra_make_args, "bindeb-pkg"]
    rc = _run_make_with_progress(cmd, src, env, log_file)
    if rc != 0:
        _surface_build_error(log_file)
        raise RuntimeError(f"make bindeb-pkg failed (exit {rc})")

    out_dir = src.parent
    # linux-hyphaed-tools* is built separately below, never by bindeb-pkg —
    # excluded here so a stale leftover from a prior run (same deterministic
    # version string => same filename, thanks to the SOURCE_DATE_EPOCH fix)
    # can't get picked up and later duplicated when _build_tools_deb appends
    # a freshly-built one with the same name.
    debs = sorted(
        d for d in out_dir.glob("linux-*.deb")
        if not d.name.startswith("linux-hyphaed-tools")
    )
    log.ok(f"produced {len(debs)} kernel .deb package(s) in {out_dir}")
    for d in debs:
        log.console.print(f"  • {d.name}")

    # Offer to build kernel tools (perf, bpftool, cpupower, turbostat, …)
    build_tools = ctx.non_interactive or confirm(
        "build kernel tools package (perf, bpftool, cpupower, turbostat)?",
        default=True,
    )
    if build_tools:
        tools_deb = _build_tools_deb(src, pkgver, profile.flavour, jobs)
        if tools_deb is not None:
            debs = [*debs, tools_deb]

    ctx.built_debs = debs
    return debs
