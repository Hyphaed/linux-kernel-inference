// SPDX-License-Identifier: GPL-2.0
/*
 * dmabuf_ioctl_probe — exercise the 0019 priority and 0020 compression
 * ioctls against a real dma-buf on the running kernel.
 *
 * Exports a udmabuf over a sealed memfd (that is a genuine dma-buf fd from
 * an in-tree exporter, not a mock) and issues every ioctl the two patches
 * add, checking the values that come back.
 *
 * Emits one "name: PASS/FAIL detail" line per check and exits non-zero if
 * any failed, so the pytest wrapper can parse it without a JSON dependency.
 *
 * Unprivileged: needs rw on /dev/udmabuf, which the udev ACL grants to the
 * seat owner.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <linux/dma-buf.h>
#include <linux/udmabuf.h>

#define BUF_SIZE (2u * 1024 * 1024)	/* 2 MiB, THP-sized */

static int failures;

static void report(const char *name, int ok, const char *fmt, ...)
{
	va_list ap;

	printf("%-46s %s", name, ok ? "PASS" : "FAIL");
	if (fmt && *fmt) {
		printf("  ");
		va_start(ap, fmt);
		vprintf(fmt, ap);
		va_end(ap);
	}
	putchar('\n');
	if (!ok)
		failures++;
}

/* Export a real dma-buf: sealed memfd -> UDMABUF_CREATE. */
static int make_dmabuf(void)
{
	struct udmabuf_create create = { 0 };
	int memfd, devfd, dbuf;

	memfd = memfd_create("dmabuf-probe", MFD_ALLOW_SEALING | MFD_CLOEXEC);
	if (memfd < 0) {
		perror("memfd_create");
		return -1;
	}
	if (ftruncate(memfd, BUF_SIZE) < 0) {
		perror("ftruncate");
		close(memfd);
		return -1;
	}
	/* udmabuf refuses a memfd that can still shrink. */
	if (fcntl(memfd, F_ADD_SEALS, F_SEAL_SHRINK) < 0) {
		perror("F_ADD_SEALS");
		close(memfd);
		return -1;
	}

	devfd = open("/dev/udmabuf", O_RDWR | O_CLOEXEC);
	if (devfd < 0) {
		fprintf(stderr, "open /dev/udmabuf: %s\n", strerror(errno));
		close(memfd);
		return -1;
	}

	create.memfd = memfd;
	create.flags = UDMABUF_FLAGS_CLOEXEC;
	create.offset = 0;
	create.size = BUF_SIZE;

	dbuf = ioctl(devfd, UDMABUF_CREATE, &create);
	if (dbuf < 0)
		fprintf(stderr, "UDMABUF_CREATE: %s\n", strerror(errno));

	close(devfd);
	close(memfd);
	return dbuf;
}

/* Read the "priority:" line the 0019 fdinfo hunk adds. */
static long fdinfo_priority(int fd)
{
	char path[64], line[256];
	long val = -1;
	FILE *f;

	snprintf(path, sizeof(path), "/proc/self/fdinfo/%d", fd);
	f = fopen(path, "r");
	if (!f)
		return -1;
	while (fgets(line, sizeof(line), f)) {
		if (!strncmp(line, "priority:", 9)) {
			val = strtol(line + 9, NULL, 10);
			break;
		}
	}
	fclose(f);
	return val;
}

