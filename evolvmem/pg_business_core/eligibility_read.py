"""Historical qualification query boundary; no activation or request receipts.

The private historical implementation remains owner-only quarantine validation.
The product constructor uses an independent native-v1 schema/readiness contract;
native activation never grants promotion of historical imported records.
"""
import json
from evolvmem.library_foundation import FoundationError
from evolvmem.library_foundation.sessions import LibrarySession
from evolvmem import source_qualification as verdict


class _EligibilityQueries:
    def __init__(self, scope):
        self.scope = scope

    def _read(self, name, value):
        return self.scope.query(name, (self.scope.library_id, value))

    def currently_backed(self, item_id):
        rows = self._read('backing', item_id)
        if any(r['task_exists'] is None or r['item_exists'] is None for r in rows):
            raise FoundationError('historical_orphan_derivation')
        # Import does not prove that inherited ownership is still current.
        if any(r['stale'] for r in self._read('basis', item_id)):
            raise FoundationError('basis_reconciliation_required')

        def members(key):
            found = self._read('conversation', key)
            if len(found) > 1: raise FoundationError('ambiguous_conversation')
            return {int(k.split(':')[1]) for k in json.loads(found[0]['members'])
                    if k.startswith('archive:')} if found else set()

        return verdict.currently_backed(rows, members, lambda:self._read('sources', item_id))

    def eligible(self, item_id):
        if not self.currently_backed(item_id): return False
        found = self._read('eligibility', item_id)
        if len(found) > 1: raise FoundationError('ambiguous_learning')
        if not found: return False
        row = found[0]
        if row['content_type'] not in ('fact', 'reference') or row['category'] == 'experience':
            raise FoundationError('unsupported_historical_content_type')
        return verdict.eligible_row(row, lambda category,payload:verdict.unconfirmed_rule(
            category,payload,lambda:bool(self._read('human', item_id)),
            lambda:bool(self._read('manual', item_id))),
            lambda:False)  # summary rejected above, never a substitute rollup proof


from .native_qualification import NativeEligibilityRead


class PgEligibilityRead(NativeEligibilityRead):
    """Explicit native-v1 readiness; historical quarantine is not promoted."""
