#!/usr/bin/env python3
"""Keep ONE copy of each duplicated git clone (the newest), and remove the listed trees.

Dry run by default: prints the plan and the space it would free, changes nothing.
  python3 scripts/dedupe-clones.py                      # plan only
  sudo python3 scripts/dedupe-clones.py --go            # do it (asks you to type DELETE)
  sudo python3 scripts/dedupe-clones.py --go --yes      # no question
  ... --force /path/to/blocked/copy                     # also remove one copy the safety checks blocked

What "newest" means: the copy whose HEAD commit is newest; a tie goes to the copy whose .git was touched last.
What happens to the other copies: they are replaced by a symlink to the kept copy, so every path that other projects
and build files use still resolves (--no-symlink deletes them outright instead).

Safety (a copy is SKIPPED, and the reason printed, when any of these is true):
  - tracked files modified, commits no remote has, stashes, or more than 5 MB of untracked files
  - a process has its working directory inside it
  - it is a git submodule of another repository
  - a repository nested inside it has any of the above
Small untracked files (<= 5 MB) are copied to the backup folder first. Every removal is recorded in a manifest
(path, remote URL, commit, branch) under ~/Documents/removed-clones/<date>/, so any copy can be cloned again.
A forced removal also saves `git diff` and a bundle of the local-only commits there.
"""
from __future__ import annotations

import argparse
import collections
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# remotes (host/owner/name, lower case) that must end up with a single copy
TARGETS = (
    "github.com/valvesoftware/proton",
    "github.com/lightricks/ltx-2",
    "github.com/nvidia/dcgm",
    "github.com/nvidia/tensorrt-llm",
    "github.com/nvidia/dlss",
    "github.com/triton-lang/triton",
    "github.com/godotengine/godot",
    "github.com/paddlepaddle/paddleocr",
    "github.com/vllm-project/vllm",
)
# whole trees to remove (owner request 2026-10-31)
REMOVE = ("Dev/github_gaming/RTX-Kit", "Documents/linux-tkg")
UNTRACKED_LIMIT = 5 * 1024 * 1024


def home() -> Path:
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "ferran"
    return Path(pwd.getpwnam(user).pw_dir)


def sh(args: list[str], cwd: Path | None = None) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    return r.stdout.strip()


def git(repo: Path, *a: str) -> str:
    return sh(["git", "-c", "safe.directory=*", "-c", "core.fileMode=false", "-C", str(repo), *a])


def norm(url: str) -> str:
    u = re.sub(r"^(git@|https?://|ssh://git@)", "", url.strip())
    return re.sub(r"(\.git)?/?$", "", u.replace(":", "/", 1) if "@" in url and "://" not in url else u).lower()


def du_kb(p: Path) -> int:
    out = sh(["du", "-sk", str(p)])
    return int(out.split()[0]) if out else 0


def discover(root: Path) -> list[Path]:
    cmd = ["find", str(root), "-xdev",
           "(", "-name", "node_modules", "-o", "-name", ".cache", "-o", "-name", ".miniforge3",
           "-o", "-path", "*/.local/share/Steam", ")", "-prune", "-o",
           "-type", "d", "-name", ".git", "-print", "-prune"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False).stdout.split("\n")
    return sorted(Path(x).parent for x in out if x)


