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
import time

from decision_kernel.domain.records import RecordError, require
from decision_kernel.infrastructure.relay_http import CAPABILITIES, RelayHTTP
from decision_kernel.infrastructure.relay_failure import metadata_retry_delay


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
            client=None, env=None, clock=None, retries=0, max_wait_seconds=120, sleep=None):
    require(public_rights_confirmed is True, "CAPABILITIES_PUBLIC_RIGHTS_REQUIRED")
    require(isinstance(code_sha, str) and re.fullmatch(r"[a-f0-9]{40}", code_sha),
            "CAPABILITIES_CODE_SHA_REQUIRED")
    require(type(retries) is int and 0 <= retries <= 2, "CAPABILITIES_RETRY_BOUND_INVALID")
    require(type(max_wait_seconds) is int and 0 <= max_wait_seconds <= 120,
            "CAPABILITIES_WAIT_BOUND_INVALID")
    require(api is None or isinstance(api, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", api),
            "RELAY_CAPABILITY_NAME_INVALID")
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
    sleep = time.sleep if sleep is None else sleep
    receipt.update(attempts=[], retries_allowed=retries, total_wait_seconds=0)
    def persist():
        (out / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    persist()  # A process interruption must not erase the fact that a run started.
    for number in range(1, retries + 2):
        attempt = {"attempt": number, "requested_at": clock(), "status": "not-completed"}
        receipt["attempts"].append(attempt)
        persist()
        try:
            body = client.capabilities(api)
            require(isinstance(body, (dict, list)), "CAPABILITIES_BODY_INVALID")
            if isinstance(body, dict):
                require(body.get("ok", True) is not False and body.get("success", True) is not False
                        and (body.get("code") is None or type(body["code"]) is int and body["code"] == 0)
                        and not body.get("error"), "CAPABILITIES_SERVICE_FAILED")
            public_metadata(body)
            raw = client.last_response_bytes
            require(isinstance(raw, bytes) and sha256(raw).hexdigest() ==
                    client.last_response_meta["wire_sha256"], "CAPABILITIES_RAW_IDENTITY_INVALID")
            (out / "capabilities.raw.json").write_bytes(raw)
            attempt.update(status="captured", received_at=clock(), transport=client.last_response_meta)
            receipt.update(status="captured", received_at=attempt["received_at"], bytes=len(raw),
                           sha256=sha256(raw).hexdigest(), transport=client.last_response_meta)
            receipt.pop("error", None)
            receipt.pop("retry_outcome", None)
            break
        except RecordError as exc:
            failure = getattr(client, "last_error_meta", None)
            attempt.update(status="failed", received_at=clock(), error=str(exc), failure=failure)
            receipt.update(status="failed", received_at=attempt["received_at"], error=str(exc))
            delay = metadata_retry_delay(failure, number)
            decision = ("not-retryable" if delay is None else "attempts-exhausted" if number > retries
                        else "wait-budget-exceeded" if receipt["total_wait_seconds"] + delay > max_wait_seconds
                        else "wait")
            attempt.update(retry_decision=decision, required_wait_seconds=delay)
            receipt["retry_outcome"] = decision
        finally:
            persist()  # All completed attempts survive a subsequent sleep/request interruption.
        if decision != "wait":
            break
        sleep(delay)  # Never shorten server Retry-After to fit this job's wait budget.
        receipt["total_wait_seconds"] += delay
        attempt["wait_completed"] = True
        persist()
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--api", help="optional exact metadata entry; never calls /pro")
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--public-rights-confirmed", action="store_true")
    parser.add_argument("--retries", type=int, choices=(0, 1, 2), default=0)
    parser.add_argument("--max-wait-seconds", type=int, default=120, help="total wait budget; long Retry-After is not shortened")
    args = parser.parse_args(argv)
    try:
        result = capture(args.out, code_sha=args.code_sha, api=args.api,
                         public_rights_confirmed=args.public_rights_confirmed,
                         retries=args.retries, max_wait_seconds=args.max_wait_seconds)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "captured" else 2
    except (RecordError, OSError, ValueError, TypeError):
        print('{"status":"failed","error":"CAPABILITIES_CAPTURE_REJECTED"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
