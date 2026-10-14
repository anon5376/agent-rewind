"""Run from this repository: python3 -m agent_rewind --help."""

import argparse
import json
import sys

from .evidence import compare, export_patch, run_test
from .model import continue_run, models
from .storage import RewindError, checkpoint, fork


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = None
    if argv and argv[0] == "test" and "--" in argv:
        separator = argv.index("--")
        command, argv = argv[separator + 1:], argv[:separator]
    parser = argparse.ArgumentParser(description="Checkpoint files/context; compare independent continuations.")
    subs = parser.add_subparsers(dest="action", required=True)
    save = subs.add_parser("checkpoint", help="save filtered files and user-recorded context")
    save.add_argument("project")
    save.add_argument("--context", required=True)
    save.add_argument("--store", required=True)
    branch = subs.add_parser("fork", help="create one fresh continuation copy")
    branch.add_argument("store")
    branch.add_argument("checkpoint")
    branch.add_argument("--name", required=True)
    test = subs.add_parser("test", help="run an explicit command after --, with OS user permissions")
    test.add_argument("store")
    test.add_argument("run")
    test.add_argument("--timeout", type=float, default=30)
    diff = subs.add_parser("compare", help="compare diffs and fresh/stale test evidence")
    diff.add_argument("store")
    diff.add_argument("a")
    diff.add_argument("b")
    export = subs.add_parser("export", help="explicitly choose a run and write a new patch; never apply")
    export.add_argument("store")
    export.add_argument("run")
    export.add_argument("--output", required=True)
    export.add_argument("--allow-source-drift", action="store_true",
                        help="export against the saved checkpoint even if retained source files changed or are unavailable")
    listing = subs.add_parser("models", help="list models from an already-running loopback /v1 endpoint")
    listing.add_argument("--endpoint", required=True)
    listing.add_argument("--timeout", type=float, default=10)
    continuation = subs.add_parser("continue", help="request allowlisted file changes; no model tools or commands")
    continuation.add_argument("store")
    continuation.add_argument("run")
    continuation.add_argument("--endpoint", required=True)
    continuation.add_argument("--model", required=True)
    continuation.add_argument("--files", nargs="+", required=True)
    continuation.add_argument("--instruction", required=True)
    continuation.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)
    if args.action == "test" and not command:
        parser.error("test requires an explicit command after --")
    try:
        if args.action == "checkpoint":
            result = checkpoint(args.project, args.context, args.store)
        elif args.action == "fork":
            result = fork(args.store, args.checkpoint, args.name)
        elif args.action == "test":
            result = run_test(args.store, args.run, command, args.timeout)
        elif args.action == "compare":
            result = compare(args.store, args.a, args.b)
        elif args.action == "export":
            result = export_patch(args.store, args.run, args.output, args.allow_source_drift)
        elif args.action == "models":
            result = {"models": models(args.endpoint, args.timeout), "model_provenance": "operator must verify"}
        else:
            result = continue_run(args.store, args.run, args.endpoint, args.model,
                                  args.files, args.instruction, args.timeout)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if args.action == "test" and result["status"] != "pass" else 0
    except (RewindError, OSError) as exc:
        # OSError paths may be useful; never include file contents or remote bodies.
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
