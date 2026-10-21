"""Exact market work checkpoints and collection manifests in the existing store.

Checkpoints retain execution control separately from the existing source page
projection. Collection manifests contain references, not concatenated market
tables. Neither representation authorizes a source request or a public export.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from decision_kernel.infrastructure.runtime_performance import measure_phase, timed

from decision_kernel.application.market_custody import (
    _document, _put, _read_source, authorize_market_custody, fresh_market_reader,
    market_read_scope,
)
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    MAX_BYTES, Selection, SourceInput, SourceRef, digest, encoded, identifier, require,
)

CHECKPOINT_SCHEMA = "market-capture-checkpoint-custody-v1"
COLLECTION_SCHEMA = "market-collection-custody-v1"
REPAIR_COLLECTION_SCHEMA = "market-collection-custody-v2"
MAX_CONTROL_BYTES = 512 * 1024
MAX_CHECKPOINT_PAGES = 100
ORIGIN = "https://github.com/auguspp/Decision-OS-2"
LIMITATIONS = (
    "GENERATED_MARKET_CUSTODY_MANIFEST_NOT_ORIGINAL_HTTP_OR_PUBLISHER_AUTHENTICATION",
    "EXACT_REFERENCES_AND_EXECUTION_CONTROL_NOT_SOURCE_PERMISSION_OR_AUTOMATIC_RETRY",
    "NO_LATEST_SUBSTITUTION_OR_SECOND_STORE; SOURCE_RETENTION_AUTHORIZED_BY_TRUSTED_CALLER",
)


def collection_source_prefix(collection_id):
    identifier(collection_id)
    return "market-collection-" + digest(encoded(collection_id))[:24] + "-"


def collection_source_id(collection_id, generation=None):
    if isinstance(collection_id, dict):
        require(generation is None, "MARKET_COLLECTION_ID_INPUT")
        collection_id, generation = collection_id.get("collection_id"), collection_id.get("generation")
    identifier(collection_id); identifier(generation)
    return collection_source_prefix(collection_id) + digest(encoded(generation))[:24]


def checkpoint_source_id(checkpoint_id):
    identifier(checkpoint_id)
    return "market-checkpoint-" + digest(encoded(checkpoint_id))[:48]


def _source(content):
    require(isinstance(content, dict), "MARKET_COLLECTION_CUSTODY_SCHEMA")
    schema = content.get("schema")
    require(schema in (CHECKPOINT_SCHEMA, COLLECTION_SCHEMA, REPAIR_COLLECTION_SCHEMA), "MARKET_COLLECTION_CUSTODY_SCHEMA")
    if schema == CHECKPOINT_SCHEMA:
        source_id = checkpoint_source_id(content.get("checkpoint_id"))
        title = "Retained market execution checkpoint"
    else:
        require(isinstance(content.get("collection"), dict)
                and isinstance(content["collection"].get("plan"), dict), "MARKET_COLLECTION_MANIFEST")
        plan = content["collection"]["plan"]
        source_id = collection_source_id(plan.get("collection_id"), plan.get("generation"))
        title = "Retained market collection"
    return SourceInput(source_id=source_id, kind="content", title=title, origin=ORIGIN,
                       text=encoded(content).decode(), custody="provided", limitations=LIMITATIONS)


def _reference(value, repository, *, optional=False):
    if optional and value is None:
        return None
    reference = value if isinstance(value, SourceRef) else SourceRef.from_dict(value)
    require(reference.selection == Selection() and reference.manifest.repository == repository,
            "MARKET_COLLECTION_EXACT_LOCAL_REFERENCE_REQUIRED")
    return _document(encoded(asdict(reference)))


def _read_manifest(reference, registry, store, schema):
    require(isinstance(registry, RelayRegistry) and isinstance(store, VersionedStore),
            "MARKET_COLLECTION_CUSTODY_INPUT")
    ref = _reference(reference, store.backend.repository)
    _, view = _read_source(store, ref)
    content = _document(view["data"].encode())
    require(isinstance(content, dict) and content.get("schema") in (schema if isinstance(schema, tuple) else (schema,)),
            "MARKET_COLLECTION_CUSTODY_SCHEMA")
    require(content.get("registry") == registry.snapshot, "MARKET_COLLECTION_REGISTRY_CHANGED")
    expected = _source(content)
    require(encoded({**view["manifest"]["input"], "text": view["data"]})
            == encoded(asdict(expected)), "MARKET_COLLECTION_MANIFEST_SOURCE_CHANGED")
    return ref, content


def _checkpoint(state, registry):
    # The source owner validates the exact capture and finite control state. It
    # also validates them again against the caller's request before source I/O.
    from decision_kernel.application.relay_capture_checkpoint import validate_capture_checkpoint
    from decision_kernel.infrastructure.relay_retry import RetryPolicy
    require(isinstance(state, dict) and isinstance(state.get("control"), dict),
            "MARKET_CHECKPOINT_INPUT")
    control = state["control"]
    policy = control.get("policy")
    require(isinstance(policy, dict) and set(policy) == {"max_attempts", "max_wait_seconds"}
            and type(policy["max_attempts"]) is int and 1 <= policy["max_attempts"] <= 4
            and type(policy["max_wait_seconds"]) is int and 0 <= policy["max_wait_seconds"] <= 180,
            "MARKET_CHECKPOINT_POLICY")
    require(type(control.get("max_pages")) is int and 1 <= control["max_pages"] <= MAX_CHECKPOINT_PAGES,
            "MARKET_CHECKPOINT_PAGE_BOUND")
    checked = validate_capture_checkpoint(state, control.get("request"), registry,
        max_pages=control["max_pages"], policy=RetryPolicy(**policy))
    require(len(encoded({"control": checked["control"],
        "capture": {k: v for k, v in checked["capture"].items() if k != "pages"},
        "page_attempts": [p["attempts"] for p in checked["capture"]["pages"]]})) <= MAX_CONTROL_BYTES,
        "MARKET_CHECKPOINT_CONTROL_BOUND")
    return checked


def _page_sources(state, checkpoint_id, registry):
    from decision_kernel.application.research_sources import source_page_inputs
    capture = state["capture"]
    result = {"case_id": checkpoint_id, "use": state["control"]["request"],
              "status": "captured" if capture["complete"] else "capture-incomplete", "capture": capture}
    return source_page_inputs(result, registry)


def _checkpoint_manifest(reference, registry, store):
    ref, content = _read_manifest(reference, registry, store, CHECKPOINT_SCHEMA)
    require(set(content) == {"schema", "checkpoint_id", "registry", "predecessor", "state_schema",
        "capture", "control", "pages", "state_sha256"}, "MARKET_CHECKPOINT_MANIFEST")
    identifier(content["checkpoint_id"])
    require(isinstance(content["capture"], dict) and "pages" not in content["capture"]
            and isinstance(content["control"], dict) and isinstance(content["pages"], list)
            and len(content["pages"]) <= MAX_CHECKPOINT_PAGES, "MARKET_CHECKPOINT_MANIFEST")
    _reference(content["predecessor"], store.backend.repository, optional=True)
    for page in content["pages"]:
        require(isinstance(page, dict) and set(page) == {"reference", "source_sha256", "attempts"}
                and isinstance(page["attempts"], list), "MARKET_CHECKPOINT_PAGE_REFERENCE")
        _reference(page["reference"], store.backend.repository)
    return ref, content


@timed("checkpoint.save")
def retain_market_checkpoint(state, registry, store, *, checkpoint_id, fresh_reader, predecessor=None,
                             custody_authorized=False, public_rights_confirmed=False):
    """Save accepted pages once, then synchronously save and read back control.

    An unknown write or failed readback raises and therefore prevents the source
    caller from issuing its next request. No automatic retry or alternate key is
    introduced; stable SourceInputs retain the existing journal reconciliation.
    """
    authorize_market_custody(store, custody_authorized=custody_authorized,
                            public_rights_confirmed=public_rights_confirmed)
    identifier(checkpoint_id)
    with measure_phase("checkpoint.validate"):
        checked = _checkpoint(state, registry)
        sources = _page_sources(checked, checkpoint_id, registry)
    reader = fresh_market_reader(store, fresh_reader)
    previous = None
    predecessor = _reference(predecessor, store.backend.repository, optional=True)
    if predecessor is not None:
        _, previous = _checkpoint_manifest(predecessor, registry, store)
        require(previous["checkpoint_id"] == checkpoint_id
                and previous["control"]["request"] == checked["control"]["request"]
                and previous["control"]["max_pages"] == checked["control"]["max_pages"]
                and previous["control"]["policy"] == checked["control"]["policy"],
                "MARKET_CHECKPOINT_PREDECESSOR_CHANGED")
        require(len(previous["pages"]) <= len(sources), "MARKET_CHECKPOINT_PAGE_HISTORY_CHANGED")
        for old, source, page in zip(previous["pages"], sources, checked["capture"]["pages"]):
            require(old["source_sha256"] == digest(encoded(asdict(source)))
                    and old["attempts"] == page["attempts"], "MARKET_CHECKPOINT_PAGE_HISTORY_CHANGED")
        if previous["state_sha256"] == digest(encoded(checked)):
            with measure_phase("checkpoint.readback"):
                restored = restore_market_checkpoint(predecessor, registry, reader)
            require(encoded(restored["state"]) == encoded(checked), "MARKET_CHECKPOINT_READBACK_CHANGED")
            return {"status": "stored-and-independently-restored-checkpoint", "reference": predecessor,
                    "source_id": checkpoint_source_id(checkpoint_id), "page_count": len(sources),
                    "state_sha256": previous["state_sha256"], "restored": restored}
        # Accepted page hashes and attempts above authenticate this prefix to
        # the exact predecessor, so its full state can be reconstructed without
        # fetching the same successful page bodies again.
        from decision_kernel.application.relay_capture_checkpoint import validate_capture_transition
        from decision_kernel.infrastructure.relay_retry import RetryPolicy
        previous_state = {"schema": previous["state_schema"],
            "capture": {**previous["capture"],
                "pages": deepcopy(checked["capture"]["pages"][:len(previous["pages"])])},
            "control": previous["control"]}
        require(digest(encoded(previous_state)) == previous["state_sha256"],
                "MARKET_CHECKPOINT_PREDECESSOR_CHANGED")
        validate_capture_transition(previous_state, checked, checked["control"]["request"], registry,
            max_pages=checked["control"]["max_pages"], policy=RetryPolicy(**checked["control"]["policy"]))
    content = {"schema": CHECKPOINT_SCHEMA, "checkpoint_id": checkpoint_id,
        "registry": deepcopy(registry.snapshot), "predecessor": predecessor, "state_schema": checked["schema"],
        "capture": {k: deepcopy(v) for k, v in checked["capture"].items() if k != "pages"},
        "control": deepcopy(checked["control"]), "pages": [], "state_sha256": digest(encoded(checked))}
    # Validate the generated control SourceInput before any page is written.
    _source(content)
    for index, (source, page) in enumerate(zip(sources, checked["capture"]["pages"])):
        if previous is not None and index < len(previous["pages"]):
            reference = previous["pages"][index]["reference"]
        else:
            with measure_phase("checkpoint.page_save"):
                reference = asdict(_put(store, source))
        content["pages"].append({"reference": reference,
            "source_sha256": digest(encoded(asdict(source))), "attempts": deepcopy(page["attempts"])})
    with measure_phase("checkpoint.control_save"):
        reference = _put(store, _source(content))
    with measure_phase("checkpoint.readback"):
        restored = restore_market_checkpoint(reference, registry, reader)
    require(encoded(restored["state"]) == encoded(checked), "MARKET_CHECKPOINT_READBACK_CHANGED")
    return {"status": "stored-and-independently-restored-checkpoint",
            "reference": _reference(reference, store.backend.repository),
            "source_id": checkpoint_source_id(checkpoint_id), "page_count": len(sources),
            "state_sha256": content["state_sha256"], "restored": restored}


def restore_market_checkpoint(reference, registry, store):
    """Restore the original projected capture AND its retained execution control."""
    with market_read_scope(store) as reader:
        return _restore_market_checkpoint(reference, registry, reader)


def _restore_market_checkpoint(reference, registry, store):
    ref, content = _checkpoint_manifest(reference, registry, store)
    pages, originals, seen, total = [], [], set(), 0
    for entry in content["pages"]:
        encoded_ref = encoded(entry["reference"])
        require(encoded_ref not in seen, "MARKET_CHECKPOINT_DUPLICATE_PAGE")
        seen.add(encoded_ref)
        _, view = _read_source(store, entry["reference"])
        source = {**view["manifest"]["input"], "text": view["data"]}
        require(digest(encoded(source)) == entry["source_sha256"], "MARKET_CHECKPOINT_PAGE_CHANGED")
        total += len(view["data"].encode())
        require(total <= 16 * 1024 * 1024, "MARKET_CHECKPOINT_CAPTURE_BOUND")
        body = _document(view["data"].encode())
        require(isinstance(body, dict) and set(body) == {"schema", "api", "parameters", "page"}
                and body["schema"] == content["capture"].get("schema")
                and body["api"] == content["capture"].get("api")
                and body["parameters"] == content["capture"].get("parameters")
                and isinstance(body["page"], dict)
                and set(body["page"]) == {"offset", "captured_at", "table", "transport"},
                "MARKET_CHECKPOINT_PAGE_SCOPE")
        pages.append({**body["page"], "attempts": deepcopy(entry["attempts"])})
        originals.append(source)
    state = {"schema": content["state_schema"], "capture": {**content["capture"], "pages": pages},
             "control": content["control"]}
    checked = _checkpoint(state, registry)
    require(digest(encoded(checked)) == content["state_sha256"], "MARKET_CHECKPOINT_STATE_CHANGED")
    require(encoded([asdict(s) for s in _page_sources(checked, content["checkpoint_id"], registry)]) == encoded(originals),
            "MARKET_CHECKPOINT_SOURCE_CHANGED")
    return {"status": "restored-execution-checkpoint", "reference": ref, "state": checked,
            "checkpoint_id": content["checkpoint_id"], "predecessor": content["predecessor"],
            "source_id": checkpoint_source_id(content["checkpoint_id"]),
            "page_references": [deepcopy(page["reference"]) for page in content["pages"]],
            "original_http_restored": False, "execution_control_restored": True}


def _collection(collection, registry, repository):
    from decision_kernel.application.market_collection import validate_collection
    checked = validate_collection(collection, registry)
    require(len(encoded(checked)) <= MAX_BYTES // 2, "MARKET_COLLECTION_MANIFEST_BOUND")
    attempt = checked["attempt"]
    require(all(isinstance(attempt[k], str) and "T" in attempt[k] for k in ("started_at", "updated_at")),
            "MARKET_COLLECTION_CLOCK")
    require(datetime.fromisoformat(attempt["updated_at"]) >= datetime.fromisoformat(attempt["started_at"]),
            "MARKET_COLLECTION_CLOCK")
    for unit in checked["units"].values():
        for key in ("query", "checkpoint"):
            _reference(unit[key], repository, optional=True)
    _reference(checked["last_qualified"], repository, optional=True)
    if "repair" in checked:
        repair = checked["repair"]
        request = repair["grant"]["request"]
        _reference(request["base_reference"], repository)
        for reference in request["terminal_references"].values():
            _reference(reference, repository)
        for evidence in request["transport_evidence"].values():
            for key in ("collection_reference", "checkpoint_reference", "query_reference"):
                _reference(evidence[key], repository)
        for unit in repair["units"].values():
            for key in ("query", "checkpoint"):
                _reference(unit[key], repository, optional=True)
    return checked


def _collection_manifest(reference, registry, store):
    ref, content = _read_manifest(reference, registry, store, (COLLECTION_SCHEMA, REPAIR_COLLECTION_SCHEMA))
    require(set(content) == {"schema", "registry", "predecessor", "collection"},
            "MARKET_COLLECTION_MANIFEST")
    _reference(content["predecessor"], store.backend.repository, optional=True)
    checked = _collection(content["collection"], registry, store.backend.repository)
    require((content["schema"] == REPAIR_COLLECTION_SCHEMA) == ("repair" in checked),
            "MARKET_COLLECTION_REPAIR_SCHEMA")
    return ref, {**content, "collection": checked}


def retain_market_collection(collection, registry, store, *, fresh_reader, predecessor=None,
                             custody_authorized=False, public_rights_confirmed=False,
                             verify_evidence=None):
    """Retain the exact finite plan and SourceRef slots, never supplier tables.

    V1 restores the manifest. V2 also independently proves its immutable grant,
    original exhausted terminals, transport evidence and repair dependencies.
    A trusted native verifier is required at the one explicit v1-to-v2 edge.
    """
    visibility = authorize_market_custody(store, custody_authorized=custody_authorized,
                            public_rights_confirmed=public_rights_confirmed)
    checked = _collection(collection, registry, store.backend.repository)
    require("repair" not in checked or visibility in {"private", "synthetic"},
            "ASHARE_REPAIR_PRIVATE_CUSTODY_REQUIRED")
    reader = fresh_market_reader(store, fresh_reader)
    predecessor = _reference(predecessor, store.backend.repository, optional=True)
    previous = None
    if predecessor is not None:
        _, previous = _collection_manifest(predecessor, registry, store)
        require(previous["collection"]["plan"]["collection_id"] == checked["plan"]["collection_id"],
                "MARKET_COLLECTION_PREDECESSOR_CHANGED")
    from decision_kernel.application.market_collection_repair import (
        validate_native_context, validate_repair_records, validate_repair_transition,
    )
    old = None if previous is None else previous["collection"]
    validate_repair_transition(old, checked, registry, predecessor=predecessor)
    activation = "repair" in checked and (old is None or "repair" not in old)
    if activation:
        require(callable(verify_evidence), "ASHARE_REPAIR_NATIVE_EVIDENCE_REQUIRED")
        context = verify_evidence(deepcopy(checked["repair"]["grant"]["request"]))
        grant = checked["repair"]["grant"]
        validate_native_context(context, grant["request"], grant["execution_code_sha"])
        require(context["transport_runs"] == grant["transport_runs"], "ASHARE_REPAIR_NATIVE_RUNS_CHANGED")
    if "repair" in checked:
        from decision_kernel.application.market_collection_reading import inspect_market_chain, same_reference
        with market_read_scope(reader) as grouped:
            chain = inspect_market_chain(grouped, registry, collection_id=checked["plan"]["collection_id"])
            require(same_reference(chain["reference"], predecessor), "ASHARE_COLLECTION_STALE_REFERENCE")
            _, fresh_previous = _collection_manifest(predecessor, registry, grouped)
            validate_repair_transition(fresh_previous["collection"], checked, registry, predecessor=predecessor)
            validate_repair_records(checked, registry, grouped, activation=activation, chain=chain,
                reference=None if activation else predecessor, previous=fresh_previous["collection"])
        # The grant is a new object. Its readback uses a newly constructed
        # independent reader, never this pre-write verification state.
        reader = fresh_market_reader(store, fresh_reader)
    content = {"schema": REPAIR_COLLECTION_SCHEMA if "repair" in checked else COLLECTION_SCHEMA,
               "registry": deepcopy(registry.snapshot),
               "predecessor": predecessor, "collection": checked}
    source = _source(content)
    reference = _put(store, source)
    restored = restore_market_collection(reference, registry, reader)
    require(encoded(restored["collection"]) == encoded(checked), "MARKET_COLLECTION_READBACK_CHANGED")
    return {"status": "stored-and-independently-restored-collection-manifest",
            "reference": _reference(reference, store.backend.repository),
            "source_id": source.source_id, "readback": restored, "queries_restored": False}


def restore_market_collection(reference, registry, store):
    with market_read_scope(store) as reader:
        return _restore_market_collection(reference, registry, reader)


def _restore_market_collection(reference, registry, store):
    ref, content = _collection_manifest(reference, registry, store)
    if "repair" in content["collection"]:
        from decision_kernel.application.market_collection_repair import validate_repair_records
        _, previous = _collection_manifest(content["predecessor"], registry, store)
        validate_repair_records(content["collection"], registry, store, reference=ref,
            previous=previous["collection"])
    plan = content["collection"]["plan"]
    return {"status": "restored-collection-manifest", "reference": ref,
            "collection": content["collection"], "predecessor": content["predecessor"],
            "source_id": collection_source_id(plan["collection_id"], plan["generation"]),
            "queries_restored": False}
