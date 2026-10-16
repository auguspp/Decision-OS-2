"""Compose native receipt authentication with the existing research/store interfaces."""
from decision_kernel.application.research_source_use import SourceUseAudit
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.infrastructure.research_receipts import NativeResearchReceipts, download_archive
from decision_kernel.interfaces.research_payload import decode_payload


def audit_source_use(store, payload):
    backend = store.backend
    reader = NativeResearchReceipts(backend.binding, backend.api, archive_loader=download_archive) if isinstance(backend, GitHubStore) else None
    return SourceUseAudit(store, receipt_reader=reader, payload_decoder=decode_payload).audit(payload)
