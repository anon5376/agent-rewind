# Manual workflow demonstration

About 90 seconds. Both continuation files in this recording are written by hand.
Use a fresh directory and show the terminal output without editing test results.

```bash
python3 examples/demo.py --output .demo/recording --select b
python3 -m agent_rewind compare .demo/recording/state a b
cat .demo/recording/selected.patch
cat .demo/recording/selected.patch.json
```

Say:

“Agent Rewind saves project files and recorded task context so I can compare
alternative continuations before choosing a patch. This demonstration uses two
manual variants from the same checkpoint. Manual A takes a shortcut and passes
two of four whitespace tests. Manual B fixes the cases and passes all four.
The comparison shows each patch and the actual test results. I explicitly choose
B and export its patch. The original project remains unchanged.”

Then show the `mode` and `model.status` fields and say:

“This recording uses manual variants. Separate fake HTTP tests check the local
adapter, and a user-operated Anthropic trial is recorded in the README.
We have not demonstrated an open-weight model run yet.”

Keep both failed and passed test logs available. The exported receipt names B,
records source status, and links its test evidence. For a later model recording,
use `demo.py --endpoint ... --model ...`, preserve the actual outcomes, and
include the model card and license reference. Model variants may fail the tests.
