# Agent Rewind

Save a coding task, try two continuations in separate file copies, compare
their diffs and tests, and explicitly export the patch you choose.

## Try the local prototype

Requires Python 3.11+ and Git. Uses only Python's standard library. Verified on
macOS; the full suite uses POSIX terminal support and loopback sockets. Run from
this repository; no package installation is needed.

```bash
python3 -m agent_rewind --help
python3 -m unittest discover -s tests -v
python3 examples/demo.py --output .demo/manual --select b
```

Use a fresh output directory. The demo saves a whitespace-parsing task and makes
two **manual fixture** continuations: a shortcut that fails two of four tests,
and a fix that passes all four. It saves the comparison, test logs, selected
patch, and evidence sidecar. The source remains unchanged. This demonstrates
the file workflow; it provides no evidence about an AI model's performance.

## Use it on a project

Write task context yourself: goal, constraints, relevant decisions, and the next
step. Checkpointing stores that text, rather than an agent's implicit memory.

```bash
python3 -m agent_rewind checkpoint ../my-project \
  --context ../task.txt --store .rewind
# Copy the returned checkpoint id:
python3 -m agent_rewind fork .rewind CHECKPOINT_ID --name a
python3 -m agent_rewind fork .rewind CHECKPOINT_ID --name b
```

Edit the returned working directories, or point your coding agent at them.
Only an explicitly supplied test command runs:

```bash
python3 -m agent_rewind test .rewind a --timeout 30 -- \
  python3 -m unittest discover -s tests -v
python3 -m agent_rewind test .rewind b --timeout 30 -- \
  python3 -m unittest discover -s tests -v
python3 -m agent_rewind compare .rewind a b
python3 -m agent_rewind export .rewind b --output ../chosen.patch
```

The test command exits with 0 for pass, 1 for failure/timeout, and 2 for CLI
errors. Other commands return JSON and exit 0 on success. Comparison requires
a shared checkpoint. It includes applicable Git patches, file changes, elapsed
test time, reported model token counts when present, and exclusions. Changed
code makes earlier test evidence **stale**; ignored test artifacts do not.
Export writes a new patch and `.patch.json` receipt. It never applies the patch
or chooses a winner. Review both files before applying to your project.
Apply from the target project's Git root. From a nested directory, Git may skip
paths outside that directory even when `git apply` returns zero. Verify the
resulting files and tests after application.

Comparison also checks the retained source files against the checkpoint.
If they changed or became unavailable, export stops by default. Create a fresh
checkpoint, or use `export --allow-source-drift` to explicitly export a patch
against the saved checkpoint. The receipt records this choice and source status.
Run names are checked for collisions without regard to letter case.

## Existing local model adapter

The `continue` command sends recorded task context and explicitly allowlisted
files to a loopback OpenAI-compatible server. It requests a JSON file proposal,
checks the paths and contents, and changes only the named files. It has no model
tools. Use an already-listed model that you have verified runs locally.

```bash
python3 -m agent_rewind models --endpoint http://127.0.0.1:1234/v1
python3 -m agent_rewind continue .rewind a \
  --endpoint http://127.0.0.1:1234/v1 --model YOUR_EXISTING_MODEL_ID \
  --files src/parser.py --instruction 'Use a readable regular expression.'
```

