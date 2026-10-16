"""Replay and export one finite material-price capture through existing custody."""
from copy import deepcopy
import csv
from datetime import datetime
import io

from decision_kernel.application.industry_reading import COLUMNS
from decision_kernel.domain.records import Entity, SourceInput, digest, encoded, require
from decision_kernel.observations.material_prices import (
    ATTRIBUTION, ROOT, allowed, discover, monitor, parse_release,
)


def seal(report, *, predecessor=None):
    prior = None
    if predecessor is not None:
        verify(predecessor)
        require(datetime.fromisoformat(predecessor["report"]["as_of"])
                <= datetime.fromisoformat(report["as_of"]), "MATERIAL_PREDECESSOR_AFTER_CAPTURE")
        prior = {"capture_sha256": predecessor["capture_sha256"],
                 "releases": predecessor["report"]["releases"][-1:]}
    value = {"schema": "material-price-capture-v1", "report": report, "predecessor": prior}
    value["capture_sha256"] = digest(encoded(value))
    return verify(value)


def verify(value):
    require(isinstance(value, dict) and set(value) ==
            {"schema", "report", "predecessor", "capture_sha256"}
            and value["schema"] == "material-price-capture-v1", "MATERIAL_CAPTURE_SHAPE")
    body = {k: v for k, v in value.items() if k != "capture_sha256"}
    require(digest(encoded(body)) == value["capture_sha256"], "MATERIAL_CAPTURE_DIGEST")
    report = value["report"]
    require(monitor(report["releases"], as_of=report["as_of"], receipts=report["receipts"],
                    predecessor=value["predecessor"]) == report, "MATERIAL_RESULT_DIFFERS")
    return value


def replay(value, originals):
    """Actual retained bytes are necessary for offline original replay."""
    verify(value)
    report, releases = value["report"], []
    captured = set()
    for receipt in report["receipts"]:
        allowed(receipt["url"])
        require(datetime.fromisoformat(receipt["requested_at"])
                <= datetime.fromisoformat(receipt["received_at"])
                <= datetime.fromisoformat(report["as_of"]), "MATERIAL_RECEIPT_CLOCK")
        if "response_sha256" not in receipt:
            continue
        raw = originals[receipt["url"]]
        require(digest(raw) == receipt["response_sha256"]
                and len(raw) == receipt["response_bytes"], "MATERIAL_ORIGINAL_CHANGED")
        captured.add(receipt["url"])
        if receipt["status"] == "captured":
            if receipt["title"] is None:
                discover(raw, receipt["url"])
            else:
                releases.append(parse_release(raw, url=receipt["url"], title=receipt["title"],
                                              received_at=receipt["received_at"]))
    require(set(originals) == captured, "MATERIAL_ORIGINAL_INVENTORY")
    require(monitor(releases, as_of=report["as_of"], receipts=report["receipts"],
                    predecessor=value["predecessor"]) == report, "MATERIAL_REPLAY_DIFFERS")
    return deepcopy(report)


def source_input(value, source_id):
    verify(value)
    return SourceInput(source_id=source_id, kind="content", title="生产资料价格机械监测",
                       origin=ROOT, text=encoded(value).decode(), custody="provided",
                       acquired_at=value["report"]["as_of"],
                       limitations=(ATTRIBUTION, "EXTRACTED_STATISTICS_AND_DERIVED_RESULTS_NOT_ORIGINAL_HTML",
                                    "ORIGINAL_RESPONSE_HASHES_PRESENT_LOCAL_RAW_BYTES_REQUIRED_FOR_REPLAY",
                                    "BOUNDED_NBS_SLICE_NOT_ALL_INDUSTRY_OR_COMPANY_BENEFIT"))


def dataset_inputs(value):
    """Each release is a distinct immutable version of the same survey dataset."""
    verify(value)
    sources = []
    for release in value["report"]["releases"]:
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        entities = []
        for row in release["rows"]:
            entities.append(Entity(row["subject_id"], "commodity", row["name"]))
            basis = {k: v for k, v in release["basis"].items() if k in COLUMNS}
            writer.writerow({**basis, "subject_id": row["subject_id"], "specification": row["name"],
                             "unit": row["unit"], "period_start": release["period_start"],
                             "period_end": release["period_end"], "value": row["value"] or ""})
        sources.append(SourceInput(source_id="nbs-material-prices", kind="dataset",
            title=release["title"], origin=ROOT, text=stream.getvalue(), entities=tuple(entities),
            acquired_at=release["received_at"], published_at=release["published_at"],
            report_period=release["period_start"] + "/" + release["period_end"],
            limitations=(ATTRIBUTION, "NUMERIC_EXTRACTION_NOT_RETAINED_ORIGINAL",
                         "RELEASE_URL=" + release["url"], "RESPONSE_SHA256=" + release["response_sha256"],
                         "PUBLISHED_SURVEY_NOT_COMPANY_CONTRACT_OR_EX_FACTORY_PRICE")))
    return sources
