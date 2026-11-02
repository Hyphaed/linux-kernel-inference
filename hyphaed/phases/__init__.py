from . import detect, source, patch, configure, security, build, package, install, postinstall, nvidia, desktop, proton

ORDER = [
    "detect",
    "source",
    "patch",
    "configure",
    "security",
    "build",
    "package",
    "install",
    "postinstall",
    "nvidia",
    "desktop",
    "proton",
]

MODULES = {
    "detect": detect,
    "source": source,
    "patch": patch,
    "configure": configure,
    "security": security,
    "build": build,
    "package": package,
    "install": install,
    "nvidia": nvidia,
    "desktop": desktop,
    "proton": proton,
    "postinstall": postinstall,
}
