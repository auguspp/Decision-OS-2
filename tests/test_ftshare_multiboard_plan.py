"""Exact independent-candidate expansion configuration (no source requests)."""
import json
from pathlib import Path

from decision_kernel.interfaces.ftshare_collection_cli import configuration


def test_trusted_independent_generation_preserves_v4_base_and_daily_pool():
    config_root = Path(__file__).resolve().parents[1] / "config"
    path = config_root / "ftshare-daily.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    assert configuration(value) == value
    assert value["collection_id"] == "ftshare-cn-eod"
    assert value["generation"] == "g20261020-independent-v1"
    assert not value["generation"].startswith("daily-")
    assert value["qualification"] is None
    assert value["kernel_adoption"]["run_id"] == 39187632132
    sync = value["kernel_sync"]
    assert set(sync) == {"base_reference", "start", "end", "targets", "source_budget", "completion_policy"}
    assert sync["base_reference"] == {
        "manifest": {
            "commit": "d8ce218889ff19fd85a1ef9dbd3d68d7d5e1fbd6",
            "path": "source-views/ftshare-collection-7fa4428ff5ea645a694ffb0270daabe7-r024/ce47828c5b6eb9ce532cde6558751303243aaf702f968a653afe89fcff3525b2/manifest.json",
            "repository": "auguspp/Decision-OS-2",
            "sha256": "345a181424a101c6011923f7717fdf3df747b200a71001f8866657b32b10b869"
        },
        "selection": {
            "columns": [],
            "end": None,
            "kind": "all",
            "start": None
        }
    }
    assert (sync["start"], sync["end"]) == ("20260701", "20261020")
    assert sync["completion_policy"] == "ashare-eod-1835-v1"
    expected = {(symbol, kind) for symbol in ("000530.SZ", "002343.SZ", "300006.SZ", "301052.SZ")
                for kind in ("none", "forward")}
    actual = {(row["symbol"], row["adjust_kind"]) for row in sync["targets"]}
    assert actual == expected and len(sync["targets"]) == 8 and sync["source_budget"] == 8
    daily = json.loads((config_root / "daily-market.json").read_text(encoding="utf-8"))
    assert {symbol for symbol, _ in actual}.isdisjoint(daily["symbols"])
