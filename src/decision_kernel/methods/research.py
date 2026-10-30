"""Finite trusted methods, with separate new-write and historical read selection."""
from decision_kernel.domain.records import DIGEST, RecordError, require
from decision_kernel.methods.external_full_v1 import external_full_method as external_v1
from decision_kernel.methods.external_full_v2 import EXTERNAL_METHOD_ID, external_full_method
from decision_kernel.methods.research_v1 import research_method as method_v1
from decision_kernel.methods.full_v2 import full_method as full_v2
from decision_kernel.methods.full_v3 import full_method


def _writable_methods(kind: str) -> tuple[dict, ...]:
    require(kind in ("quick", "full"), "NOT_A_RESEARCH_KIND")
    return (full_method(), external_full_method()) if kind == "full" else (method_v1(kind),)


def _retained_methods(kind: str) -> tuple[dict, ...]:
    """All known bytes for exact reads/reconciliation, never a new-write allowlist."""
    current = _writable_methods(kind)
    return (*current, full_v2(), external_v1(), method_v1(kind)) if kind == "full" else current


def is_external_method(method: dict | None) -> bool:
    """Recognize only the finite trusted ID/digest pairs, including archived reads."""
    if not isinstance(method, dict):
        return False
    identity = (method.get("id"), method.get("sha256"))
    return any(identity == (candidate["id"], candidate["sha256"])
               for candidate in (external_full_method(), external_v1()))


def research_method(kind: str, *, sha256: str | None = None, method_id: str | None = None) -> dict:
    """A method digest identifies content; it never grants write/research authority.

    Retained method bytes are unchanged. IDs select current supported
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
    for candidate in _retained_methods(kind):
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
    for candidate in _retained_methods(kind):
        if stored_token == candidate["id"] + "@sha256:" + candidate["sha256"]:
            return candidate
    return None


def continuation_method(kind: str, previous_method: str | None = None, *,
                        method_id: str | None = None) -> dict:
    """Upgrade within the known contract family; never turn external work into Pool work."""
    if method_id is not None:
        return research_method(kind, method_id=method_id)
    previous = stored_research_method(kind, previous_method)
    if is_external_method(previous):
        return external_full_method()
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
