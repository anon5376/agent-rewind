# User-operated Anthropic trial

Open your own macOS Terminal window. The exact command is:

```bash
cd agent-rewind
python3 -B -m agent_rewind.anthropic_trial
```

The launcher shows the price estimate and promo-only funding condition first.
At this prompt, paste the key and press Enter to confirm and send the trial:

```text
Anthropic API key (hidden; Enter confirms 2 requests):
```

Characters are hidden. Empty input or Ctrl-C cancels. The launcher refuses
pipes and agent terminals, reads no existing credential file or environment key,
and keeps the supplied key only in its process memory. It disables core dumps,
HTTP debug output and TLS key logging. Errors contain fixed nonsensitive codes;
the key is absent from command arguments, shell history, artifacts and reports.

It sends at most two Messages API requests for a fixed artificial slugify task,
with at most 1024 output tokens per call. No automatic retries, tools, private
project input, test execution or model installation. Results go to a new
`.demo/anthropic-…/` directory: two copies, model usage metadata, comparison and
summary. No patch is selected automatically. Review and test after the launcher
process has exited. This is a proprietary-model API trial; open-weight contest
evidence remains separate.

The official [Haiku 5.5 pricing](https://platform.claude.com/docs/en/about-claude/pricing)
is $0.10 per million input tokens and $0.50 per million output tokens for short
prompts. A deliberately generous 10,000 input tokens plus 1024 output tokens per
call gives $0.003024 for two calls; reserve $0.01 promo credit. The fixed request
body is below 8 KiB. Rates were checked on 14 October 2026. This is an estimate,
not an account balance or billing receipt. The script cannot determine whether
credits are promotional: confirm the balance and absence of card/auto-reload in
Claude Console before pressing Enter.

The adapter follows the official [Messages API](https://platform.claude.com/docs/en/api/messages/create)
and [authentication](https://platform.claude.com/docs/en/api/overview) contracts.
Haiku 5.5 [supports disabled thinking at low effort](https://platform.claude.com/docs/en/build-with-claude/thinking).
All adapter tests use inert markers and mocked HTTPS; no real API request was
made during implementation. An optional `--ready-file PATH` writes only fixed
readiness metadata after the hidden prompt is printed and terminal echo is off.

After implementation, a user-operated real trial completed two responses and
local verification. The aggregate results are recorded in the README; run files
and credentials are excluded from publication.
