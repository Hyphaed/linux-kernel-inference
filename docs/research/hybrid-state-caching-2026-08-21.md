# The recurrent replay problem, and what to do about it

Date: 2026-08-21. Sources read in full via docling, not skimmed from abstracts.

## The finding

This box serves a **hybrid** model: 17 full-attention layers and 48 Gated
DeltaNet recurrent layers, `qwen35` arch, 65 blocks, `full_attention_interval`
4. GreenBoost's own governed metric `recurrent_replay_ms` already says what
that costs and calls it *"the largest recoverable per-turn cost here"*:

> a recurrent layer's state at position t only exists by running every token up
> to t, so the KV cache can hit 99.9% and the recurrent stack still replays the
> entire conversation. Measured 2026-08-18 over 29 warm turns: median 6,937 ms
> prefill for a median of 42 new tokens.

The literature confirms the mechanism and names the cause. Marconi
(`arXiv 2411.19379`, MLSys 2025):

> their use of in-place state updates for recurrent layers precludes rolling
> back cache entries for partial sequence overlaps, and instead **mandates only
> exact-match cache hits**

That is also why `--cache-reuse` is refused for this context. It is not a
misconfiguration to fix. A recurrent state cannot be sliced on a sequence
dimension the way a KV tensor can, because it was overwritten in place.

Measured on this box the same day, one 16k-token prompt:

```
n_tokens =  2048, t =  37.01 s / 55.34 tok/s
n_tokens = 15981, t = 306.76 s / 52.10 tok/s
```

306 seconds of prefill, degrading with depth. `prompt_cache_hit_pct = 0`.

## Why this box is close to the ideal case for the fix

Marconi's microbenchmarks improve with **longer contexts, higher ratios of SSM
layers, and larger SSM state dimensions**. This box is extreme on all three:
74% recurrent layers against the 1-attention-per-6-to-10 that the paper calls
typical, and a 24,576-token window.

The 2026 follow-up (`arXiv 2605.05219`) names the architecture outright:

> for models such as **Qwen-3.5**, the recurrent (**GatedDeltaNet**) state per
> head scales as d²_head, whereas attention KV storage grows only linearly in
> d_head per head

Marconi's headline: **34.4x higher token hit rates, 71.1% / 617 ms lower P95
TTFT.** Open source at `github.com/ruipeterpan/marconi`.

## The cheap answer for our workload, stated by the paper itself

Do **not** start with the sparse-checkpoint dynamic program. `2605.05219` is
explicit that it does not apply to us:

> For chat-like workloads, where each consecutive request within a conversation
> contains the previous one as a substring, **the optimal strategy is obviously
> to store only the last state**, so our method does not bring additional
> benefits (but also incurs no overhead).

An agentic CLI session is exactly that shape. Marconi reaches the same policy
from the other direction: for input+output sharing it *"assigns value only to
SSM states that represent the last decoded token (which conversations typically
resume from)"*.

So the whole win here is **one state, saved at the last decoded token, restored
on the next turn**. Not a cache hierarchy.

## The precondition, which we are currently violating

"Each request contains the previous one as a substring" is a property the
*agent* must maintain, and dataflux says it does not:

| Segment | Evidence | Meaning |
|---|---|---|
| `agent_compaction_broke_prefix` | `prefix_kept_pct = 50` | half of compactions rewrote the prefix |
| `prompt_cache_cold` | `prompt_cache_hit_pct = 0` | nothing is being reused |
| `ctx_estimate_undercounts` | `-8.11%` | turns end on a 400 at the window edge |

For a pure Transformer a broken prefix costs you the tokens after the break.
**For a hybrid model it costs you everything**, because exact-match is the only
hit mode. This is the difference between a 50% regression and a total one, and
it is why the agent change is not cosmetic.

## Plan

### P1. Agent: prefix-preserving compaction. Highest leverage, no kernel work.

`agent_compaction_prefix_kept_pct` must go to 100. Compaction has to be
append-only with respect to the token prefix: never rewrite or re-order what
came before, only drop or summarise at the tail and continue. Anything that
edits the middle invalidates the single stored recurrent state and forces a
full replay of the conversation.

Also fix `ctx_estimate_undercounts` while in there. It under-predicts by 8.11%
and has already produced a real 400 at 24,654 tokens against a 24,576 window.

Acceptance: `prefix_kept_pct` 100 over 20 compactions; `prompt_cache_hit_pct`
non-zero; no 400 at the window edge.

### P2. GreenBoost: actually call the slot save/restore that is already wired.

**This is not a build. It is already plumbed and has never been used once.**
Checked on the running serve (pid 180426):

