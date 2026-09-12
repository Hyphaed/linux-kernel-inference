# Thin convenience wrapper around `python -m hyphaed`.
# Real logic lives in the Python package.

PY ?= python3

# Keep this in step with the rules below, both ways. A name here with no
# rule makes `make <name>` print "Nothing to be done" and exit 0 -- that is
# how `make install` came to silently do nothing (2026-08-26). A rule missing
# from here breaks the day a file of that name appears in the tree.
# tests/test_makefile_targets.py checks both directions.
.PHONY: help detect deps fetch-patches fetch-patches-discover build build-package \
        build-dry install rebase clean list-presets status verify prune uninstall \
        print-config install-deps test compare snapshot doctor update-cmdline \
        scx-install scx-status scx-run scx-stop scx-enable scx-disable

help:
	@echo "hyphaed kernel wizard targets:"
	@echo "  make deps             install build dependencies via apt"
	@echo "  make install-deps     same as \`deps\`"
	@echo "  make detect           show detected hardware profile"
	@echo "  make print-config     print the composed cmdline + selected fragments"
	@echo "  make list-presets     list available presets"
	@echo "  make fetch-patches    download + verify vendored patches"
	@echo "  make build-package    RECOMMENDED single command: fetch -> patch -> configure ->"
	@echo "                        build -> package (kernel.org --target default + gaming-ai-vm"
	@echo "                        by default, see hyphaed/cli.py). Produces .debs only —"
	@echo "                        stops before touching your"
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
	@echo "  make scx-install / scx-status / scx-run SCHED=bpfland (default)"
	@echo "  make scx-enable SCHED=bpfland install persistent systemd unit"
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

# The other half of build-package. `--phase postinstall` auto-runs its unmet
# prerequisites in the same process, which after build-package is just
# install + postinstall: dpkg -i on out/debs/linux-*.deb ONLY, then the
# idempotent GRUB drop-in, the DKMS autoinstall and the NVIDIA ENDBR/IBT +
# nvidia-fs smoke checks.
#
# This target did not exist until 2026-08-26, and `install` was already in
# .PHONY above -- so `make install` printed "Nothing to be done for
# 'install'" and exited 0. A documented command that silently does nothing
# and reports success is worse than a missing one: CLAUDE.md tells the
# operator to run `make install` rather than `dpkg -i *`, and 7.1.10 was
# installed with `dpkg -i *` anyway. Without the .PHONY entry make would at
# least have said "No rule to make target".
install:
	$(PY) -m hyphaed --phase postinstall

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
	$(PY) -m hyphaed scx run $(or $(SCHED),bpfland)

scx-stop:
	$(PY) -m hyphaed scx stop

scx-enable:
	$(PY) -m hyphaed scx enable $(or $(SCHED),bpfland)

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
	rm -rf build/* out/debs/* out/logs/* out/manifest.json out/System.map-* state/*.json
