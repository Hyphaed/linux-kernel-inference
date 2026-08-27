// SPDX-License-Identifier: GPL-2.0
/*
 * cache_ext_probe — loader for cache_ext_probe.bpf.c.
 *
 * Loads the counting policy, attaches it as a page_cache_ext_ops
 * struct_ops link, points 0023's /proc/page_cache_ext_enabled_cgroup at a
 * cgroup we made, moves ourselves into it, then hammers the page cache and
 * reads the counters back.
 *
 * A non-zero folio_added/folio_accessed counter is the thing that
 * separates "the struct_ops type registered" from "0023's call sites in
 * mm/filemap.c and mm/vmscan.c survived the 6.6 -> 7.1 forward-port".
 *
 * Must run as root (BPF struct_ops load + cgroup creation). Undoes
 * everything it did on the way out, including clearing the enabled cgroup.
 */
#define _GNU_SOURCE
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define PROC_ENABLE	"/proc/page_cache_ext_enabled_cgroup"
#define CG_ROOT		"/sys/fs/cgroup"
#define CG_NAME		"cache_ext_probe"
#define SCRATCH		"/var/tmp/cache_ext_probe.dat"
#define SCRATCH_MB	512

enum { C_INIT, C_ADDED, C_ACCESSED, C_EVICTED, C_EVICT_CALL, C_EVICT_REQ, C_NR };

static const char *cnames[C_NR] = {
	"init", "folio_added", "folio_accessed", "folio_evicted",
	"evict_folios calls", "evict_folios folios requested",
};

static int write_file(const char *path, const char *val)
{
	int fd = open(path, O_WRONLY);
	ssize_t n;

	if (fd < 0)
		return -1;
	n = write(fd, val, strlen(val));
	close(fd);
	return n < 0 ? -1 : 0;
}

/* Make a scratch cgroup and move this process into it. */
static int setup_cgroup(char *path, size_t len)
{
	char procs[640];

	snprintf(path, len, "%s/%s", CG_ROOT, CG_NAME);
	if (mkdir(path, 0755) < 0 && errno != EEXIST) {
		fprintf(stderr, "mkdir %s: %s\n", path, strerror(errno));
		return -1;
	}
	snprintf(procs, sizeof(procs), "%s/cgroup.procs", path);
	if (write_file(procs, "0") < 0) {
		fprintf(stderr, "join %s: %s\n", procs, strerror(errno));
		return -1;
	}
	return 0;
}

/* Generate page-cache traffic: write a file, drop it, read it back twice. */
static void churn_page_cache(void)
{
	char *buf = malloc(1 << 20);
	int fd, i;

	if (!buf)
		return;
	memset(buf, 0xa5, 1 << 20);

	fd = open(SCRATCH, O_RDWR | O_CREAT | O_TRUNC, 0600);
	if (fd < 0) {
		fprintf(stderr, "open %s: %s\n", SCRATCH, strerror(errno));
		free(buf);
		return;
	}
	for (i = 0; i < SCRATCH_MB; i++)
		if (write(fd, buf, 1 << 20) < 0)
			break;
	fsync(fd);

	/* Evict it, then fault it all back in — that is folio_added. */
	posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED);
	lseek(fd, 0, SEEK_SET);
	while (read(fd, buf, 1 << 20) > 0)
		;
	/* Read again while resident — that is folio_accessed. */
	lseek(fd, 0, SEEK_SET);
	while (read(fd, buf, 1 << 20) > 0)
		;

	close(fd);
	unlink(SCRATCH);
	free(buf);
}

