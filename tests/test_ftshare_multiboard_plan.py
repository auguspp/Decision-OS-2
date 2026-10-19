"""Exact bounded next-generation market configuration (no source requests)."""
import json
from pathlib import Path

from decision_kernel.interfaces.ftshare_collection_cli import configuration


def test_trusted_multiboard_generation_has_exact_retained_predecessor_and_budget():
    path = Path(__file__).resolve().parents[1] / "config" / "ftshare-daily.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    assert configuration(value) == value
    assert value["collection_id"] == "ftshare-cn-eod"
    assert value["generation"] == "g20261016-v2"
    assert value["qualification"] is None
    assert value["kernel_adoption"]["run_id"] == 39187632132
    sync = value["kernel_sync"]
    assert set(sync) == {"base_reference", "start", "end", "targets", "source_budget"}
    assert sync["base_reference"] == {
        "manifest": {
            "commit": "2649d19995f28399d3b3fced12ba4f86b1d5a5a9",
            "path": "source-views/ftshare-collection-bacbf1600c88094b6e87d13b2a82f876-r002/45e1a2cc1ed8df4a6d1de11477624c988fce22c533bac6c5f891f4688466f24c/manifest.json",
            "repository": "auguspp/Decision-OS-2",
            "sha256": "758516c20e906debf3677828cc1abec21d5ce52d846de29c371200a9c160cd0d"
        },
        "selection": {
            "columns": [],
            "end": null,
            "kind": "all",
            "start": null
        }
    }
    assert (sync["start"], sync["end"]) == ("20260701", "20261016")
    expected = {(symbol, kind) for symbol in ("002384.SZ", "002714.SZ", "300750.SZ", "688981.SH")
                for kind in ("none", "forward")}
    actual = {(row["symbol"], row["adjust_kind"]) for row in sync["targets"]}
    assert actual == expected and len(sync["targets"]) == 8 and sync["source_budget"] == 8
    assert not any(symbol in {"000001.SZ", "600000.SH"} for symbol, _ in actual)
