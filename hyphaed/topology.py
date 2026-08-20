from __future__ import annotations
import json
import os
import platform
import re
import shutil
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .util import log
from .util.run import run


INTEL_CODENAMES = {
    # family 6 model -> codename (subset relevant to recent client CPUs)
    151: "alderlake",
    154: "alderlake",
    183: "raptorlake-r",      # i9-14900K(F), i7-14700K(F), i5-14600K  (family 6, model 183)
    186: "raptorlake",
    191: "raptorlake",
    170: "meteorlake",
    173: "lunarlake",
    174: "arrowlake",
}

# AMD family (int) -> list of (model_lo, model_hi, codename)
AMD_CODENAMES: dict[int, list[tuple[int, int, str]]] = {
    0x17: [(0x00, 0xFF, "zen2")],
    0x19: [
        (0x00, 0x0F, "milan"),        # EPYC Zen3
        (0x20, 0x2F, "vermeer"),      # Ryzen 5000 desktop
        (0x40, 0x4F, "rembrandt"),    # Ryzen 6000 (Zen3+)
        (0x50, 0x5F, "cezanne"),      # Ryzen 5000G APU
        (0x60, 0x6F, "raphael"),      # Ryzen 7000 desktop
        (0x70, 0x7F, "phoenix"),      # Ryzen 7040 mobile
    ],
    0x1A: [
        (0x00, 0x0F, "strixhalo"),    # Ryzen AI Max (large APU)
        (0x20, 0x2F, "strixpoint"),   # Ryzen AI 300 (Zen5+Zen5c, this laptop)
        (0x40, 0x4F, "graniteridge"), # Ryzen 9000 desktop
        (0x60, 0x6F, "krackanpoint"),
        (0x70, 0x7F, "strixhalo2"),
    ],
}

# Chassis type integers (from /sys/class/dmi/id/chassis_type)
_LAPTOP_CHASSIS = {8, 9, 10, 14, 31, 32}   # Portable, Laptop, Notebook, SubNotebook, ...
_DESKTOP_CHASSIS = {3, 4, 5, 6, 7, 15, 16}


@dataclass
class HardwareProfile:
    cpu_vendor: str = "unknown"          # intel | amd
    cpu_codename: str = "unknown"
    cpu_family: int = 0
    cpu_model: int = 0
    cpu_brand: str = ""
    p_cores: int = 0
    e_cores: int = 0
    total_cores: int = 0
    total_threads: int = 0
    smt: bool = False
    p_thread_mask: str = ""              # e.g. "0-15"
    e_thread_mask: str = ""              # e.g. "16-31"
    numa_nodes: int = 1
    ram_gb: int = 0

    gpu_vendor: str = "none"             # nvidia | amd | intel | none
    gpu_arch: str = "unknown"            # blackwell, ada, hopper, ampere, rdna3, ...
    gpu_model: str = ""
    gpu_vram_gb: int = 0
    has_amd_gpu: bool = False
    has_intel_gpu: bool = False

    iommu_present: bool = False
    secure_boot: bool = False
    session_type: str = "unknown"        # wayland | x11 | tty
    desktop: str = "unknown"             # gnome | kde | ...
    boot_free_mb: int = 0
    running_kernel: str = ""
    ubuntu_release: str = ""
    has_vmware: bool = False
    has_greenboost: bool = False

    flavour: str = "hyphaed"
    build_metadata: str = ""             # rptr-nvbw-wl

    # Per-machine identity (defaulted for backward compat with old profile JSON)
    chassis: str = "unknown"             # laptop | desktop | unknown
    hostname: str = ""
    machine_id: str = ""                 # first 8 hex of /etc/machine-id

    def tag(self) -> str:
        # e.g. "rptr-nvbw-wl" or "strx-nvbw-wl"
        parts = []
        codemap = {
            "raptorlake-r": "rptr",
            "raptorlake": "rptr",
            "alderlake": "adlk",
            "meteorlake": "mtlk",
            "arrowlake": "arwl",
            "lunarlake": "lnlk",
            "strixpoint": "strx",
            "strixhalo": "strxh",
            "strixhalo2": "strxh2",
            "graniteridge": "grrd",
            "phoenix": "phx",
            "raphael": "rph",
            "vermeer": "vrm",
            "rembrandt": "rmb",
            "milan": "mil",
            "zen2": "zn2",
        }
        parts.append(codemap.get(self.cpu_codename, self.cpu_codename[:4] or "cpu"))
        gpumap = {
            "blackwell": "nvbw",
            "ada": "nvad",
            "hopper": "nvhp",
            "ampere": "nvam",
            "turing": "nvtu",
            "rdna3": "amrd3",
            "rdna4": "amrd4",
        }
        parts.append(gpumap.get(self.gpu_arch, self.gpu_vendor[:4] or "gpu"))
        if self.session_type == "wayland":
            parts.append("wl")
        elif self.session_type == "x11":
            parts.append("x11")
        return "-".join(parts)

    def kdeb_pkgversion(self, base_pkgver: str, build_num: int = 1) -> str:
        # base_pkgver like "7.0.0-15.15" → "7.0.0-15.15hyphaed1+rptr-nvbw-wl"
        return f"{base_pkgver}{self.flavour}{build_num}+{self.tag()}"

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path) -> "HardwareProfile":
        return cls(**json.loads(path.read_text()))


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _cmd_out(cmd: list[str]) -> str:
    """Read-only probe — must run even under --dry-run."""
    try:
        r = run(cmd, check=False, capture=True, force=True)
        return r.stdout
    except Exception:
        return ""


