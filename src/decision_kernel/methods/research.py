"""Finite trusted methods, with separate new-write and historical read selection."""
from decision_kernel.domain.records import DIGEST, RecordError, require
from decision_kernel.methods.external_full_v1 import EXTERNAL_METHOD_ID, external_full_method
from decision_kernel.methods.research_v1 import research_method as method_v1
from decision_kernel.methods.full_v2 import full_method


def _writable_methods(kind: str) -> tuple[dict, ...]:
    require(kind in ("quick", "full"), "NOT_A_RESEARCH_KIND")
    return (full_method(), external_full_method()) if kind == "full" else (method_v1(kind),)


def research_method(kind: str, *, sha256: str | None = None, method_id: str | None = None) -> dict:
    """A method digest identifies content; it never grants write/research authority.

    Defaults and retained method bytes are unchanged. IDs select current supported
    contracts; exact digests can also reconstruct archived methods for read/replay.
    No caller-provided method body or executable code is accepted.
    """
    current = _writable_methods(kind)
    require(sha256 is None or method_id is None, "ONE_RESEARCH_METHOD_SELECTOR_REQUIRED")
    if method_id is not None:
        require(isinstance(method_id, str), "UNKNOWN_RESEARCH_METHOD")
        for candidate in current:
            if candidate["id"] == method_id:
                return candidate
        raise RecordError("UNKNOWN_RESEARCH_METHOD")
    if sha256 is None:
        return current[0]
    require(isinstance(sha256, str) and bool(DIGEST.fullmatch(sha256)), "INVALID_METHOD_DIGEST")
    for candidate in (*current, method_v1(kind)):
        if candidate["sha256"] == sha256:
            return candidate
    raise RecordError("UNKNOWN_RESEARCH_METHOD")


def writable_research_method(kind: str, sha256: str) -> dict:
    """Validate a new hosted write; archived identities confer no new write right."""
    for candidate in _writable_methods(kind):
        if isinstance(sha256, str) and candidate["sha256"] == sha256:
            return candidate
    raise RecordError("RESEARCH_METHOD_CHANGED_REPREPARE")


def stored_research_method(kind: str, stored_token: str) -> dict | None:
    """Recognize both exact identity parts without rejecting free-form old records."""
    if kind not in ("quick", "full") or not isinstance(stored_token, str):
        return None
    for candidate in (*_writable_methods(kind), method_v1(kind)):
        if stored_token == candidate["id"] + "@sha256:" + candidate["sha256"]:
            return candidate
    return None


def continuation_method(kind: str, previous_method: str | None = None, *,
                        method_id: str | None = None) -> dict:
    """Keep an external contract on implicit continuation; default upgrades persist."""
    if method_id is not None:
        return research_method(kind, method_id=method_id)
    previous = stored_research_method(kind, previous_method)
    if previous is not None and previous["id"] == EXTERNAL_METHOD_ID:
        return previous
    return research_method(kind)


def main(argv=None) -> int:
    """Installed, offline method reader; no store, credentials or research execution."""
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser(description="Read research discipline; never execute or authorize research")
    parser.add_argument("--kind", choices=("quick", "full"), required=True)
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--sha256", help="exact retained method digest; omitted selects current")
    selector.add_argument("--method-id", help="exact current supported method ID; no code or method upload")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(research_method(args.kind, sha256=args.sha256, method_id=args.method_id),
                         ensure_ascii=False, sort_keys=True))
        return 0
    except RecordError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
