# Thin convenience wrapper around `python -m hyphaed`.
# Real logic lives in the Python package.

PY ?= python3

.PHONY: help detect deps fetch-patches build build-package build-dry install rebase clean list-presets \
        status verify prune uninstall print-config install-deps test compare scx \
        snapshot scx-enable scx-disable doctor update-cmdline

help:
	@echo "hyphaed kernel wizard targets:"
	@echo "  make deps             install build dependencies via apt"
	@echo "  make install-deps     same as \`deps\`"
	@echo "  make detect           show detected hardware profile"
	@echo "  make print-config     print the composed cmdline + selected fragments"
	@echo "  make list-presets     list available presets"
	@echo "  make fetch-patches    download + verify vendored patches"
	@echo "  make build-package    RECOMMENDED single command: fetch -> patch -> configure ->"
	@echo "                        build -> package (kernel.org 7.1.3 + gaming-ai-vm by"
	@echo "                        default). Produces .debs only — stops before touching your"
	@echo "                        running system. Review, then \`make install\` when ready."
	@echo "  make build            same defaults, but runs ALL the way through install +"
	@echo "                        postinstall (dpkg -i + GRUB drop-in) in one shot"
	@echo "  make build-dry        preview build-package's plan, --dry-run (does not persist"
	@echo "                        phase-completion state, so it's safe to run before the"
	@echo "                        real thing without needing a state reset afterward)"
	@echo "  make status           report whether running kernel is hyphaed-built"
	@echo "  make verify           post-reboot health check"
	@echo "  make doctor           full system self-diagnostic"
	@echo "  make update-cmdline   apply GRUB cmdline drop-in (no kernel build)"
	@echo "  make snapshot         export hardware/build state as a YAML snapshot"
	@echo "  make compare A=<.config> B=<.config>"
	@echo "  make prune            remove old hyphaed kernels (KEEP=2 by default)"
	@echo "  make uninstall        dpkg --purge all hyphaed kernels"
	@echo "  make rebase TARGET=7.0.0-16-generic"
	@echo "  make scx-install / scx-status / scx-run SCHED=lavd"
	@echo "  make scx-enable SCHED=lavd    install persistent systemd unit"
	@echo "  make scx-disable              remove scx systemd unit"
	@echo "  make test             run pytest invariants"
	@echo "  make clean            remove build/, out/, state/"

deps:
	sudo apt install -y build-essential dpkg-dev fakeroot libncurses-dev \
	    libssl-dev libelf-dev bison flex dwarves rsync kmod cpio bc zstd \
	    python3 python3-pip python3-rich python3-yaml git \
	    libslang2-dev libcap-dev libbpf-dev libpci-dev binutils-dev

detect:
	$(PY) -m hyphaed detect

list-presets:
	$(PY) -m hyphaed list-presets

fetch-patches:
	$(PY) -m hyphaed fetch-patches

fetch-patches-discover:
	$(PY) -m patches.fetch --discover --try-branches

build:
	$(PY) -m hyphaed

build-dry:
	$(PY) -m hyphaed --dry-run

# `--phase package` auto-runs its unmet prerequisites (detect/source/patch/
# configure/security/build) IN THE SAME PROCESS before running package
# itself (see cli.py's `deps` map + `_run_phases`) — never chain separate
# `--phase X` subprocess calls here: each subprocess reloads ctx.profile
# from state/profile-*.json, which does NOT capture the in-memory
# profile.running_kernel retarget that source.py's _fetch_kernel_org/
# _fetch_xanmod do for --source-mode kernel-org|xanmod (confirmed
# 2026-07-09: chaining produced base_git_tag()/kdeb_pkgversion() computed
# from the HOST's running kernel instead of the fetched --target, corrupting
# the git-am 3-way baseline). One `--phase package` call keeps ctx alive
# across all of them, so the retarget sticks. Stops after `package` so
# .debs are produced but the running system is untouched; `make install`
# when ready.
build-package:
	$(PY) -m hyphaed --phase package

rebase:
	@if [ -z "$(TARGET)" ]; then echo "set TARGET=, e.g. make rebase TARGET=linux-image-7.0.0-16-generic"; exit 1; fi
	$(PY) -m hyphaed rebase --target $(TARGET)

install-deps:
	$(PY) -m hyphaed install-deps

status:
	$(PY) -m hyphaed status

verify:
	$(PY) -m hyphaed verify

prune:
	$(PY) -m hyphaed prune --keep $(or $(KEEP),2)

uninstall:
	$(PY) -m hyphaed uninstall

print-config:
	$(PY) -m hyphaed print-config

compare:
	@if [ -z "$(A)" ] || [ -z "$(B)" ]; then echo "set A= and B= to two .config paths"; exit 1; fi
	$(PY) -m hyphaed compare $(A) $(B)

scx-install:
	$(PY) -m hyphaed scx install

scx-status:
	$(PY) -m hyphaed scx status

scx-run:
	$(PY) -m hyphaed scx run $(or $(SCHED),lavd)

scx-stop:
	$(PY) -m hyphaed scx stop

scx-enable:
	$(PY) -m hyphaed scx enable $(or $(SCHED),lavd)

scx-disable:
	$(PY) -m hyphaed scx disable

doctor:
	$(PY) -m hyphaed doctor

update-cmdline:
	$(PY) -m hyphaed update-cmdline

snapshot:
	$(PY) -m hyphaed snapshot

test:
	$(PY) -m pytest tests/ -q

clean:
	rm -rf build/* out/debs/* out/logs/* state/*.json
