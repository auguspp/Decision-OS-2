"""Pack explicitly selected local originals for delivery. No network, glob or file execution."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import stat
import sys

from decision_kernel.application.research_delivery import validate_delivery
from decision_kernel.domain.records import MAX_BYTES, RecordError, encoded, require
from decision_kernel.domain.research_artifacts import ArtifactInput, MAX_ARTIFACT_BYTES
from decision_kernel.interfaces.store_cli import _load


def _read(root: Path, relative: str, limit: int) -> bytes:
    require(isinstance(relative, str) and bool(relative) and not Path(relative).is_absolute()
            and all(p not in ("..", ".") for p in Path(relative).parts), "LOCAL_DELIVERY_PATH_INVALID")
    path = root
    for part in Path(relative).parts:
        path = path / part
        require(not path.is_symlink(), "LOCAL_DELIVERY_SYMLINK_REJECTED")
    require(path.resolve().is_relative_to(root), "LOCAL_DELIVERY_PATH_INVALID")
    require(stat.S_ISREG(path.stat().st_mode), "LOCAL_DELIVERY_REGULAR_FILE_REQUIRED")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    require(len(raw) <= limit, "LOCAL_DELIVERY_FILE_TOO_LARGE")
    return raw


def pack(path: Path) -> dict:
    root = path.resolve(strict=True).parent
    payload = _load(path)
    require(payload.get("rights_confirmed") is True, "DELIVERY_RIGHTS_NOT_CONFIRMED")
    progress = payload.get("progress", {})
    if "body_file" in progress:
        require("body" not in progress, "ONE_DELIVERY_BODY_REQUIRED")
        # Decode/re-encode only; preserve CRLF, whitespace, styles and raw file artifacts separately.
        progress["body"] = _read(root, progress.pop("body_file"), MAX_BYTES).decode("utf-8")
    for item in payload.get("items", []):
        local = item.pop("artifact_file", None)
        source_file = item.pop("source_file", None)
        require(not (local is not None and source_file is not None), "ONE_DELIVERY_REPRESENTATION_REQUIRED")
        if local is None and source_file is None:
            continue
        if item.get("status") != "include":
            # Withheld files are never opened, nor are their local paths sent to the host.
            continue
        try:
            if local is not None:
                require(isinstance(local, dict) and set(local) == {"path", "artifact_id", "name", "media_type"},
                        "INVALID_LOCAL_ARTIFACT")
                raw = _read(root, local["path"], MAX_ARTIFACT_BYTES)
                item["artifact"] = asdict(ArtifactInput.from_bytes(local["artifact_id"], local["name"], local["media_type"], raw))
            else:
                require(isinstance(source_file, dict) and set(source_file) == {"path", "metadata"}
                        and isinstance(source_file["metadata"], dict) and "text" not in source_file["metadata"],
                        "INVALID_LOCAL_SOURCE")
                raw = _read(root, source_file["path"], MAX_BYTES)
                item["source"] = {**source_file["metadata"], "text": raw.decode("utf-8")}
        except FileNotFoundError:
            item.update(status="missing", reason="Selected original file was not found; no replacement was manufactured.")
    validate_delivery(payload)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="explicit inventory; local paths relative to this file")
    args = parser.parse_args(argv)
    try:
        result = pack(args.input)
        sys.stdout.buffer.write(encoded(result))
        return 0
    except (RecordError, OSError, ValueError, TypeError, KeyError) as exc:
        code = str(exc) if isinstance(exc, RecordError) else "LOCAL_DELIVERY_INPUT_FAILED"
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
