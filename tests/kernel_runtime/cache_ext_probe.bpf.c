// SPDX-License-Identifier: GPL-2.0
/*
 * cache_ext_probe.bpf.c — does patch 0023's page-cache-eviction hook
 * actually fire?
 *
 * 0023 is a forward-port of the cache_ext research code (Zussman et al.,
 * SOSP '25) from a 6.6.8 fork to 7.1.10, across a real bpf_struct_ops API
 * break. Verifying that the struct_ops type registers proves the port
 * compiled and registered; it does NOT prove the call sites in mm/vmscan.c
 * and mm/filemap.c survived the rebase.
 *
 * It also has to prove the kfunc set registered. Checking that the
 * struct_ops type is in BTF does not: on 7.1.10 the type registered while
 * register_btf_kfunc_id_set() rejected both kfunc sets with -EINVAL, because
 * the port carried 6.6.8's BTF_SET8_START instead of BTF_KFUNCS_START. The
 * struct_ops attached fine and every cache_ext kfunc was missing from the
 * verifier's table. A probe that calls no kfunc cannot see that, and this one
 * could not, so it did not. It calls one now -- see probe_folio_evicted().
 *
 * So this policy does the least it possibly can: it counts. Every callback
 * bumps a counter and returns. evict_folios() deliberately sets
 * nr_folios_to_evict = 0, which tells the kernel "I selected nothing" and
 * leaves the built-in LRU to do its normal job. No folio is ever moved,
 * isolated or freed by this program, so it cannot change what the machine
 * reclaims — it can only observe that it was asked.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

char LICENSE[] SEC("license") = "GPL";

/*
 * One counter per callback. A plain global array in .bss — struct_ops
 * callbacks can race across CPUs, and these are diagnostics rather than
 * accounting, so an occasional lost increment is acceptable and cheaper
 * than a per-CPU map lookup on a page-cache hot path.
 */
#define C_INIT		0
#define C_ADDED		1
#define C_ACCESSED	2
#define C_EVICTED	3
#define C_EVICT_CALL	4
#define C_EVICT_REQ	5	/* sum of request_nr_folios_to_evict */
#define C_NR		6

__u64 counters[C_NR] = {};

/*
 * One cache_ext kfunc, declared so the verifier has to resolve it at load
 * time. If the kfunc set failed to register, libbpf's load fails here with
 * "calling kernel function bpf_cache_ext_list_del is not allowed" rather than
 * succeeding and leaving the breakage for a real policy to discover.
 */
extern int bpf_cache_ext_list_del(struct folio *folio) __ksym;

/*
 * Plain .bss, never written by userspace, so it is always false at run time
 * and the guarded call never executes. It is deliberately NOT
 * `const volatile`: libbpf dead-code-eliminates branches on those once the
 * value is known, which would delete the call before the verifier ever saw
 * it and put us straight back to not testing the thing that broke.
 */
bool probe_call_kfunc = false;

SEC("struct_ops/init")
s32 BPF_PROG(probe_init, struct mem_cgroup *memcg)
{
	__sync_fetch_and_add(&counters[C_INIT], 1);
	return 0;
}

SEC("struct_ops/folio_added")
void BPF_PROG(probe_folio_added, struct folio *folio)
{
	__sync_fetch_and_add(&counters[C_ADDED], 1);
}

SEC("struct_ops/folio_accessed")
void BPF_PROG(probe_folio_accessed, struct folio *folio)
{
	__sync_fetch_and_add(&counters[C_ACCESSED], 1);
}

SEC("struct_ops/folio_evicted")
void BPF_PROG(probe_folio_evicted, struct folio *folio)
{
	__sync_fetch_and_add(&counters[C_EVICTED], 1);

	/*
	 * Verified, never executed. This probe never adds a folio to a
	 * cache_ext list, so deleting one would be meaningless at best; the
	 * branch exists so the load fails loudly when the kfunc is absent.
	 */
	if (probe_call_kfunc)
		bpf_cache_ext_list_del(folio);
}

/* Note: the parameter is NOT called ctx — BPF_PROG() uses that name for its
 * own context pointer and the two collide. */
SEC("struct_ops/evict_folios")
void BPF_PROG(probe_evict_folios, struct page_cache_ext_eviction_ctx *ectx,
	      struct mem_cgroup *memcg)
{
	__sync_fetch_and_add(&counters[C_EVICT_CALL], 1);
	__sync_fetch_and_add(&counters[C_EVICT_REQ],
			     ectx->request_nr_folios_to_evict);

	/*
	 * Select nothing. The kernel falls back to its own LRU walk, so
	 * enabling this policy is observationally neutral apart from the
	 * counters above.
	 */
	ectx->nr_folios_to_evict = 0;
}

SEC(".struct_ops.link")
struct page_cache_ext_ops probe_ops = {
	.init		= (void *)probe_init,
	.evict_folios	= (void *)probe_evict_folios,
	.folio_added	= (void *)probe_folio_added,
	.folio_accessed	= (void *)probe_folio_accessed,
	.folio_evicted	= (void *)probe_folio_evicted,
};
