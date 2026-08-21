# Recurrent-state reuse: what to build, with the numbers it rests on

Date 2026-08-21. Every figure below is measured on this box or derived from the
served model's own GGUF metadata, not from a paper's setup.

## Measured ground truth

A live slot save at 12,762 tokens:

```
{"id_slot":0,"n_saved":12762,"n_written":392430240,"timings":{"save_ms":94.788}}
```

Predicted from `qwen35` metadata (`key_length`/`value_length` 256,
`head_count_kv` 4, `block_count` 65, `full_attention_interval` 4,
`ssm.inner_size` 6144, `ssm.state_size` 128, `ssm.conv_kernel` 4), with
`--cache-type-k/v q4_0`:

| Component | Size at 12,762 tok | Scaling |
|---|---|---|
| KV, 16 attention layers | 235.2 MB | 18.0 KB/token |
| SSM state, 49 recurrent layers | 154.1 MB | **fixed** |
| conv state | 3.6 MB | **fixed** |
| **total predicted** | **393.0 MB** | |
| **measured** | **392.4 MB** | 0.15% error |

**The saved slot contains the recurrent state.** That was the open question and
it is now closed. 158 MB of it, constant regardless of depth.

At the full 24,576-token window a slot is ~600 MB.

## What is already working, contrary to my earlier reading

`/slots` during a live turn:

```
n_prompt_tokens 12665 · n_prompt_tokens_processed 251 · n_prompt_tokens_cache 12285
```

Same-slot prefix reuse is running at **97%**. Prefill is *not* being re-run
every turn. The serve banner said as much and it is correct.

`prompt_cache_hit_pct = 0` is not a contradiction: that metric tracks the
`--cache-reuse` / `--cache-ram` host-memory path, which the engine refuses for
this context. Two different caches. Both statements are true.

So the cost is not "every turn". It is the two cases the serve banner already
names: **the first turn after a restart, and any turn whose prompt changed in
the middle.** Those are P2 and P1 below.

## P1. Compaction throws the prefix away on restored sessions

`workflow/intelligence.py:619`

```python
head = session.messages[:_HEAD_KEEP]
for m in head:
    if isinstance(m.get("content"), str) and m["content"].startswith(_MEMORY_MARKER):
        head = []                     # degenerate: head IS a memory block
        break
```

A memory block sitting at the front **is** a stable prefix: byte-identical from
turn to turn, which is exactly what should stay pinned. Zeroing the head pushes
it into `middle`, where it is absorbed into `prior_memory` and re-emitted inside
the new summary at a different position. Every byte after position zero changes.

This fires on the first compaction of any *restored* session, and this CLI
restores sessions by default ("Restored last session [12 messages]"). That is
the measured `agent_compaction_prefix_kept_pct = 50`.

Fix: stop zeroing the head. The prefix then grows monotonically, never rewrites.
A second memory block simply lands after the first; the next compaction absorbs
the inner one from the middle and the pinned head is still untouched.

**Honest limit.** This restores KV reuse for the 16 attention layers. It does
**not** save the 49 recurrent layers from replaying, because their state was
overwritten in place and exists only at the tip , Marconi's core finding, and
why `recurrent_replay_ms` is called the largest recoverable per-turn cost. The
real lever for recurrent replay at compaction is P2's machinery, not head
placement.

## P2. Slot persistence across restarts , BUILT, TESTED, FALSIFIED

Implemented `checkpoint()` / `restore_checkpoint()` in `gb_synapse.py`: save
every idle slot without stopping the engine, validate model/quant/ctx/kv_type/
engine before restoring, refuse a torn dump from a busy slot.

It works mechanically and it does not deliver the win. Measured end to end:

| | time | cached |
|---|---|---|
| cold prefill, 11,767 tokens | 62.9 s | 0 |
| warm, same live engine | **1.65 s** | 11,763 |
| checkpoint save, 11,770 tok / 356 MB | 94 ms | |
| restore | 60 ms | |
| exact replay after restart + restore | **62.5 s** | **0** |

The engine picked the right slot and reported `sim_best = 1.000`,
`f_keep = 1.000` against the restored tokens, then re-prefilled all of them.
A continuation that genuinely extended the restored sequence behaved the same.

**Restoring the cells does not make llama.cpp reuse them for this hybrid
model.** Whatever bookkeeping partial reuse depends on is not reconstructed by
`llama_state_seq_set_data` here.

### This also invalidates an existing claim

`resume()`'s docstring says *"the restore itself measured 537 ms for 54,377
tokens"*. That is the byte restore, and nothing ever checked whether a request
after it actually skipped prefill. On this model it does not. Pause/resume
returns the session, not the prefill. Both docstrings now say so.

### What the same experiment proved instead

Same-slot reuse inside one live engine is excellent: **62.9 s to 1.65 s,
cached=11,763**. The lever is keeping the engine alive and the prefix intact,
which is P1, not persisting state across restarts.

### Two design bugs the test caught before shipping

- The first fingerprint included `tensor_split`, which is recomputed from live
  free VRAM each serve (10352 one start, 10267 the next). Every checkpoint was
  unrestorable for a difference that cannot affect correctness. Narrowed to the
  fields that change byte layout.
- My first acceptance test replayed a prompt *shorter* than the restored state,
  which diverges rather than extends. It looked like a code failure and was a
  test failure. Re-run in the continuation shape, then the exact shape.

## P3. Not doing

Sparse checkpointing (`arXiv 2605.05219`). Its own text rules it out for
chat-shaped workloads: *"the optimal strategy is obviously to store only the
last state, so our method does not bring additional benefits"*. Revisit only if
the RAG path shows long non-identical shared prefixes, which nothing measures
yet.

No new kernel patches. During prefill the PCIe link runs 18-29 GB/s with SMs at
100%, memory-controller util 1-5%, 47 W of 250 W: stalled on host reads at line
rate. CPU is 4% user / 6% sys / 90% idle.