static void test_priority(int dbuf)
{
	struct dma_buf_priority p;
	static const unsigned int vals[] = {
		DMA_BUF_PRIORITY_MIN, 1, 64, DMA_BUF_PRIORITY_DEFAULT,
		200, DMA_BUF_PRIORITY_MAX,
	};
	size_t i;
	int rc, err;

	/* A freshly exported buffer must read back the documented default. */
	memset(&p, 0xff, sizeof(p));
	if (ioctl(dbuf, DMA_BUF_IOCTL_GET_PRIORITY, &p) < 0) {
		report("0019 GET_PRIORITY on fresh buffer", 0,
		       "ioctl: %s", strerror(errno));
	} else {
		report("0019 default is DMA_BUF_PRIORITY_DEFAULT",
		       p.priority == DMA_BUF_PRIORITY_DEFAULT,
		       "got %u, want %u", p.priority, DMA_BUF_PRIORITY_DEFAULT);
		report("0019 GET zeroes pad", p.pad == 0, "pad=%u", p.pad);
	}

	/* SET/GET round-trip across the whole documented range. */
	for (i = 0; i < sizeof(vals) / sizeof(vals[0]); i++) {
		char name[64];

		memset(&p, 0, sizeof(p));
		p.priority = vals[i];
		if (ioctl(dbuf, DMA_BUF_IOCTL_SET_PRIORITY, &p) < 0) {
			snprintf(name, sizeof(name),
				 "0019 SET_PRIORITY %u", vals[i]);
			report(name, 0, "ioctl: %s", strerror(errno));
			continue;
		}
		memset(&p, 0, sizeof(p));
		if (ioctl(dbuf, DMA_BUF_IOCTL_GET_PRIORITY, &p) < 0) {
			snprintf(name, sizeof(name),
				 "0019 GET_PRIORITY %u", vals[i]);
			report(name, 0, "ioctl: %s", strerror(errno));
			continue;
		}
		snprintf(name, sizeof(name), "0019 round-trip priority=%u",
			 vals[i]);
		report(name, p.priority == vals[i], "got %u", p.priority);

		/* fdinfo must track what SET stored. */
		snprintf(name, sizeof(name), "0019 fdinfo priority=%u",
			 vals[i]);
		report(name, fdinfo_priority(dbuf) == (long)vals[i],
		       "fdinfo says %ld", fdinfo_priority(dbuf));
	}

	/* Above MAX must be rejected, not silently clamped, on the ioctl path. */
	memset(&p, 0, sizeof(p));
	p.priority = DMA_BUF_PRIORITY_MAX + 1;
	errno = 0;
	rc = ioctl(dbuf, DMA_BUF_IOCTL_SET_PRIORITY, &p);
	err = errno;
	report("0019 SET_PRIORITY 256 rejected EINVAL",
	       rc < 0 && err == EINVAL, "rc=%d errno=%d (%s)", rc, err,
	       strerror(err));

	/* pad is documented "must be zero" — SET enforces it. */
	memset(&p, 0, sizeof(p));
	p.priority = 100;
	p.pad = 0xdeadbeef;
	errno = 0;
	rc = ioctl(dbuf, DMA_BUF_IOCTL_SET_PRIORITY, &p);
	err = errno;
	report("0019 SET_PRIORITY nonzero pad rejected",
	       rc < 0 && err == EINVAL, "rc=%d errno=%d (%s)", rc, err,
	       strerror(err));

	/*
	 * Known-open review finding, pinned deliberately.
	 *
	 * GET is _IOR, so the kernel never copies the struct IN and cannot
	 * see a non-zero pad. The "must be zero" promise is therefore
	 * enforced on SET and not on GET. This asserts today's real
	 * behaviour so that the planned v2 (_IOWR + reject non-zero pad)
	 * makes this test fail loudly rather than slipping through.
	 */
	memset(&p, 0, sizeof(p));
	p.pad = 0xdeadbeef;
	errno = 0;
	rc = ioctl(dbuf, DMA_BUF_IOCTL_GET_PRIORITY, &p);
	report("0019 GET ignores pad (v2 will flip this)", rc == 0,
	       "GET is _IOR so pad is never copied in; rc=%d", rc);

	/* Leave the buffer at the default so later checks start clean. */
	memset(&p, 0, sizeof(p));
	p.priority = DMA_BUF_PRIORITY_DEFAULT;
	ioctl(dbuf, DMA_BUF_IOCTL_SET_PRIORITY, &p);
}

