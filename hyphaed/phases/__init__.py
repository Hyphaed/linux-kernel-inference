from . import detect, source, patch, configure, security, build, package, install, postinstall

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
    "postinstall": postinstall,
}
