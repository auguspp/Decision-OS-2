"""Current method selection plus exact historical reconstruction for read-only replay."""
from decision_kernel.domain.records import DIGEST, RecordError, require
from decision_kernel.methods.research_v1 import research_method as method_v1
from decision_kernel.methods.full_v2 import full_method


def research_method(kind: str, *, sha256: str | None = None) -> dict:
    """A method digest identifies content; it never grants write/research authority.

    Existing v1 methods remain byte-identical. New Full uses v2; Quick is unchanged.
    Runtime writes require the current method; read-only reconciliation can select
    an archived digest to reconstruct the original intent after a method upgrade.
    """
    require(kind in ("quick", "full"), "NOT_A_RESEARCH_KIND")
    current = full_method() if kind == "full" else method_v1(kind)
    if sha256 is None:
        return current
    require(isinstance(sha256, str) and bool(DIGEST.fullmatch(sha256)), "INVALID_METHOD_DIGEST")
    for candidate in (current, method_v1(kind)):
        if candidate["sha256"] == sha256:
            return candidate
    raise RecordError("UNKNOWN_RESEARCH_METHOD")


def main(argv=None) -> int:
    """Installed, offline method reader; no store, credentials or research execution."""
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser(description="Read research discipline; never execute or authorize research")
    parser.add_argument("--kind", choices=("quick", "full"), required=True)
    parser.add_argument("--sha256", help="exact retained method digest; omitted selects current")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(research_method(args.kind, sha256=args.sha256), ensure_ascii=False, sort_keys=True))
        return 0
    except RecordError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
