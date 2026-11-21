# Blast Radius: test selection for sqlglot

Builders Buildathon, RWX track. For each patch to sqlglot (pinned at `eb11aec`), pick the tests that can fail and pack them onto up to 8 workers.

## How it works

1. **Per-test map (built offline from the pinned commit, `maps/map.json.gz`).** Every test runs in its own forked process with `sys.monitoring` line tracing. For each test we record the lines it runs (split into module/class-body lines and function-body lines), the files it opens (fixtures), and the sqlglot classes it calls methods on at run time. Process pools are run inline so their work is attributed to the owning test. Built by `tools/mapper.py` and `tools/compile_map.py`.
2. **Per patch:**
   - Comment and whitespace edits (identical AST) select nothing.
   - Edits inside a function or lambda body select the tests that ran the changed statement. Anything less certain falls back to the whole function.
   - Class-level and module-level edits import the patched tree in a child process and diff a fingerprint of every module global and class attribute against the baseline (`selector/fp.py`). That catches dict spreads, metaclass-derived tables and closures built at import. Tests that use a changed class, or that run a line naming it, are selected.
   - Fixture and data files select the tests that opened them.
   - New, deleted or renamed files, parse errors, and any internal error select the full suite.
3. **Packing.** Try every worker count from 1 to 8 with longest-first plus local moves and swaps, and keep the cheapest (busiest worker + 2 s per worker).

## Tools

- `tools/mapper.py`, `tools/compile_map.py`, `tools/trace_import.py`: build the map.
- `tools/label.py`: ground truth. Applies a patch and runs the real suite (reproduces the sample answers).
- `tools/mutants.py`: synthetic patches used only for evaluation. No patch-to-test table is shipped.
- `tools/evaluate.py`: exact scorer, oracle comparison, misses, CPU.

## Open source used

Python standard library only at run time. Offline: pytest, pytest-xdist (from the organisers' `requirements.lock`) for labeling. sqlglot (MIT) is the system under test.

MIT licensed.
