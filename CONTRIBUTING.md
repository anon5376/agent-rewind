# Contributing

Agent Rewind is an early Python CLI prototype. A useful contribution makes one
part of saving, comparing, or reviewing a continuation easier to trust.

Run from the repository root with Python 3.11+ and Git:

```bash
python3 -m unittest discover -s tests -v
git diff --check
python3 examples/demo.py --output .demo/contribution --select b
```

Tests use temporary directories and a loopback-only fake HTTP server. They use
no models, API keys, external services, or third-party packages. An execution
sandbox must allow loopback binding for the full suite. The manual demo requires
a fresh output directory and labels its continuation files as fixtures.

Add a boundary test for a changed user-facing contract. Keep fake endpoint
results separate from real model evidence. Reproduce model issues with explicit
model/server versions and public license references; omit credentials and private code.

Good next improvements: a clearer comparison display, a second verified local
server recipe, or support for selected binary assets with honest export semantics.
Preserve explicit command execution and patch selection, original-source
preservation, and the documented limits. Please keep contributions focused.