Use the same command for `b` with another instruction, then test and compare.
The adapter requires `/v1/models` and `/v1/chat/completions`, nonstreaming text
responses, and JSON proposal support. See [Ollama's compatibility documentation](https://docs.ollama.com/api/openai-compatibility)
for one server's wire contract. Numeric loopback HTTP with an explicit port is
required; proxies, credentials, redirects, and cloud model name suffixes are refused.
The CLI does not install, pull, or download a model.

**No open-weight model demo has been completed.** The checked local endpoint
returned an empty model list. This adapter's tests use a fake HTTP server.

For an open-weight/open-source model demo, first supply an existing endpoint
with a named available model. Verify its model card, license, and local serving
configuration. A loopback address or reported model name alone cannot prove
that the server uses local weights rather than forwarding requests elsewhere.
Then run:

```bash
python3 examples/demo.py --output .demo/local-model \
  --endpoint http://127.0.0.1:1234/v1 --model YOUR_EXISTING_MODEL_ID
# Inspect both results and explicitly select one:
python3 -m agent_rewind export .demo/local-model/state b \
  --output ../model-choice.patch
```

Keep the model card/license reference alongside the saved comparison and model
metadata. Neither model variant is guaranteed to pass the task's tests.

## Recorded Anthropic trial

On 14 October 2026, a user manually entered a key in a separate macOS Terminal
and ran the bounded Anthropic launcher on the synthetic slugify task. The two
Messages API responses reported `claude-haiku-5-5`. Both produced identical code
using a regular expression, including the variant prompted to use string
operations. Neither result is a demonstrated improvement over the other.

| Variant | Reported input tokens | Reported output tokens | Fixed local tests |
| --- | ---: | ---: | --- |
| A | 272 | 89 | 4/4 pass |
| B | 270 | 89 | 4/4 pass |

The code was inspected before execution. A was exported as a representative
of the identical outputs and applied to another copy, which passed the same four
tests. The original source and both model responses stayed unchanged.
The token-based estimate is about $0.00014 at the [documented Haiku 5.5 rates](https://platform.claude.com/docs/en/about-claude/pricing).
Account billing and promotional funding were not independently verified.

This is a real **proprietary-model API trial**, separate from the manual demo and
fake HTTP tests. It does not satisfy an open-weight/open-source model requirement.
The [manual launcher instructions](examples/ANTHROPIC-TRIAL.md) reproduce the
procedure; another model response may differ or fail. Raw run artifacts,
credentials and local logs are excluded from this repository.

## Scope and limits

Checkpoints retain UTF-8 regular files, executable bits, and recorded context.
They exclude common secret paths, recognized secret patterns, symbolic links,
binary files, service directories, and files over 1 MiB. Retained project files
are limited to 20 MiB and 10,000 files; context is limited to 64 KiB. The store and
export destination must be outside the original project. State is plaintext
under a directory restricted to the current user. Filtering is heuristic;
inspect `skipped` and protect sensitive data yourself. Export refuses unsafe
candidate files rather than turning filtered modifications into deletions.
The store must be a fresh/empty directory or an existing Agent Rewind store.

The publication check used local secret patterns because no dedicated scanner
was installed. It recognized inert test markers and code references, and found
no credential-like value requiring review in the selected source files. Pattern
checks can miss unknown formats, obfuscation and encoded or split secrets; they
are not proof that a credential has never been exposed elsewhere.

File copies separate ordinary edits. They are not a security sandbox: explicitly
run tests/agents have your OS permissions and can access networks or other files.
The CLI does not automatically run project scripts, Git hooks/configured filters,
or model-proposed commands. Test subprocesses receive a small environment without
ambient credential variables; tools needing extra environment setup may fail.
Only the first 64 KiB of each test output stream is retained; recognized secret
patterns are redacted. Review command arguments and logs before sharing them.

This prototype cannot restore hidden model state, undo external actions, or
preserve original Git history and ignored files. Single-writer use is intended.
Test results record exit codes from the command you chose, rather than proving
test quality. Generated code and checkpoints still need human review.

## Inspiration

Inspired by [agent-communication-system](https://github.com/anon5376/agent-communication-system):
durable handoffs, explicit state, and observable completion. Agent Rewind is a
new implementation focused on branching and comparison. No ACS code was copied.

MIT licensed. See [CONTRIBUTING.md](CONTRIBUTING.md) for checks and useful small contributions.
An honest recording script is in [examples/DEMO-SCRIPT.md](examples/DEMO-SCRIPT.md).
For a user-operated, two-call proprietary-model trial, see [examples/ANTHROPIC-TRIAL.md](examples/ANTHROPIC-TRIAL.md).
