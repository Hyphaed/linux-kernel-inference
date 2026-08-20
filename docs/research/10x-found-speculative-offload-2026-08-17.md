# The 10x is real, and it is speculative decoding under offloading (2026-08-17)

**This document corrects the previous one.**
`10x-tok-s-kernel-path-2026-08-17.md` concluded that 10x could only come from
making weights VRAM-resident. That conclusion rested on an assumption I did not
examine: **that one full weight-read produces one token.** Speculative decoding
breaks exactly that assumption, and it does so *better* under offloading, not
worse. Two independent papers measure ~9-14x on consumer GPUs in our exact
configuration. Both were fetched and read (PDF → md via docling).

---

## 1. The mechanism

Under parameter offloading, decode time is dominated by moving weights across
PCIe. That cost is **per forward pass, not per token**. A forward pass that
validates a tree of K candidate tokens moves the same bytes as a pass that
produces one token — the transfer is amortised across every token accepted.

SpecExec states it directly:

> *"With parameter offloading, hundreds or thousands of tokens can be processed
> in batches within the same time as just one token, making it a natural fit for
> speculative decoding."* — `2406.02532`, abstract

So offloading, which makes ordinary decode catastrophically slow, is precisely
the regime where speculation pays the most. The worse the PCIe penalty, the
larger the speculative win.

## 2. The measurements

### `2406.02532` — SpecExec (Yandex / Together AI / CMU / Meta AI, NeurIPS-class)

Consumer GPUs, 4-bit Llama-2-70B target, **batch size 1**, RAM offloading —
i.e. our setup:

| GPU | Draft | Budget | Tokens/target pass | tok/s | Speedup |
|---|---|---|---|---|---|
| RTX 4090 | Llama 2-7B | 256 | 13.46 | 5.66 | 8.3x |
| **RTX 3090** | Llama 2-7B | 256 | **14.3** | 3.68 | **10.6x** |
| RTX 4060 | Llama 2-7B | 128 | 9.70 | 3.28 | 4.6x |

Headline: *"10-18x speedups compared to sequential inference on the same
hardware."* Note the 3090/2080Ti runs were on **PCIe Gen3** — a worse link than
our Gen4, and they still got 10.6x.

### `2509.18344` — SubSpec (NYCU / Cornell)

**Lossless and training-free.** Builds the draft model by generating low-bit
quantised *substitute layers from the offloaded portions of the target model
itself*, and shares the GPU-resident layers and KV-cache.

- **9.1x average speedup**, individual results to **13.76x**, 12.5x, 9.8x.
- Average acceptance length τ **near 30 tokens** on some tasks.
- *"76% speedup without requiring an additional draft model"* and 30–50% better
  than same-family small draft models.

## 3. Redone arithmetic for this box

From the measured baseline in the previous document — 263 ms/token, 3.8 tok/s
(10 GB resident + 6.2 GB over PCIe Gen4):

```
sequential      : 263 ms/token                        ->  3.8 tok/s
speculative, τ≈13 : 263 ms per pass / 13 accepted      ->  ~49 tok/s (ideal)
published end-to-end, 9.1x - 10.6x on 3.8 tok/s        ->  ~34-40 tok/s
```

**~34-40 tok/s is the 10x**, and unlike the residency route it requires no
quality loss, no smaller model, and no change to the quantisation.

The two routes **multiply** rather than compete: improving residency raises the
sequential baseline that speculation then multiplies.

## 4. Why this fits GreenBoost specifically

Three properties of our stack make SubSpec unusually well matched:

1. **No separate draft model, no extra VRAM.** We measured 1.8 GB headroom at
   rest (`vram-residency-baseline.py`). SpecExec needs a 7B draft resident;
   SubSpec does not — it derives the draft from weights we are already holding.
2. **Training-free matters here.** Our default model is a custom merge
   (`DavidAU/Qwen3.6-27B-Fable-Fusion-711-…-MTP-GGUF`). There is **no
   same-family pretrained draft model**, which rules out most speculative
   methods. SubSpec's substitute-layer construction is designed for exactly this.
3. **The draft is built from offloaded layers — which GreenBoost already
   manages.** GreenBoost's tier manager knows precisely which layers live in
   T2/T3. That is the input SubSpec needs. We are better positioned to implement
   this than a userspace framework, because we own the tier metadata.

**And the model already has native MTP heads.** Multi-token prediction heads are
a built-in draft mechanism (`2404.19737`, `2502.09419`). The MTP predictive
prefetch oracle already scaffolded in `gb_moe.py` was aimed at prefetching; the
same heads can drive *speculation*, which is worth an order of magnitude more.

## 5. What this means for the kernel

Unchanged, and it should be said plainly: **the kernel is still worth ~5.7%**,
bounded by the 14.9 ms compute half of the pass. Speculation does shift the mix
— transfers become ~13x rarer while per-pass compute grows with tree size, so
the compute half's *share* rises — but the kernel is not where the 10x lives.

The correct division of labour:

| Layer | Contribution |
|---|---|
| **Inference runtime / GreenBoost** | **9-14x** — speculative decoding over offloaded weights |
| Quantisation / residency | up to 10.8x, at a quality cost; multiplies with the above |
| dma-buf compressed transfer (patches 0019/0020) | reduces the 248 ms term directly; multiplies |
| Kernel scheduling / topology config | ≤5.7%, worth having, not a headline |

## 6. Next steps, in order

1. **Measure the real baseline.** Everything above is arithmetic on one
   `nvidia-smi` reading and published results. Get actual tok/s for the default
   model before building anything.
2. **Prototype speculation with the existing MTP heads** — cheapest path,
   because the draft mechanism already ships inside the model.
3. **Implement SubSpec-style substitute-layer drafting in GreenBoost**, using
   the tier manager's knowledge of which layers are offloaded. This is the
   defensible research contribution: SubSpec assumes a generic offloading
   engine; we have a kernel-backed tiering system that knows exact residency.
4. **Re-run the energy question.** `2508.06978` showed NVMe offload costs ~12x
   per-token energy. Speculation reduces the *number* of transfers by ~13x,
   which should substantially reduce that penalty — plausibly making T3 viable
   on the laptop after all. Worth checking rather than assuming.

## 7. Correction log

The earlier document's claim — "the 10x can only come from residency" — was
wrong. It was arithmetically sound given its assumption (one token per weight
read) and that assumption went unstated and unexamined. The lesson is the same
one recorded in the project notes for Kconfig: an unverified premise produces a
confident, well-argued, wrong conclusion. The PCIe bound was real; "therefore
only residency helps" did not follow.
