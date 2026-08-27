# Build the two userspace probes the live-kernel suite shells out to.
#
#   make            build both
#   make clean      remove built artefacts (vmlinux.h included)
#
# vmlinux.h is generated from the RUNNING kernel's BTF, never committed —
# it is 167k lines and is only correct for the kernel that produced it.

CFLAGS  ?= -O2 -Wall -Wextra
BPF_CFLAGS := -target bpf -D__TARGET_ARCH_x86 -O2 -g -Wall

all: dmabuf_ioctl_probe cache_ext_probe cache_ext_probe.bpf.o

dmabuf_ioctl_probe: dmabuf_ioctl_probe.c
	$(CC) $(CFLAGS) -o $@ $<

vmlinux.h:
	bpftool btf dump file /sys/kernel/btf/vmlinux format c > $@

cache_ext_probe.bpf.o: cache_ext_probe.bpf.c vmlinux.h
	clang $(BPF_CFLAGS) -c $< -o $@

cache_ext_probe: cache_ext_probe.c
	$(CC) $(CFLAGS) -o $@ $< -lbpf

clean:
	rm -f dmabuf_ioctl_probe cache_ext_probe cache_ext_probe.bpf.o vmlinux.h

.PHONY: all clean