| Thing | State |
|---|---|
| `llama_state_seq_get_data` / `_set_data` in `libllama.so.0` | exported |
| `--slot-save-path /var/lib/greenboost/synapse/slots` | passed on the serve line |
| `POST /slots/{id}?action=save\|restore` | implemented, `gb_synapse.py:5293` |
| `/var/lib/greenboost/synapse/slots` | **empty, 4.0K** |

So the one mechanism that can persist a Gated DeltaNet state across turns is
configured, reachable, and dormant. Everything else on that serve line is
caching the wrong thing for a hybrid model:

- `--cache-reuse 256` is passed and the engine **rejects it** at load
  (`"cache_reuse is not supported by this context"`), exactly as Marconi
  predicts for in-place recurrent updates.
- `--cache-ram 7408` gives 7.4 GB of host-memory KV cache. KV is the 17
  attention layers. It does nothing for the 48 recurrent ones.

Work: call `action=save` after the final decoded token of a turn and
`action=restore` before the next, keyed per conversation. That is Marconi's
last-decoded-token policy and `2605.05219`'s "obviously optimal" chat strategy,
and here it is one API call at each end of a turn.

**Verify, do not assume, that a saved slot carries the recurrent state.**
`llama_state_seq_*` is documented as sequence state, but whether this build
serialises the recurrent memory alongside KV is a property to measure, not to
infer: save one slot at a known depth and compare the file size against the KV
size implied by `--cache-type-k/v q4_0` at that depth. A file the size of KV
alone means the recurrent state is not in there and P2 needs engine work after
all.

Acceptance: `recurrent_replay_ms` falls on a resumed turn. Report it against
`prompt_depth_tokens`, which exists precisely because a TTFT or tok/s number
quoted without the depth it was measured at is not comparable to another.

### P2b. Speculative decoding is already on. Measure it rather than re-enabling it.

The serve line carries `draft-mtp`, `--spec-draft-n-max 4`,
`--spec-draft-p-min 0.3`, so the MTP path from
`10x-found-speculative-offload-2026-08-17.md` is live, and
`tasks_patches.md` T15 already measured 3.80-3.92 to 5.62-6.03 tok/s from it.
`draft-n-max 4` is conservative next to SpecExec's measured 13-14 accepted
tokens per pass under offloading. Sweeping it is cheap and is decode-side,
so it stacks with P1/P2 rather than competing.

### P3. Sparse checkpointing. Deferred, and gated on evidence.

The `O(NM)` DP only pays when requests share a long but **non-identical**
prefix: many questions over one document, or RAG-retrieved text. This box has
a 272,080-chunk RAG index, so the pattern may exist, but nothing measures it
yet. Instrument the overlap-depth distribution first. If it is chat-shaped, P2
already captured the whole win and this is dead work.

### P4. Kernel: nothing new here.

The kernel's share of decode is ~5.7% (`10x-tok-s-kernel-path-2026-08-17.md`)
and today's telemetry agrees from a different direction: during prefill the
PCIe link runs 18-29 GB/s with SMs at 100%, memory-controller util at 1-5% and
47 W of a 250 W budget. The GPU is stalled on host reads at line rate. No
transfer-path patch makes a saturated link faster, and CPU sits at 4% user /
6% sys / 90% idle, so the kernel is not consuming the time either.

The one kernel item that *is* real is unrelated to inference speed and already
prepared: `patches/custom/0022-udmabuf-do-not-create-malformed-scatterlists.patch`,
Jason Gunthorpe's `5bf888673e0d` backported to 7.1.9, which 7.1.y never picked
up despite the `Fixes:` tag.

## What was checked and deliberately not proposed

Re-confirmed dead by today's measurements, on top of `tasks_patches.md` T14:
a prefetch ioctl (the link has no idle window to prefetch into), PCIe MPS/MRRS
tuning (already at the device ceiling), TierBPF-style eBPF hooks (one NUMA
node, `numa_balancing=disable`), GDS/P2PDMA (enabled, and `nvidia-fs` stats are
all zero because T3 has never been allocated).

## Sources

| Paper | Read | Bearing on this box |
|---|---|---|
| `2411.19379` Marconi, MLSys 2025 | full, docling | in-place update ⇒ exact-match only; last-decoded-token policy; FLOP-aware eviction |
| `2605.05219` Sparse Prefix Caching, 2026-05 | full, docling | names GatedDeltaNet/Qwen-3.5; says chat workloads want last-state-only |
| `2607.01299` HYPIC, 2026-07 | conversion pending | position-independent caching for hybrid attention |
