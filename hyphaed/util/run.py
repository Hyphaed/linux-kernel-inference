from __future__ import annotations
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import log


_DRY_RUN = False


def set_dry_run(enabled: bool) -> None:
    """Module-wide dry-run toggle. When set, run()/run_sudo() print the
    command but don't execute it. Set once from CLI on startup.
    """
    global _DRY_RUN
    _DRY_RUN = enabled


@dataclass
class RunResult:
    code: int
    stdout: str
    stderr: str
    cmd: str

    def ok(self) -> bool:
        return self.code == 0

    def check(self) -> "RunResult":
        if self.code != 0:
            raise RuntimeError(
                f"command failed (exit {self.code}): {self.cmd}\n"
                f"stderr tail:\n{self.stderr[-2000:]}"
            )
        return self


def _format_cmd(cmd: Sequence[str] | str) -> str:
    if isinstance(cmd, str):
        return cmd
    return " ".join(shlex.quote(c) for c in cmd)


def run(
    cmd: Sequence[str] | str,
    *,
    cwd: Path | str | None = None,
    env: dict | None = None,
    check: bool = True,
    capture: bool = True,
    tee_log: Path | None = None,
    dry_run: bool = False,
    input_str: str | None = None,
    timeout: float | None = None,
    force: bool = False,
    on_line: Callable[[str], None] | None = None,
) -> RunResult:
    """Run a subprocess.

    force=True bypasses the module-level dry-run flag. Use it for read-only
    probes (uname, lspci, nvidia-smi, sysfs reads) — dry-run should suppress
    mutating actions, not hardware detection.
    """
    pretty = _format_cmd(cmd)
    if not force or not _DRY_RUN:
        log.console.print(f"[muted]$[/muted] {pretty}")
    if (dry_run or _DRY_RUN) and not force:
        return RunResult(0, "", "", pretty)

    shell = isinstance(cmd, str)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    if not capture and tee_log is None:
        # Direct passthrough for things like make menuconfig
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            shell=shell,
            input=input_str,
            timeout=timeout,
        )
        result = RunResult(proc.returncode, "", "", pretty)
    else:
        log_fh = tee_log.open("ab") if tee_log else None
        if log_fh:
            log_fh.write(f"# $ {pretty}\n".encode())
            log_fh.flush()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd) if cwd else None,
                env=full_env,
                shell=shell,
                stdin=subprocess.PIPE if input_str else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            out_parts: list[bytes] = []
            err_parts: list[bytes] = []
            if input_str:
                assert proc.stdin
                proc.stdin.write(input_str.encode())
                proc.stdin.close()

            # Stream stdout/stderr to log + collect
            import selectors
            sel = selectors.DefaultSelector()
            sel.register(proc.stdout, selectors.EVENT_READ, "out")
            sel.register(proc.stderr, selectors.EVENT_READ, "err")
            _line_bufs: dict[str, bytes] = {"out": b"", "err": b""}
            # The deadline has to be enforced HERE, not at the proc.wait()
            # below. This loop runs until both pipes hit EOF, which only
            # happens when the child exits — so a `timeout=` that is only
            # honoured by wait() is honoured after the wait is already over,
            # and bounds nothing. Found 2026-08-20: `hyphaed list-versions`
            # took 80 s on two `git fetch --tags` of the Linux tree and could
            # stall indefinitely on a bad network, with the wizard sitting on
            # "Fetching latest kernel.org stable releases…" forever.
            _deadline = (time.monotonic() + timeout) if timeout else None
            while sel.get_map():
                if _deadline is not None and time.monotonic() >= _deadline:
                    proc.kill()
                    proc.wait()
                    raise subprocess.TimeoutExpired(cmd, timeout,
                                                    output=b"".join(out_parts),
                                                    stderr=b"".join(err_parts))
                for key, _ in sel.select(timeout=1.0):
                    chunk = key.fileobj.read1(65536)
                    if not chunk:
                        if on_line is not None and _line_bufs[key.data]:
                            try:
                                on_line(_line_bufs[key.data].decode(errors="replace").rstrip("\r"))
                            except Exception:
                                pass
                            _line_bufs[key.data] = b""
                        sel.unregister(key.fileobj)
                        continue
                    if key.data == "out":
                        out_parts.append(chunk)
                    else:
                        err_parts.append(chunk)
                    if log_fh:
                        log_fh.write(chunk)
                        log_fh.flush()
                    if on_line is not None:
                        buf = _line_bufs[key.data] + chunk
                        lines = buf.split(b"\n")
                        _line_bufs[key.data] = lines[-1]
                        for line_bytes in lines[:-1]:
                            try:
                                on_line(line_bytes.decode(errors="replace").rstrip("\r"))
                            except Exception:
                                pass
            proc.wait(timeout=timeout)
            result = RunResult(
                proc.returncode,
                b"".join(out_parts).decode(errors="replace"),
                b"".join(err_parts).decode(errors="replace"),
                pretty,
            )
        finally:
            if log_fh:
                log_fh.close()

    if check and not result.ok():
        result.check()
    return result


def run_sudo(cmd: Sequence[str], **kw) -> RunResult:
    if os.geteuid() == 0:
        return run(list(cmd), **kw)
    return run(["sudo", *cmd], **kw)


def sudo_keepalive() -> bool:
    """Prime sudo credentials. Subsequent run_sudo calls use the cached
    credential (default ~15 min) and won't prompt again.

    Returns True on success.
    """
    if os.geteuid() == 0:
        return True
    r = subprocess.run(["sudo", "-v"], check=False)
    return r.returncode == 0


def need_root() -> bool:
    return os.geteuid() != 0
