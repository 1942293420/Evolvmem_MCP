"""Native synthetic SQLite/search regression for bounded vector-window reads.

Uses canonical evolvmem imports. ANN ranking/embedding are explicit test ports;
SQL, get, scoped snapshots and vector proofs execute actual implementation.
The small literal pre-batch reference checks behavior independently, without
loading an artifact or rewriting/importing source at runtime.
"""
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import patch

import numpy as np
from evolvmem.capability_store import CapabilityStore
from evolvmem.capability_index import CapabilityIndex, _Incompatible
from evolvmem.capability_provenance import provenance_snapshot
from evolvmem.vector_provenance import entry, contract
from evolvmem.capability_models import CAPABILITY_SCHEMA
from evolvmem.request_trace import stage


class _PerIdReference(CapabilityIndex):
    # Literal search body from 6ad52c, before window batching. It uses the
    # real get/proof/owned-snapshot methods, not a success-stubbed oracle.
    # No baseline file, AST parser, workspace path or private data is loaded.
    def search(self, query: str, categories=(), top_k: int = 10) -> list[dict]:
        if self.embedding_engine is None or not getattr(self.embedding_engine, "is_loaded", False):
            return []
        if self.context_store._connection().in_transaction or self.context_store._transaction_depth:
            return []
        try:
            encoded_contract = contract(self.config)
            with stage('capability_embedding'):
                embedding = np.asarray(self.embedding_engine.encode_query(query), dtype=np.float32)
            if encoded_contract != contract(self.config):
                raise _Incompatible('embedding_contract_changed')
            with self._snapshot() as (vectors, proof, _):
                limit = max(1, int(top_k))
                matches = vectors.search(embedding, k=limit * 4)
                # A coherent read snapshot, not a promise that no later write
                # can occur. LAN selected-result revalidation remains in place.
                with self._read_snapshot():
                    store = CapabilityStore(self.context_store)
                    categories = set(categories)
                    results = []
                    # Keep the same ranked candidate budget and answer. Deep
                    # source proofs are needed only until enough valid hits
                    # survive. All windows share this owned read snapshot.
                    window = min(20, limit)
                    for offset in range(0, len(matches), window):
                        candidates = []
                        for match in matches[offset:offset + window]:
                            ident = int(match["id"])
                            memory = store.get(ident)
                            if memory is None or memory["status"] != "active":
                                continue
                            # Uncertified legacy vectors retain the existing
                            # backfill behavior; mismatching proofs never pass.
                            if (self.eligible is None and categories
                                    and memory["category"] not in categories):
                                continue
                            if (self.eligible is None and str(ident) in proof["items"]
                                    and not self._proof_matches(ident, memory, proof["items"], vectors=vectors)):
                                continue
                            candidates.append((match, memory))
                        with stage('capability_vector_eligibility'):
                            allowed = (set(self.eligible_ids([row["memory_id"] for _, row in candidates]))
                                       if candidates and self.eligible_ids is not None else set())
                        for match, memory in candidates:
                            if not self._eligible(memory, allowed=allowed):
                                continue
                            # Custom predicates historically receive mutable
                            # rows. Preserve category/proof checks after them.
                            if self.eligible is not None:
                                if categories and memory["category"] not in categories:
                                    continue
                                ident = int(match["id"])
                                if (str(ident) in proof["items"] and not self._proof_matches(
                                        ident, memory, proof["items"], vectors=vectors)):
                                    continue
                            memory["vector_score"] = 1.0 - float(match["distance"]) / 2.0
                            results.append(memory)
                            if len(results) >= limit:
                                break
                        if len(results) >= limit:
                            break
                if encoded_contract != contract(self.config):
                    raise _Incompatible('embedding_contract_changed')
                return results
        except Exception:
            return []



class WindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'synthetic.db'
        self.conn=sqlite3.connect(self.path);self.conn.row_factory=sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('''
          CREATE TABLE context_project_registry(project TEXT,status TEXT);
          CREATE TABLE capability_memories(memory_id INTEGER PRIMARY KEY,status TEXT,category TEXT,
             title TEXT,content TEXT,keywords TEXT,score REAL,extra TEXT);
          CREATE TABLE capability_sources(source_id INTEGER PRIMARY KEY,memory_id INTEGER,projects TEXT,
             source_ref TEXT,extra TEXT);
          CREATE INDEX sources_memory ON capability_sources(memory_id);
        ''')
        self.conn.executemany('INSERT INTO capability_memories VALUES(?,?,?,?,?,?,?,?)',
            [(i,'active','frontend','title'+str(i),'body'+str(i),'["key"]',3.5,'retained') for i in range(1,46)])
        self.conn.executemany('INSERT INTO capability_sources VALUES(?,?,?,?,?)',
            [(i,i,'["p2","p1"]','ref'+str(i),'retained') for i in range(1,46)])
        self.conn.commit();self.counts=Counter();self.conn.set_trace_callback(self.count)
        self.store=SimpleNamespace(_connection=lambda:self.conn,_transaction_depth=0)
        self.config=SimpleNamespace(embedding_model_filename='synthetic',embedding_dim=2,embedding_doc_prefix='')

    def tearDown(self):self.conn.close();self.tmp.cleanup()

    def count(self,sql):
        s=' '.join(sql.lower().split())
        if s.startswith('select') and 'from capability_memories' in s:self.counts['memory']+=1
        if s.startswith('select') and 'from capability_sources' in s:self.counts['sources']+=1

    def index(self,ids,*,baseline=False,allowed=None,custom=None,proof_ids=()):
        vector=np.asarray([1,0],dtype=np.float32)
        proof={'items':{str(i):entry('title'+str(i)+'\nbody'+str(i),vector) for i in proof_ids}}
        calls=[]
        class RankedPort:
            def search(inner,embedding,k):
                calls.append(('ann',k));return [{'id':i,'distance':pos/100} for pos,i in enumerate(ids[:k])]
            def vector_copy(inner,ident):calls.append(('proof',ident));return vector.copy()
        vectors=RankedPort()
        @contextmanager
        def frozen():yield vectors,proof,None
        value=(_PerIdReference if baseline else CapabilityIndex).__new__(_PerIdReference if baseline else CapabilityIndex)
        value.context_store=self.store;value.config=self.config;value.eligible=custom
        def eligible_ids(values):
            calls.append(('eligibility',tuple(values)));return set(values) if allowed is None else set(values)&set(allowed)
        value.eligible_ids=eligible_ids
        value.embedding_engine=SimpleNamespace(is_loaded=True,encode_query=lambda q:vector.copy())
        value._snapshot=frozen
        return value,calls,proof

    def helper(self,ids):
        self.assertTrue(hasattr(CapabilityStore,'_get_vector_window'),'bounded window reader missing')
        return CapabilityStore(self.store)._get_vector_window(ids)

    def test_same_window_uses_two_sql_queries_and_exact_dto(self):
        oldindex,oldcalls,_=self.index(range(1,21),baseline=True)
        expected=oldindex.search('synthetic',top_k=20);self.counts.clear()
        index,calls,_=self.index(range(1,21))
        self.assertEqual(expected,index.search('synthetic',top_k=20));self.assertEqual(oldcalls,calls)
        self.assertEqual({'memory':1,'sources':1},dict(self.counts))

    def test_store_window_preserves_order_missing_duplicates_and_json(self):
        ids=[4,1,999,4,2]
        with provenance_snapshot(self.store):
            expected=[CapabilityStore(self.store).get(i) for i in ids];self.counts.clear()
            actual=self.helper(ids)
        self.assertEqual(expected,actual);self.assertEqual({'memory':1,'sources':1},dict(self.counts))
        self.assertIsNot(actual[0],actual[3]);self.assertIsNot(actual[0]['keywords'],actual[3]['keywords'])
        self.assertIsNot(actual[0]['source_projects'],actual[3]['source_projects'])
        self.assertNotIn('score',actual[0])

    def test_custom_mutating_callback_stays_on_original_get_path(self):
        def custom(row):row['category']='backend';return row['memory_id']%2==0
        oldindex,_,_=self.index(range(1,10),baseline=True,custom=custom)
        expected=oldindex.search('synthetic',categories=['backend'],top_k=3)
        index,_,_=self.index(range(1,10),custom=custom);self.counts.clear()
        if hasattr(CapabilityStore,'_get_vector_window'):
            with patch.object(CapabilityStore,'_get_vector_window',side_effect=AssertionError('custom must use old get')):
                actual=index.search('synthetic',categories=['backend'],top_k=3)
        else:actual=index.search('synthetic',categories=['backend'],top_k=3)
        self.assertEqual(expected,actual);self.assertEqual(6,self.counts['memory'])

    def test_category_status_and_bad_vector_proof_filter_before_same_eligibility_batch(self):
        self.conn.execute("UPDATE capability_memories SET category='backend' WHERE memory_id=1")
        self.conn.execute("UPDATE capability_memories SET status='archived' WHERE memory_id=2");self.conn.commit()
        traces=[]
        for baseline in (True,False):
            index,calls,proof=self.index([1,2,3,4,5],baseline=baseline,allowed=[4,5],proof_ids=[3,4])
            proof['items']['3']['text']='wrong'
            traces.append((index.search('synthetic',categories=['frontend'],top_k=3),calls))
        self.assertEqual(*traces);self.assertEqual([4,5],[r['memory_id'] for r in traces[1][0]])

    def test_window_budget_topk_stops_before_later_corrupt_window(self):
        self.conn.execute("UPDATE capability_sources SET projects='broken' WHERE memory_id=21");self.conn.commit()
        index,calls,_=self.index(range(1,46));rows=index.search('synthetic',top_k=20)
        self.assertEqual(list(range(1,21)),[r['memory_id'] for r in rows]);self.assertEqual(('ann',80),calls[0])
        self.assertEqual(1,self.counts['memory']);self.assertEqual(1,self.counts['sources'])

    def test_rejected_first_window_backfills_same_ranked_candidates(self):
        outcomes=[]
        for baseline in (True,False):
            index,calls,_=self.index(range(1,46),baseline=baseline,allowed=range(21,46))
            outcomes.append((index.search('synthetic',top_k=20),calls))
        self.assertEqual(*outcomes);self.assertEqual(list(range(21,41)),[r['memory_id'] for r in outcomes[1][0]])

    def test_corrupt_json_even_on_category_excluded_memory_fails_closed(self):
        for table,column in [('capability_memories','keywords'),('capability_sources','projects')]:
            with self.subTest(table=table):
                self.conn.execute('SAVEPOINT synthetic')
                self.conn.execute("UPDATE capability_memories SET category='backend' WHERE memory_id=2")
                self.conn.execute('UPDATE '+table+' SET '+column+"='broken' WHERE memory_id=2");self.conn.execute('RELEASE synthetic')
                for baseline in (True,False):
                    index,_,_=self.index([1,2],baseline=baseline)
                    self.assertEqual([],index.search('synthetic',categories=['frontend'],top_k=2))
                self.conn.execute("UPDATE capability_memories SET category='frontend',keywords='[]'")
                self.conn.execute("UPDATE capability_sources SET projects='[]'");self.conn.commit()

    def test_bad_text_in_unused_source_column_is_not_projected_away(self):
        self.conn.execute("UPDATE capability_sources SET extra=CAST(x'80' AS TEXT) WHERE memory_id=2");self.conn.commit()
        for baseline in (True,False):
            index,_,_=self.index([1,2],baseline=baseline)
            self.assertEqual([],index.search('synthetic',top_k=2))

    def test_missing_memory_does_not_read_orphan_source(self):
        self.conn.execute("INSERT INTO capability_sources VALUES(999,999,'broken','orphan','')");self.conn.commit()
        with provenance_snapshot(self.store):
            self.assertEqual([None],self.helper([999]))

    def test_helper_rejects_unbounded_or_noninteger_ids_and_unowned_reads(self):
        self.assertTrue(hasattr(CapabilityStore,'_get_vector_window'),'bounded window reader missing')
        with self.assertRaises(ValueError):self.helper([1])
        with provenance_snapshot(self.store):
            for ids in (list(range(21)),[True],['1']):
                with self.subTest(ids=ids),self.assertRaises(ValueError):self.helper(ids)
            self.assertEqual([],self.helper([]))

    def test_fresh_search_does_not_reuse_previous_window_after_commit(self):
        index,_,_=self.index([1]);self.assertEqual(1,len(index.search('synthetic',top_k=1)))
        self.conn.execute("UPDATE capability_memories SET status='archived' WHERE memory_id=1");self.conn.commit()
        self.assertEqual([],index.search('synthetic',top_k=1))
        self.assertFalse(self.conn.in_transaction);self.assertEqual(0,self.conn.execute('PRAGMA query_only').fetchone()[0])

    def test_custom_text_decoder_keeps_original_per_id_read_schedule(self):
        def decode(raw):return raw.decode('utf8')
        with provenance_snapshot(self.store):
            self.conn.text_factory=decode
            expected=[CapabilityStore(self.store).get(i) for i in [4,1,3]];self.counts.clear()
            actual=self.helper([4,1,3])
            self.assertEqual(expected,actual)
            self.assertEqual({'memory':3,'sources':3},dict(self.counts))
            self.conn.text_factory=str

    def test_source_rows_stream_without_holding_window_source_bodies(self):
        self.conn.executemany('INSERT INTO capability_sources VALUES(?,?,?,?,?)',
            [(1000+i,1,'["p"]','r','bounded synthetic extra') for i in range(2000)])
        self.conn.commit();live=weakref.WeakSet();peak=[0]
        class Observed(dict):
            __hash__=object.__hash__
        def observe(cursor,values):
            if cursor.description[0][0]=='source_id':
                row=Observed(zip((x[0] for x in cursor.description),values));live.add(row)
                peak[0]=max(peak[0],len(live));return row
            return sqlite3.Row(cursor,values)
        with provenance_snapshot(self.store):
            self.conn.row_factory=observe
            actual=self.helper([1,2])
            self.conn.row_factory=sqlite3.Row
        self.assertEqual(2001,actual[0]['sources_count']);self.assertLessEqual(peak[0],2)
        self.assertFalse(live)

    def test_rejected_window_dtos_are_released_before_next_window_read(self):
        live=weakref.WeakSet();at_entry=[];original=CapabilityStore._get_vector_window
        class Observed(dict):
            __hash__=object.__hash__
        def observed(store,ids):
            at_entry.append(len(live))
            rows=original(store,ids);tracked=[]
            for row in rows:
                value=Observed(row) if row is not None else None
                if value is not None:live.add(value)
                tracked.append(value)
            return tracked
        index,calls,_=self.index(range(1,41),allowed=range(21,41))
        expected,expected_calls,_=self.index(range(1,41),baseline=True,allowed=range(21,41))
        want=expected.search('synthetic',top_k=20)
        with patch.object(CapabilityStore,'_get_vector_window',observed):
            actual=index.search('synthetic',top_k=20)
        self.assertEqual(want,actual);self.assertEqual(expected_calls,calls)
        self.assertEqual([21+i for i in range(20)],[r['memory_id'] for r in actual])
        self.assertEqual(2,len(at_entry));self.assertEqual(0,at_entry[0])
        self.assertLessEqual(at_entry[1],1,'previous rejected window remains held during the next SQL read')

    def test_bad_memory_text_is_not_hidden_by_inactive_filter(self):
        self.conn.execute("UPDATE capability_memories SET status='archived',extra=CAST(x'80' AS TEXT) WHERE memory_id=2");self.conn.commit()
        for baseline in (True,False):
            index,_,_=self.index([1,2],baseline=baseline)
            self.assertEqual([],index.search('synthetic',top_k=2))

    def test_project_claim_shape_and_empty_sources_match_original_get(self):
        self.conn.execute("DELETE FROM capability_sources WHERE memory_id=2")
        self.conn.execute("UPDATE capability_sources SET projects=? WHERE memory_id=1",('{"alpha": 1, "beta": 2}',))
        self.conn.execute("INSERT INTO capability_sources VALUES(99,1,'[\"beta\",\"gamma\"]','r','')");self.conn.commit()
        with provenance_snapshot(self.store):
            expected=[CapabilityStore(self.store).get(i) for i in [1,2]]
            self.assertEqual(expected,self.helper([1,2]))

    def test_batch_helper_refuses_borrowed_query_only_transaction(self):
        self.conn.execute('PRAGMA query_only=ON');self.conn.execute('BEGIN')
        try:
            with self.assertRaises(ValueError):self.helper([1])
            self.assertTrue(self.conn.in_transaction)
        finally:self.conn.rollback();self.conn.execute('PRAGMA query_only=OFF')

    def test_all_real_source_text_columns_fail_closed_and_restore_snapshot_state(self):
        connection=sqlite3.connect(':memory:');connection.row_factory=sqlite3.Row
        original_store=self.store
        try:
            connection.execute('CREATE TABLE context_project_registry(project TEXT,status TEXT)')
            for ddl in CAPABILITY_SCHEMA[:3]:connection.execute(ddl)
            connection.execute("INSERT INTO capability_memories(memory_id,category,title,content,fingerprint,created_at,updated_at) VALUES(1,'frontend','title1','body1','unique','old','old')")
            source=dict(source_id=1,memory_id=1,source_kind='manual',source_key='synthetic',source_ref='synthetic',
                source_revision='synthetic',unit_digest='',projects='["project"]',knowledge_scope='project',
                evidence_status='unverified',created_at='old',updated_at='old')
            connection.execute('INSERT INTO capability_sources('+','.join(source)+') VALUES('+','.join('?' for _ in source)+')',tuple(source.values()))
            connection.commit()
            self.store=SimpleNamespace(_connection=lambda:connection,_transaction_depth=0)
            columns=[r['name'] for r in connection.execute('PRAGMA table_info(capability_sources)') if r['type']=='TEXT']
            self.assertEqual(10,len(columns))
            for column in columns:
                for previous_query_only in (0,1):
                    with self.subTest(column=column,previous_query_only=previous_query_only):
                        # Corruption is created ONLY in this synthetic SQLite
                        # file. Actual read uses normal constraints/str decoder.
                        connection.execute('PRAGMA ignore_check_constraints=ON')
                        connection.execute('UPDATE capability_sources SET '+column+"=CAST(x'80' AS TEXT)")
                        connection.commit();connection.execute('PRAGMA ignore_check_constraints=OFF')
                        connection.execute('PRAGMA query_only='+str(previous_query_only))
                        for baseline in (True,False):
                            index,_,_=self.index([1],baseline=baseline)
                            self.assertEqual([],index.search('synthetic',top_k=1))
                            self.assertFalse(connection.in_transaction)
                            self.assertEqual(previous_query_only,connection.execute('PRAGMA query_only').fetchone()[0])
                        connection.execute('PRAGMA query_only=OFF')
                        connection.execute('UPDATE capability_sources SET '+column+'=?',(source[column],));connection.commit()
            index,_,_=self.index([1]);self.assertEqual(1,len(index.search('synthetic',top_k=1)))
        finally:self.store=original_store;connection.close()

    def test_json_failure_restores_snapshot_and_next_search_is_fresh(self):
        for previous_query_only in (0,1):
            self.conn.execute("UPDATE capability_sources SET projects='broken' WHERE memory_id=1");self.conn.commit()
            self.conn.execute('PRAGMA query_only='+str(previous_query_only))
            index,_,_=self.index([1])
            self.assertEqual([],index.search('synthetic',top_k=1))
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual(previous_query_only,self.conn.execute('PRAGMA query_only').fetchone()[0])
            self.conn.execute('PRAGMA query_only=OFF')
            self.conn.execute("UPDATE capability_sources SET projects='[]' WHERE memory_id=1");self.conn.commit()
            self.assertEqual(1,len(index.search('synthetic',top_k=1)))


if __name__=='__main__':unittest.main(verbosity=2)
