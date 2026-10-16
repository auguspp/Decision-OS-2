"""Operating-cycle consumer of the original retained industry packet and scopes."""
from contextlib import ExitStack

from decision_kernel.application.industry_reading import read_industry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import identifier, require
from decision_kernel.application.operating_cycle import operating_cycle


def read_industry_operations(reference, store: VersionedStore, *, subject_id=None, company_id=None):
    """Read exact sources once per bounded native scope; no writes or new budget.

    The existing GitHub source_read_scope checks mutable access before AND after
    using immutable bytes. ExitStack closes every scope before returning output.
    Local backends keep their existing reads. Filters never change whole counts.
    """
    require(company_id is None, 'OPERATING_COMPANY_QUERY_USE_ORIGINAL_INDUSTRY_READER')
    if subject_id is not None:
        identifier(subject_id)
    scope = getattr(store.backend, 'source_read_scope', None)
    with ExitStack() as stack:
        readers = {}
        class Reader:
            def read_source(self, ref):
                if scope is None:
                    return store.read_source(ref)
                commit = ref.manifest.commit
                if commit not in readers:
                    require(len(readers) < 32, 'OPERATING_SOURCE_SCOPE_BOUND')
                    readers[commit] = VersionedStore(stack.enter_context(scope(commit)))
                return readers[commit].read_source(ref)
        retained = read_industry(reference, Reader())
        result = operating_cycle(retained, subject_id=subject_id)
    return result
