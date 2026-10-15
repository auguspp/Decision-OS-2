"""Explicit Relay capability metadata capture. Never follows or executes catalogue examples."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys

from decision_kernel.domain.records import RecordError, require
from decision_kernel.infrastructure.relay_http import CAPABILITIES, RelayHTTP


def public_metadata(value):
    """Refuse obvious credential-bearing payloads before retaining a public catalogue."""
    pending = [value]
    sensitive = {"api_key", "apikey", "client_key", "access_token", "refresh_token",
                 "password", "secret", "authorization", "x_api_key"}
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if key.lower().replace("-", "_") in sensitive:
                    require(child in (None, "", "<YOUR_CLIENT_KEY>", "<REDACTED>"),
                            "CAPABILITIES_SENSITIVE_FIELD")
                pending.append(child)
        elif isinstance(item, list):
            pending.extend(item)


def capture(out: Path, *, code_sha: str, public_rights_confirmed=False, api=None,
            client=None, env=None, clock=None):
    require(public_rights_confirmed is True, "CAPABILITIES_PUBLIC_RIGHTS_REQUIRED")
    require(isinstance(code_sha, str) and re.fullmatch(r"[a-f0-9]{40}", code_sha),
            "CAPABILITIES_CODE_SHA_REQUIRED")
    env = dict(os.environ) if env is None else env
    if env.get("GITHUB_ACTIONS") == "true":
        require(env.get("GITHUB_REPOSITORY") == "auguspp/Decision-OS-2" and
                env.get("GITHUB_REF") == "refs/heads/main" and
                env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and
                env.get("GITHUB_SHA") == code_sha, "CAPABILITIES_MANUAL_MAIN_ONLY")
    require(not out.exists() and not out.is_symlink(), "CAPABILITIES_OUTPUT_EXISTS")
    out.mkdir(parents=True, exist_ok=False)
    client = RelayHTTP() if client is None else client
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    receipt = {"schema": "relay-capability-receipt-v1", "code_sha": code_sha,
               "endpoint": CAPABILITIES + ("/" + api if api else ""),
               "requested_at": clock(), "business_requests": 0,
               "status": "not-completed", "run_id": env.get("GITHUB_RUN_ID"),
               "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
               "qualification": "service-metadata-not-business-validation-or-execution-authority"}
    try:
        body = client.capabilities(api)
        require(isinstance(body, (dict, list)), "CAPABILITIES_BODY_INVALID")
        if isinstance(body, dict):
            require(body.get("ok", True) is not False and body.get("success", True) is not False
                    and body.get("code") in (None, 0) and not body.get("error"),
                    "CAPABILITIES_SERVICE_FAILED")
        public_metadata(body)
        raw = client.last_response_bytes
        require(isinstance(raw, bytes) and sha256(raw).hexdigest() ==
                client.last_response_meta["wire_sha256"], "CAPABILITIES_RAW_IDENTITY_INVALID")
        receipt.update(status="captured", received_at=clock(), bytes=len(raw),
                       sha256=sha256(raw).hexdigest(), transport=client.last_response_meta)
        (out / "capabilities.raw.json").write_bytes(raw)
    except RecordError as exc:
        receipt.update(status="failed", received_at=clock(), error=str(exc))
    finally:
        (out / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--api", help="optional exact metadata entry; never calls /pro")
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--public-rights-confirmed", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = capture(args.out, code_sha=args.code_sha, api=args.api,
                         public_rights_confirmed=args.public_rights_confirmed)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "captured" else 2
    except (RecordError, OSError, ValueError, TypeError):
        print('{"status":"failed","error":"CAPABILITIES_CAPTURE_REJECTED"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