int main(void)
{
	char cgpath[512], enabled_before[512] = "";
	struct bpf_object *obj = NULL;
	struct bpf_map *ops_map, *bss;
	struct bpf_link *link = NULL;
	__u64 counters[C_NR] = { 0 };
	int rc = 1, i, bss_fd, key = 0;
	FILE *f;

	/* Piped output block-buffers stdout, so stderr jumps ahead of it and
	 * the log reads out of order. Line-buffer both. */
	setvbuf(stdout, NULL, _IOLBF, 0);
	setvbuf(stderr, NULL, _IOLBF, 0);

	if (geteuid() != 0) {
		fprintf(stderr, "must run as root\n");
		return 77;
	}
	if (access(PROC_ENABLE, F_OK) < 0) {
		fprintf(stderr, "%s missing — 0023 is not in this kernel\n",
			PROC_ENABLE);
		return 77;
	}

	/* Remember what was enabled so we can leave the machine as found. */
	f = fopen(PROC_ENABLE, "r");
	if (f) {
		if (!fgets(enabled_before, sizeof(enabled_before), f))
			enabled_before[0] = '\0';
		fclose(f);
	}

	obj = bpf_object__open_file("cache_ext_probe.bpf.o", NULL);
	if (!obj) {
		fprintf(stderr, "open bpf object: %s\n", strerror(errno));
		return 1;
	}
	if (bpf_object__load(obj)) {
		fprintf(stderr, "load bpf object: %s\n", strerror(errno));
		fprintf(stderr, "(verifier log above, if any)\n");
		/*
		 * The one failure worth naming, because its errno is a bare
		 * EINVAL and the verifier line scrolls past in a wall of log.
		 * probe_folio_evicted() calls bpf_cache_ext_list_del() behind
		 * a branch that never runs, purely so this load has to resolve
		 * a cache_ext kfunc. If register_btf_kfunc_id_set() rejected
		 * the set at boot, the kfunc is absent from the verifier's
		 * table and every real eviction policy is dead too.
		 */
		if (errno == EINVAL)
			fprintf(stderr,
				"if the verifier said \"calling kernel function "
				"bpf_cache_ext_list_del is not allowed\", the "
				"kfunc set failed to register at boot:\n"
				"  journalctl -b 0 -k | grep cache_ext\n"
				"expect the BTF ID list, not \"failed to "
				"register kfunc sets\"\n");
		goto out;
	}
	printf("bpf object loaded: struct_ops accepted by the verifier, "
	       "cache_ext kfunc resolved\n");

	/*
	 * Order matters, and getting it wrong looks like a broken patch.
	 *
	 * bpf_page_cache_ext_reg() calls page_cache_ext_get_enabled_memcg()
	 * and returns -EINVAL when no cgroup has been enabled yet. So the
	 * cgroup must exist and be published to /proc BEFORE the struct_ops
	 * is attached. Attaching first fails with a bare "Invalid argument"
	 * that reads exactly like 0023 not working.
	 */
	if (setup_cgroup(cgpath, sizeof(cgpath)))
		goto out;
	printf("scratch cgroup: %s\n", cgpath);

	if (write_file(PROC_ENABLE, cgpath + strlen(CG_ROOT)) < 0) {
		fprintf(stderr, "enable cgroup via %s: %s\n", PROC_ENABLE,
			strerror(errno));
		goto out;
	}
	printf("enabled on %s via %s\n", cgpath + strlen(CG_ROOT), PROC_ENABLE);

	ops_map = bpf_object__find_map_by_name(obj, "probe_ops");
	if (!ops_map) {
		fprintf(stderr, "probe_ops map not found\n");
		goto out;
	}
	link = bpf_map__attach_struct_ops(ops_map);
	if (!link) {
		fprintf(stderr, "attach struct_ops: %s\n", strerror(errno));
		fprintf(stderr, "check `dmesg | tail` for a page_cache_ext line\n");
		goto out;
	}
	printf("struct_ops attached: page_cache_ext_ops is live\n");

	printf("churning %d MiB of page cache...\n", SCRATCH_MB);
	churn_page_cache();

	bss = bpf_object__find_map_by_name(obj, ".bss");
	if (!bss) {
		fprintf(stderr, ".bss map not found\n");
		goto out;
	}
	bss_fd = bpf_map__fd(bss);
	if (bpf_map_lookup_elem(bss_fd, &key, counters)) {
		fprintf(stderr, "read counters: %s\n", strerror(errno));
		goto out;
	}

	printf("\ncallback counters after churn:\n");
	for (i = 0; i < C_NR; i++)
		printf("  %-32s %llu\n", cnames[i],
		       (unsigned long long)counters[i]);

	/*
	 * The verdict. folio_added is the load-bearing one: it fires from
	 * mm/filemap.c on every folio entering the cache, so a zero there
	 * after half a gigabyte of cold reads means the call site did not
	 * survive the forward-port, whatever BTF says about the type.
	 */
	printf("\n");
	if (counters[C_ADDED] == 0 && counters[C_ACCESSED] == 0) {
		printf("VERDICT: FAIL — struct_ops attached but no callback "
		       "ever fired.\n");
		printf("         0023 registers and does nothing.\n");
		rc = 1;
	} else {
		printf("VERDICT: PASS — callbacks fire from the real "
		       "page-cache path.\n");
		rc = 0;
	}

out:
	/* Put the machine back exactly as we found it. */
	write_file(PROC_ENABLE, enabled_before[0] ? enabled_before : "/");
	if (link)
		bpf_link__destroy(link);
	if (obj)
		bpf_object__close(obj);
	/* Leave our own cgroup before removing it. */
	write_file(CG_ROOT "/cgroup.procs", "0");
	snprintf(cgpath, sizeof(cgpath), "%s/%s", CG_ROOT, CG_NAME);
	rmdir(cgpath);
	return rc;
}