def _amd_cpu_topology(base: Path = Path("/sys/devices/system/cpu")) -> tuple[int, int, str, str]:
    """Return (p_cores, e_cores, p_mask, e_mask) for AMD CPUs.

    Uses acpi_cppc/highest_perf to distinguish Zen 5 (high perf) from Zen 5c
    (lower perf) cores on hybrid APUs like Strix Point. Threads at ≥90% of the
    max highest_perf value are classified as P-threads; the rest are E-threads.

    On non-hybrid AMD (all threads same perf), returns all threads as P-cores
    and an empty E-mask. Falls back to the same all-P result if cppc sysfs is absent.
    """
    cpus = sorted(int(p.name.removeprefix("cpu")) for p in base.glob("cpu[0-9]*"))
    if not cpus:
        return 0, 0, "", ""

    # Try acpi_cppc/highest_perf per thread
    perf: dict[int, int] = {}
    for c in cpus:
        cppc_file = base / f"cpu{c}" / "acpi_cppc" / "highest_perf"
        if cppc_file.exists():
            try:
                perf[c] = int(cppc_file.read_text().strip())
            except (ValueError, OSError):
                pass

    if not perf:
        # No cppc — all threads are P (single-tier AMD)
        p_cores = _count_physical_cores(base, cpus)
        return p_cores, 0, _mask(cpus), ""

    max_perf = max(perf.values())
    threshold = int(max_perf * 0.90)
    p_threads = sorted(c for c, v in perf.items() if v >= threshold)
    e_threads = sorted(c for c, v in perf.items() if v < threshold)

    # All same tier → non-hybrid
    if not e_threads:
        p_cores = _count_physical_cores(base, p_threads)
        return p_cores, 0, _mask(p_threads), ""

    p_cores = _count_physical_cores(base, p_threads)
    e_cores = _count_physical_cores(base, e_threads)
    return p_cores, e_cores, _mask(p_threads), _mask(e_threads)


def _intel_cpu_topology(base: Path = Path("/sys/devices/system/cpu")) -> tuple[int, int, str, str]:
    """Return (p_cores, e_cores, p_mask, e_mask).

    Strategy (most reliable first):
      1) /sys/devices/system/cpu/types/intel_{core,atom}_*/cpus — explicit when present.
      2) thread_siblings_list — P-cores have HT (≥2 siblings), E-cores don't (=1).
      3) cpu_capacity tiebreaker.
    """
    cpus = sorted(int(p.name.removeprefix("cpu")) for p in base.glob("cpu[0-9]*"))
    if not cpus:
        return 0, 0, "", ""

    # 1) Explicit hybrid types directory (newer kernels)
    types_dir = base / "types"
    p_threads: list[int] = []
    e_threads: list[int] = []
    if types_dir.is_dir():
        for d in types_dir.iterdir():
            cpus_file = d / "cpus"
            if not cpus_file.exists():
                continue
            cpu_list = _parse_cpu_list(cpus_file.read_text().strip())
            if "core" in d.name:
                p_threads.extend(cpu_list)
            elif "atom" in d.name:
                e_threads.extend(cpu_list)
        if p_threads or e_threads:
            p_cores = _count_physical_cores(base, p_threads)
            e_cores = _count_physical_cores(base, e_threads)
            return p_cores, e_cores, _mask(p_threads), _mask(e_threads)

    # 2) Fallback: thread_siblings_list — P has HT sibling, E doesn't
    p_set: set[int] = set()
    e_set: set[int] = set()
    seen_cores_p: set[int] = set()
    seen_cores_e: set[int] = set()
    for c in cpus:
        sib_path = base / f"cpu{c}" / "topology" / "thread_siblings_list"
        cid_path = base / f"cpu{c}" / "topology" / "core_id"
        if not sib_path.exists() or not cid_path.exists():
            continue
        siblings = _parse_cpu_list(sib_path.read_text().strip())
        try:
            cid = int(cid_path.read_text())
        except ValueError:
            continue
        if len(siblings) >= 2:
            p_set.add(c)
            seen_cores_p.add(cid)
        else:
            e_set.add(c)
            seen_cores_e.add(cid)

    return len(seen_cores_p), len(seen_cores_e), _mask(sorted(p_set)), _mask(sorted(e_set))


