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
| coccinelle | kernel's own `scripts/coccinelle/` semantic patterns | ran, `coccinelle 1.3` from Ubuntu `universe` |

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

### fork.c , resolved. Not BORE.

```
kernel/fork.c:2500  dereference of noderef expression
kernel/fork.c:2504  incorrect type in argument 2 (different address spaces)
```

The first pass read `git blame` as attributing this region to BORE and left it
open, on the grounds that a patch swapping `list_add_tail` for
`list_add_tail_rcu` is exactly where an RCU warning deserves an answer.

Blamed line by line, it does not hold up. 2500 is baseline
(`p->real_parent->signal->is_child_subreaper`); only 2504 is BORE's. And
`real_parent` is declared `struct task_struct __rcu *real_parent`
(`include/linux/sched.h:1111`), so *any* dereference of it without
`rcu_dereference()` trips the address-space check regardless of what is done
with the result.

The decisive number: `fork.c` carries **18** warnings of this class, and
exactly **one** of them sits on a BORE line. The other 17 are untouched
upstream code, including 2499 and 2500 immediately above. The `#else` branch
passes the identical `&p->real_parent->children` as argument 2, so the warning
tracks the annotation on `real_parent`, not the macro BORE substituted.

False positive with respect to BORE. The code runs under `tasklist_lock`,
which is the correct exclusion for modifying the children list; sparse has no
way to know that. **Closed.**

## Coccinelle

73 semantic patches over the 19 files the series touches.

**Scoped to the lines the series changes: zero findings.** Unscoped over the
whole of those files: 8, every one of them blamed to `base-7.1.9`.

```
fs/dcache.c:395,434,434,2932   atomic_dec_and_test variation before object free
kernel/fork.c:757,1225         atomic_dec_and_test variation before object free
kernel/exit.c:534              invalid reference to the index variable of the iterator
drivers/nvme/host/core.c:1398  Consider using %pe to print PTR_ERR()
```

Nothing in `drivers/dma-buf/`. 0019, 0020 and 0022 are clean under all three
tools now.

Coccinelle did not speak to the `fork.c` question above , its `fork.c`
findings are at 757 and 1225, a different check entirely. That question was
settled by blame and by the `__rcu` declaration, not by this run.

### Two bugs in the checker, found by running it

Both would have reported a clean tree.

**`run_cocci` swept directories, not files.** `make coccicheck` only accepts a
directory (`M=`), and the parents of these 19 files include `fs/` and
`kernel/`, which it walks recursively. A real run was still inside its first
of 11 directories after several minutes, on the first of 73 scripts. That is
not the scoped check this module's docstring promises. `coccicheck`'s own
header comment documents `M=./drivers/mfd/arizona-irq.c`, and that no longer
works , modern Kbuild answers `Not a directory`. `spatch` is now invoked
directly, once per (script, file) pair: 0.3 s a pair, so 73x19 is minutes.

**`_WARN` matched nothing coccinelle emits.** It was written for sparse and
smatch (`file:line:col: warning: msg`). Coccinelle uses an uppercase kind, a
column *range*, and frequently no colon after the kind:

```
drivers/dma-buf/udmabuf.c:557:7-8: WARNING opportunity for min()
```

So the tool ran and reported zero, which is indistinguishable from a clean
tree and is precisely the failure this document opened by warning about.

Both were caught by the positive control rather than by reading the code, and
the control had to be run twice , the first fix (the parser) still reported
zero, because the second bug (relative paths resolving against `cwd=tree`, so
`build/linux-7.1.9/build/linux-7.1.9/...`) was still there. A single clean
result proves nothing on its own.

## Next

Nothing outstanding on the analysers. All three run, all three are
positive-controlled, and our own patches are clean under each.

Worth doing when the series next changes: re-run both commands below and
diff against this document. A new finding in `drivers/dma-buf/` is ours to
answer for; a new one in baseline code is not.

```bash
python -m patches.staticcheck --against build/linux-7.1.9 \
                              --series patches/kernel-org-7.1/series
python -m patches.staticcheck --against build/linux-7.1.9 \
                              --series patches/kernel-org-7.1/series --all-lines
```