class Repo:
    def __init__(self, path: Path):
        self.path = path
        url = git(path, "config", "--get", "remote.origin.url")
        self.remote = norm(url) if url else ""
        self.url = url
        self.sha = git(path, "rev-parse", "HEAD")
        self.branch = git(path, "symbolic-ref", "-q", "--short", "HEAD")
        ct = git(path, "log", "-1", "--format=%ct", "HEAD")
        self.head_time = int(ct) if ct.isdigit() else 0
        try:
            self.touched = max((path / ".git" / n).stat().st_mtime for n in ("HEAD", "FETCH_HEAD", "index") if (path / ".git" / n).exists())
        except (ValueError, OSError):
            self.touched = 0.0

    def blockers(self, ignore_untracked: bool = False) -> list[str]:
        why = []
        st = git(self.path, "status", "--porcelain", "--untracked-files=no").splitlines()
        if st:
            why.append(f"{len(st)} modified tracked files/submodules")
        n = git(self.path, "rev-list", "--branches", "--not", "--remotes", "--count")
        if n.isdigit() and int(n) > 0:
            why.append(f"{n} commits no remote has")
        if git(self.path, "stash", "list"):
            why.append("has stashes")
        size = 0
        for f in git(self.path, "ls-files", "-o", "--exclude-standard").splitlines():
            try:
                size += (self.path / f).stat().st_size
            except OSError:
                pass
        if size > UNTRACKED_LIMIT and not ignore_untracked:
            why.append(f"{size // 1048576} MB of untracked files")
        return why

    def untracked_small(self) -> list[str]:
        return [f for f in git(self.path, "ls-files", "-o", "--exclude-standard").splitlines()
                if (self.path / f).is_file()]


def procs_inside(path: Path) -> list[str]:
    hit = []
    p = str(path)
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        if cwd == p or cwd.startswith(p + "/"):
            hit.append(f"{pid}:{Path(f'/proc/{pid}/comm').read_text().strip()}")
    return hit


def is_submodule(path: Path) -> bool:
    parent = path.parent
    while parent != parent.parent:
        if (parent / ".git").exists():
            rel = str(path.relative_to(parent))
            return git(parent, "ls-files", "-s", "--", rel).startswith("160000")
        parent = parent.parent
    return False