def _parse_cpu_list(s: str) -> list[int]:
    """Expand '0-3,8,10-11' to [0,1,2,3,8,10,11]."""
    out: list[int] = []
    for token in s.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(token))
    return out


def _count_physical_cores(base: Path, threads: list[int]) -> int:
    """Count distinct core_id values across the given threads."""
    cores: set[int] = set()
    for t in threads:
        cid_file = base / f"cpu{t}" / "topology" / "core_id"
        if cid_file.exists():
            try:
                cores.add(int(cid_file.read_text()))
            except ValueError:
                pass
    return len(cores)


def _mask(threads: list[int]) -> str:
    if not threads:
        return ""
    threads = sorted(set(threads))
    ranges = []
    start = prev = threads[0]
    for t in threads[1:]:
        if t == prev + 1:
            prev = t
            continue
        ranges.append((start, prev))
        start = prev = t
    ranges.append((start, prev))
    return ",".join(f"{a}-{b}" if a != b else str(a) for a, b in ranges)


def _detect_cpu(profile: HardwareProfile) -> None:
    cpuinfo = _read("/proc/cpuinfo")
    for line in cpuinfo.splitlines():
        if line.startswith("vendor_id"):
            v = line.split(":", 1)[1].strip()
            profile.cpu_vendor = "intel" if "Intel" in v else ("amd" if "AMD" in v else v.lower())
        elif line.startswith("cpu family"):
            try:
                profile.cpu_family = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("model") and "name" not in line:
            try:
                profile.cpu_model = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("model name"):
            profile.cpu_brand = line.split(":", 1)[1].strip()
            break

    if profile.cpu_vendor == "intel":
        profile.cpu_codename = INTEL_CODENAMES.get(profile.cpu_model, f"intel-fam{profile.cpu_family}-mod{profile.cpu_model}")
    elif profile.cpu_vendor == "amd":
        fam = profile.cpu_family
        mod = profile.cpu_model
        codename = f"amd-fam{fam:x}"
        for lo, hi, name in AMD_CODENAMES.get(fam, []):
            if lo <= mod <= hi:
                codename = name
                break
        profile.cpu_codename = codename

    if profile.cpu_vendor == "intel":
        p, e, pm, em = _intel_cpu_topology()
        profile.p_cores = p
        profile.e_cores = e
        profile.p_thread_mask = pm
        profile.e_thread_mask = em
    elif profile.cpu_vendor == "amd":
        p, e, pm, em = _amd_cpu_topology()
        profile.p_cores = p
        profile.e_cores = e
        profile.p_thread_mask = pm
        profile.e_thread_mask = em
    profile.total_cores = profile.p_cores + profile.e_cores
    profile.total_threads = os.cpu_count() or 0
    profile.smt = profile.total_threads > profile.total_cores

    # NUMA
    numa = list(Path("/sys/devices/system/node").glob("node[0-9]*"))
    profile.numa_nodes = max(1, len(numa))


def _detect_ram(profile: HardwareProfile) -> None:
    mem = _read("/proc/meminfo")
    for line in mem.splitlines():
        if line.startswith("MemTotal:"):
            kb = int(line.split()[1])
            profile.ram_gb = round(kb / 1024 / 1024)
            return


