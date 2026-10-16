"""Metadata identity must not accept Python's bool/int or float/int equality."""
import json
import pytest
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.domain.records import RecordError, encoded
from test_research_delivery import store


@pytest.mark.parametrize("size", [True, 1.0])
def test_manifest_metadata_types_are_not_coerced(store, size):
    artifacts = ResearchArtifacts(store)
    ref = artifacts.put("one", ArtifactInput.from_bytes("one", "one.bin", "application/octet-stream", b"x"))
    manifest = json.loads(store.backend.resolve(ref))
    manifest["input"]["size"] = size
    head = store.backend.commit({ref.path: encoded(manifest)}, expected=store.backend.head())
    with pytest.raises(RecordError, match="MANIFEST_MISMATCH"):
        artifacts.read(store.backend.reference(head, ref.path))