def under(a: Path, b: Path) -> bool:
    return a == b or str(a).startswith(str(b) + "/")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--go", action="store_true", help="really remove (default: plan only)")
    ap.add_argument("--yes", action="store_true", help="do not ask to type DELETE")
    ap.add_argument("--no-symlink", action="store_true", help="delete the extra copies, do not leave a symlink")
    ap.add_argument("--force", action="append", default=[], metavar="PATH", help="remove this blocked copy anyway (repeatable)")
    ap.add_argument("--all", action="store_true", help="every remote cloned more than once, not only the listed ones")
    ap.add_argument("--root", default=None, help="tree to scan (default: the home of the invoking user)")
    a = ap.parse_args()

    h = home()
    root = Path(a.root) if a.root else h
    forced = {Path(x).resolve() for x in a.force}

    print(f"scanning {root} for git repositories ...", flush=True)
    repos = [Repo(p) for p in discover(root)]
    print(f"{len(repos)} repositories")
    doomed = [(h / r).resolve() for r in REMOVE if (h / r).exists()]

    groups: dict[str, list[Repo]] = collections.defaultdict(list)
    for r in repos:
        if r.remote and (a.all or r.remote in TARGETS):
            groups[r.remote].append(r)

    actions: list[dict] = []   # {path, keeper, why_skipped, kb, repo}
    for remote, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        pool = [m for m in members if not any(under(m.path.resolve(), d) for d in doomed)]
        if not pool:
            continue   # every copy lives inside a tree that is removed anyway
        keeper = max(pool, key=lambda m: (m.head_time, m.touched))
        for m in members:
            if m is keeper or any(under(m.path.resolve(), d) for d in doomed):
                continue
            if m.path.is_symlink() or os.path.realpath(m.path) == os.path.realpath(keeper.path):
                continue
            actions.append({"repo": m, "keeper": keeper, "remote": remote})

    for d in doomed:
        r = next((x for x in repos if x.path.resolve() == d), None)
        actions.append({"repo": r, "path": d, "keeper": None, "remote": "(removal list)"})

    plan = []
    for act in actions:
        r: Repo | None = act["repo"]
        path = act.get("path") or r.path.resolve()
        why: list[str] = []
        nested = [x for x in repos if x.path.resolve() != path and under(x.path.resolve(), path)]
        explicit = act["remote"] == "(removal list)"   # removal asked for by name: big untracked files (build output) do not block
        for x in ([r] if r else []) + nested:
            if x is None:
                continue
            for b in x.blockers(ignore_untracked=explicit):
                why.append(f"{x.path.relative_to(path) if x.path.resolve() != path else '.'}: {b}")
        for x in nested:
            for g in groups.values():
                if len(g) > 1 and max((m for m in g if not any(under(m.path.resolve(), d) for d in doomed)),
                                      key=lambda m: (m.head_time, m.touched), default=None) is x:
                    why.append(f"contains the kept copy of {x.remote}")
        pr = procs_inside(path)
        if pr:
            why.append("in use by " + ",".join(pr[:3]))
        if r and is_submodule(path):
            why.append("is a submodule of another repository")
        act.update(path=path, why=why, kb=du_kb(path))
        act["forced"] = path in forced
        plan.append(act)

    plan.sort(key=lambda x: -x["kb"])
    total = skipped = 0
    for p in plan:
        todo = (not p["why"]) or p["forced"]
        tag = "REMOVE" if todo else "SKIP  "
        k = p["keeper"]
        print(f"\n{tag} {p['kb']/1048576:6.2f} GB  {p['path']}")
        print(f"       {p['remote']}" + (f"  -> keep {k.path}  ({time.strftime('%Y-%m-%d', time.localtime(k.head_time))} {k.sha[:8]})" if k else ""))
        for w in p["why"]:
            print(f"       {'forced past: ' if p['forced'] else 'blocked: '}{w}")
        if todo:
            total += p["kb"]
        else:
            skipped += p["kb"]
    print(f"\nwould free {total/1048576:.1f} GB;  {skipped/1048576:.1f} GB blocked by the safety checks"
          f" (use --force PATH after reading why)")
    if not a.go:
        print("(plan only. To do it: sudo python3 scripts/dedupe-clones.py --go)")
        return 0
    if not a.yes and input("type DELETE to continue: ").strip() != "DELETE":
        print("cancelled")
        return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bk = h / "Documents" / "removed-clones" / stamp
    bk.mkdir(parents=True, exist_ok=True)
    man = (bk / "manifest.tsv").open("w")
    man.write("path\tremote_url\tcommit\tbranch\tkept_copy\tkb\n")
    for p in plan:
        if p["why"] and not p["forced"]:
            continue
        r: Repo | None = p["repo"]
        path: Path = p["path"]
        dest = bk / "files" / str(path).lstrip("/").replace("/", "__")
        for x in ([r] if r else []) + [y for y in repos if y.path.resolve() != path and under(y.path.resolve(), path)]:
            if x is None:
                continue
            for f in x.untracked_small():
                src = x.path / f
                try:
                    if src.stat().st_size <= UNTRACKED_LIMIT:
                        t = dest / x.path.relative_to(path) / f
                        t.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, t, follow_symlinks=False)
                except OSError:
                    pass
            if p["forced"]:
                dest.mkdir(parents=True, exist_ok=True)
                (dest / f"{x.path.name}.diff").write_text(git(x.path, "diff", "HEAD"))
                subprocess.run(["git", "-c", "safe.directory=*", "-C", str(x.path), "bundle", "create",
                                str(dest / f"{x.path.name}.bundle"), "--branches", "--not", "--remotes"],
                               capture_output=True, check=False)
        man.write(f"{path}\t{r.url if r else ''}\t{r.sha if r else ''}\t{r.branch if r else ''}\t"
                  f"{p['keeper'].path if p['keeper'] else ''}\t{p['kb']}\n")
        man.flush()
        print(f"removing {path}")
        subprocess.run(["rm", "-rf", str(path)], check=True)
        if p["keeper"] and not a.no_symlink:
            os.symlink(p["keeper"].path.resolve(), path)
            if os.geteuid() == 0:
                pw = pwd.getpwnam(os.environ.get("SUDO_USER", "root"))
                os.lchown(path, pw.pw_uid, pw.pw_gid)
    man.close()
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        pw = pwd.getpwnam(os.environ["SUDO_USER"])
        subprocess.run(["chown", "-R", f"{pw.pw_uid}:{pw.pw_gid}", str(bk)], check=False)
    print(f"\ndone. manifest and backups: {bk}")
    print("free space now:", sh(["df", "-h", "--output=avail", str(root)]).split("\n")[-1].strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