static void test_compression(int dbuf)
{
	struct dma_buf_compression c;

	/* Untouched buffer must report "not compressed". */
	memset(&c, 0xff, sizeof(c));
	if (ioctl(dbuf, DMA_BUF_IOCTL_GET_COMPRESSION, &c) < 0) {
		report("0020 GET_COMPRESSION on fresh buffer", 0,
		       "ioctl: %s", strerror(errno));
		return;
	}
	report("0020 default codec is DMA_BUF_CODEC_NONE",
	       c.codec == DMA_BUF_CODEC_NONE, "codec=%u", c.codec);
	report("0020 default uncompressed_size is 0",
	       c.uncompressed_size == 0, "size=%llu",
	       (unsigned long long)c.uncompressed_size);

	/* Full descriptor round-trip, all three fields. */
	memset(&c, 0, sizeof(c));
	c.codec = 0x4c5a3400;			/* opaque, kernel must not read it */
	c.block_size = 65536;
	c.uncompressed_size = BUF_SIZE * 3ull;
	if (ioctl(dbuf, DMA_BUF_IOCTL_SET_COMPRESSION, &c) < 0) {
		report("0020 SET_COMPRESSION", 0, "ioctl: %s", strerror(errno));
		return;
	}
	memset(&c, 0, sizeof(c));
	if (ioctl(dbuf, DMA_BUF_IOCTL_GET_COMPRESSION, &c) < 0) {
		report("0020 GET_COMPRESSION after SET", 0,
		       "ioctl: %s", strerror(errno));
		return;
	}
	report("0020 round-trip codec", c.codec == 0x4c5a3400,
	       "got 0x%x", c.codec);
	report("0020 round-trip block_size", c.block_size == 65536,
	       "got %u", c.block_size);
	report("0020 round-trip uncompressed_size",
	       c.uncompressed_size == BUF_SIZE * 3ull, "got %llu",
	       (unsigned long long)c.uncompressed_size);

	/* Resetting to CODEC_NONE must be expressible. */
	memset(&c, 0, sizeof(c));
	c.codec = DMA_BUF_CODEC_NONE;
	if (ioctl(dbuf, DMA_BUF_IOCTL_SET_COMPRESSION, &c) == 0) {
		memset(&c, 0xff, sizeof(c));
		ioctl(dbuf, DMA_BUF_IOCTL_GET_COMPRESSION, &c);
		report("0020 reset to CODEC_NONE",
		       c.codec == DMA_BUF_CODEC_NONE, "codec=%u", c.codec);
	} else {
		report("0020 reset to CODEC_NONE", 0, "SET: %s",
		       strerror(errno));
	}
}

/*
 * 0019 and 0020 must not have trodden on each other's ioctl numbers, and
 * neither may shadow an ioctl that existed before them.
 */
static void test_no_collision(int dbuf)
{
	struct dma_buf_priority p = { .priority = 77 };
	struct dma_buf_compression c = { .codec = 0x5a5a5a5a };
	int rc, err;

	if (ioctl(dbuf, DMA_BUF_IOCTL_SET_PRIORITY, &p) < 0 ||
	    ioctl(dbuf, DMA_BUF_IOCTL_SET_COMPRESSION, &c) < 0) {
		report("0019/0020 independent state", 0, "SET failed");
		return;
	}
	memset(&p, 0, sizeof(p));
	memset(&c, 0, sizeof(c));
	ioctl(dbuf, DMA_BUF_IOCTL_GET_PRIORITY, &p);
	ioctl(dbuf, DMA_BUF_IOCTL_GET_COMPRESSION, &c);
	report("0019/0020 independent state",
	       p.priority == 77 && c.codec == 0x5a5a5a5a,
	       "priority=%u codec=0x%x", p.priority, c.codec);

	/* DMA_BUF_SET_NAME predates both; it must still work. */
	errno = 0;
	rc = ioctl(dbuf, DMA_BUF_SET_NAME_B, "probe");
	err = errno;
	report("pre-existing DMA_BUF_SET_NAME still works", rc == 0,
	       "rc=%d %s", rc, rc == 0 ? "" : strerror(err));

	/* An unclaimed number must still be rejected. */
	errno = 0;
	rc = ioctl(dbuf, _IOW(DMA_BUF_BASE, 9, __u64), &p);
	err = errno;
	report("unclaimed ioctl 9 still rejected",
	       rc < 0 && (err == ENOTTY || err == EINVAL),
	       "rc=%d errno=%d (%s)", rc, err, strerror(err));
}

int main(void)
{
	int dbuf = make_dmabuf();

	if (dbuf < 0) {
		fprintf(stderr, "could not export a dma-buf; cannot test\n");
		return 77;	/* distinct from a real failure */
	}
	printf("exported a real dma-buf via udmabuf, fd=%d, size=%u\n",
	       dbuf, BUF_SIZE);

	test_priority(dbuf);
	test_compression(dbuf);
	test_no_collision(dbuf);

	close(dbuf);
	printf("\n%d check(s) failed\n", failures);
	return failures ? 1 : 0;
}