def _detect_gpu(profile: HardwareProfile) -> None:
    if shutil.which("nvidia-smi"):
        out = _cmd_out(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
        if out.strip():
            first = out.strip().splitlines()[0]
            parts = [p.strip() for p in first.split(",")]
            if parts:
                profile.gpu_vendor = "nvidia"
                profile.gpu_model = parts[0]
                if len(parts) > 1:
                    try:
                        profile.gpu_vram_gb = round(int(parts[1]) / 1024)
                    except ValueError:
                        pass
                model = profile.gpu_model.lower()
                if "rtx 50" in model or "gb20" in model or "gb10" in model:
                    profile.gpu_arch = "blackwell"
                elif "rtx 40" in model or "ad10" in model:
                    profile.gpu_arch = "ada"
                elif "h100" in model or "h200" in model:
                    profile.gpu_arch = "hopper"
                elif "rtx 30" in model or "a100" in model:
                    profile.gpu_arch = "ampere"
                elif "rtx 20" in model or "rtx 16" in model:
                    profile.gpu_arch = "turing"

    # Also detect secondary/integrated cards via lspci
    # Match VGA, 3D, and Display controller class IDs — AMD iGPUs (e.g. Radeon
    # 880M on Strix Point) appear as "Display controller", not "VGA".
    lspci = _cmd_out(["lspci", "-nn"])
    _amd_re = re.compile(r"(VGA|3D|Display controller).*Advanced Micro Devices", re.IGNORECASE)
    _intel_re = re.compile(r"VGA.*Intel", re.IGNORECASE)
    profile.has_amd_gpu = any(_amd_re.search(line) for line in lspci.splitlines())
    profile.has_intel_gpu = any(_intel_re.search(line) for line in lspci.splitlines())


def _detect_iommu(profile: HardwareProfile) -> None:
    profile.iommu_present = Path("/sys/class/iommu").exists() and any(Path("/sys/class/iommu").iterdir())


def _detect_secureboot(profile: HardwareProfile) -> None:
    if not shutil.which("mokutil"):
        profile.secure_boot = False
        return
    out = _cmd_out(["mokutil", "--sb-state"]).lower()
    profile.secure_boot = "enabled" in out


def _detect_session(profile: HardwareProfile) -> None:
    profile.session_type = os.environ.get("XDG_SESSION_TYPE", "unknown") or "unknown"
    profile.desktop = (os.environ.get("XDG_CURRENT_DESKTOP", "") or "").lower().split(":")[-1] or "unknown"
    if profile.desktop == "unknown":
        # Best-effort fallback
        if Path("/usr/bin/gnome-shell").exists():
            profile.desktop = "gnome"
        elif Path("/usr/bin/plasmashell").exists():
            profile.desktop = "kde"


def _detect_boot_free(profile: HardwareProfile) -> None:
    try:
        st = os.statvfs("/boot")
        profile.boot_free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
    except OSError:
        profile.boot_free_mb = 0


def _detect_chassis(profile: HardwareProfile) -> None:
    try:
        ct = int(Path("/sys/class/dmi/id/chassis_type").read_text().strip())
        if ct in _LAPTOP_CHASSIS:
            profile.chassis = "laptop"
        elif ct in _DESKTOP_CHASSIS:
            profile.chassis = "desktop"
        else:
            profile.chassis = "unknown"
    except (OSError, ValueError):
        profile.chassis = "unknown"


def _detect_misc(profile: HardwareProfile) -> None:
    profile.running_kernel = _cmd_out(["uname", "-r"]).strip()
    if Path("/etc/os-release").exists():
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("VERSION_ID="):
                profile.ubuntu_release = line.split("=", 1)[1].strip('"')
                break
    profile.has_vmware = Path("/usr/lib/vmware").exists() or Path("/usr/bin/vmware").exists()
    profile.has_greenboost = Path.home().joinpath("Dev/greenboost_all/greenboost").exists() \
        or Path("/dev/greenboost").exists() \
        or Path("/sys/class/greenboost").exists()
    profile.hostname = platform.node()
    try:
        mid = Path("/etc/machine-id").read_text().strip()
        profile.machine_id = mid[:8]
    except OSError:
        profile.machine_id = ""


def detect() -> HardwareProfile:
    p = HardwareProfile()
    _detect_cpu(p)
    _detect_ram(p)
    _detect_gpu(p)
    _detect_iommu(p)
    _detect_secureboot(p)
    _detect_session(p)
    _detect_boot_free(p)
    _detect_misc(p)
    _detect_chassis(p)
    p.build_metadata = p.tag()
    return p
