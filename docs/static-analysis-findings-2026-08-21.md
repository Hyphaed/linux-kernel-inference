# Static analysis of the patch series, first run

Date 2026-08-21, against `build/linux-7.1.9` freshly patched with all 19.

## What was added

`patches/staticcheck.py`, run as:

```bash
python -m patches.staticcheck --against build/linux-7.1.9 \
                              --series patches/kernel-org-7.1/series
```

`checkpatch.pl` was the only patch checker this repo had, and it is a style
and hygiene tool that has never had an opinion about whether the code is
correct. These do:

| tool | catches | status here |
|---|---|---|
| sparse | type and `__user`/`__rcu` address-space errors gcc compiles silently | built from source, `~/Dev/github/sparse` |
| smatch | null derefs, unwound error paths, lock imbalance | built from source, `~/Dev/github/smatch` |
| coccinelle | kernel's own `scripts/coccinelle/` semantic patterns | **NOT RUN**, needs `sudo apt install coccinelle` (OCaml) |

Findings are scoped to the lines the series changes. Unscoped, sparse emits
40+ address-space warnings from `kernel/sched/core.c` and `fair.c` that
upstream has carried for years, and a gate reporting those is a gate nobody
reads twice. Scoped, the same run returns 8.

The "clean" results below are trustworthy because the pipeline was
positive-controlled: injecting `static int gb_sparse_probe(int __user *p)
{ return *p; }` into `udmabuf.c` produced `warning: dereference of noderef
expression` at that line, and removing it returned to clean.

## Result

**Our own patches (0019, 0020, 0022) are clean under both tools.** Every
finding is in third-party code the repo carries.

### bore.c , stale sysctl signature. Real.

```
kernel/sched/bore.c:346  incorrect type in argument 3 (different address spaces)
kernel/sched/bore.c:362  incorrect type in argument 3 (different address spaces)
kernel/sched/bore.c:378  incorrect type in initializer (incompatible argument 3 …)
kernel/sched/bore.c:387  incorrect type in initializer (incompatible argument 3 …)
```

```c
int sched_bore_update_handler(const struct ctl_table *table,
		int write, void __user *buffer, size_t *lenp, loff_t *ppos) {
	int ret = proc_dou8vec_minmax(table, write, buffer, lenp, ppos);
```

`proc_handler` stopped taking `__user` buffers in 5.8, commit `32927393dc1c`
("sysctl: pass kernel pointers to ->proc_handler"). BORE still annotates them
`__user` and hands them to a function that expects a kernel pointer.

At runtime on this build it works: the pointer value is right, only the
annotation is wrong, and gcc discards `__user` entirely. It matters because
the code documents a contract that is false, it would draw a NAK upstream,
and under a clang-CFI build a mismatched handler prototype is not free.

Not fixed here. It is CachyOS's code and the fix belongs upstream of us;
recorded so the next forward-port does not rediscover it.

### bore.c , two globals that should be static

```
kernel/sched/bore.c:29  symbol 'sched_burst_inherit_key' was not declared. Should it be static?
kernel/sched/bore.c:30  symbol 'sched_burst_ancestor_key' was not declared. Should it be static?
```

`DEFINE_STATIC_KEY_TRUE` at file scope with no header declaration. Namespace
pollution rather than a defect.

### fork.c , needs a human look

```
kernel/fork.c:2500  dereference of noderef expression
kernel/fork.c:2504  incorrect type in argument 2 (different address spaces)
```

`git blame` attributes the region to `bore`. BORE swaps `list_add_tail` for
`list_add_tail_rcu` on the sibling list under `#ifdef CONFIG_SCHED_BORE`.
Sparse is noisy around RCU list macros, so this may be a false positive, but
a patch that changes list handling to the RCU form is exactly where that
question deserves an answer rather than an assumption. **Unresolved.**

## Next

Install coccinelle and re-run; `scripts/coccinelle/` is the layer none of the
above covers, and it is the one that finds API misuse rather than type errors.
