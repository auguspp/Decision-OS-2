"""Bounded lossless JSON transport; compression grants neither secrecy nor authority."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
from pathlib import Path
import sys
import zlib

from decision_kernel.domain.records import DIGEST, MAX_BYTES, RecordError, digest, encoded, require

MAX_PAYLOAD_BYTES = 60 * 1024
MAX_DECODED_BYTES = MAX_BYTES
TRANSPORT_KEY = "__decision_os_transport__"
TRANSPORT_SCHEMA = "gzip-json-v1"


def json_object(raw: bytes) -> dict:
    """The existing host JSON policy, shared by packing and unpacking."""
    def unique(pairs):
        out = {}
        for key, value in pairs:
            require(key not in out, "DUPLICATE_JSON_KEY")
            out[key] = value
        return out
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        require(isinstance(value, dict), "JSON_OBJECT_REQUIRED")
        encoded(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise RecordError("INVALID_COMMAND_JSON") from None


def decode_payload(text: str) -> dict:
    """Decode at most one gzip member, allocating at most declared bytes plus one.

    Identity is always the decoded canonical JSON, not a particular compressor's
    output. The caller must still validate the operation, authorization and data.
    """
    require(isinstance(text, str), "STRING_PAYLOAD_REQUIRED")
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        raise RecordError("INVALID_COMMAND_UTF8") from None
    require(len(raw) <= MAX_PAYLOAD_BYTES, "DISPATCH_PAYLOAD_TOO_LARGE")
    envelope = json_object(raw)
    require(len(encoded(envelope)) <= MAX_PAYLOAD_BYTES, "DISPATCH_PAYLOAD_TOO_LARGE")
    if TRANSPORT_KEY not in envelope:
        return envelope  # Original plain-JSON inputs keep their original limit.
    require(set(envelope) == {TRANSPORT_KEY, "size", "sha256", "data"}
            and envelope[TRANSPORT_KEY] == TRANSPORT_SCHEMA, "INVALID_PAYLOAD_ENVELOPE")
    size = envelope["size"]
    require(type(size) is int and 1 <= size <= MAX_DECODED_BYTES, "INVALID_DECODED_SIZE")
    require(isinstance(envelope["sha256"], str) and DIGEST.fullmatch(envelope["sha256"]) is not None,
            "INVALID_PAYLOAD_DIGEST")
    require(isinstance(envelope["data"], str), "INVALID_PAYLOAD_BASE64")
    try:
        compressed = base64.b64decode(envelope["data"], validate=True)
        require(base64.b64encode(compressed).decode("ascii") == envelope["data"], "INVALID_PAYLOAD_BASE64")
    except (ValueError, binascii.Error):
        raise RecordError("INVALID_PAYLOAD_BASE64") from None
    stream = zlib.decompressobj(wbits=31)
    try:
        decoded = stream.decompress(compressed, max_length=size + 1)
    except zlib.error:
        raise RecordError("INVALID_PAYLOAD_GZIP") from None
    # No unlimited flush, recursive envelope, concatenated stream or hidden tail.
    require(len(decoded) == size and stream.eof and not stream.unconsumed_tail
            and not stream.unused_data, "PAYLOAD_SIZE_OR_STREAM_MISMATCH")
    require(digest(decoded) == envelope["sha256"], "PAYLOAD_DIGEST_MISMATCH")
    value = json_object(decoded)
    require(TRANSPORT_KEY not in value, "NESTED_PAYLOAD_ENVELOPE")
    require(encoded(value) == decoded, "PAYLOAD_NOT_CANONICAL_JSON")
    return value


def pack_payload(raw: bytes) -> str:
    """Pack a reviewed local JSON object without changing any field or string.

    This does not save, transmit, authorize publication, or validate research.
    Incompressible inputs that cannot fit the native field are rejected intact.
    """
    require(isinstance(raw, bytes) and len(raw) <= MAX_DECODED_BYTES, "DECODED_PAYLOAD_TOO_LARGE")
    value = json_object(raw)
    require(TRANSPORT_KEY not in value, "NESTED_PAYLOAD_ENVELOPE")
    canonical = encoded(value)
    require(len(canonical) <= MAX_DECODED_BYTES, "DECODED_PAYLOAD_TOO_LARGE")
    envelope = {TRANSPORT_KEY: TRANSPORT_SCHEMA, "size": len(canonical), "sha256": digest(canonical),
                "data": base64.b64encode(zlib.compress(canonical, level=9, wbits=31)).decode("ascii")}
    packed = encoded(envelope).decode("utf-8")
    require(len(packed.encode("utf-8")) <= MAX_PAYLOAD_BYTES, "COMPRESSED_PAYLOAD_TOO_LARGE")
    require(encoded(decode_payload(packed)) == canonical, "PAYLOAD_ROUNDTRIP_FAILED")
    return packed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pack authorized JSON losslessly; offline, no publication or credentials")
    parser.add_argument("--input", type=Path, required=True, help="reviewed UTF-8 JSON payload, not a script or URL")
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as handle:
            raw = handle.read(MAX_DECODED_BYTES + 1)
        packed = pack_payload(raw)
        sys.stdout.write(packed)
        return 0
    except (RecordError, OSError) as error:
        code = str(error) if isinstance(error, RecordError) else "PAYLOAD_FILE_UNREADABLE"
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
