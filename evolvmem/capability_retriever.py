"""Hybrid development-capability recall without project-summary injection."""
from __future__ import annotations

from evolvmem.capability_index import CapabilityIndex
from evolvmem.capability_store import CapabilityStore
from evolvmem.capability_recall_eligibility import recall_facts, eligible_ids
from evolvmem.request_trace import stage


CATEGORY_QUERY_TERMS = {
    "frontend": ("前端", "vue", "react", "页面", "组件"),
    "backend": ("后端", "django", "api", "接口", "队列"),
    "business_logic": ("业务逻辑", "业务规则", "业务流程", "审批", "状态流转"),
    "database": ("数据库", "mysql", "sql", "索引", "事务"),
    "architecture": ("架构", "技术选型", "模块划分", "系统设计"),
    "quality": ("测试", "报错", "故障", "性能", "调试", "修复"),
    "delivery": ("部署", "docker", "运维", "发布", "构建"),
}


def merge_ranked(lexical, vector, *, top_k: int) -> list[dict]:
    merged = {}
    for rank, item in enumerate(lexical):
        key = int(item["memory_id"])
        merged[key] = dict(item, _score=1.0 / (rank + 1))
    for rank, item in enumerate(vector):
        key = int(item["memory_id"])
        vector_score = float(item.get("vector_score", 0.0))
        if key in merged:
            merged[key]["_score"] += vector_score
        else:
            merged[key] = dict(item, _score=vector_score / (rank + 1))
    return sorted(merged.values(), key=lambda item: (-item["_score"], item["memory_id"]))[:top_k]


class CapabilityRetriever:
    def __init__(self, service):
        self.service = service

    def _index(self):
        factory = getattr(self.service, "capability_index", None)
        if callable(factory):
            return factory()
        config = getattr(self.service, "config", None)
        if config is None:
            return None
        return CapabilityIndex(
            self.service.store, config, getattr(self.service, "embedding_engine", None),
            eligible_ids=lambda ids: eligible_ids(self.service.store, ids)
        )

    @stage('capability_search')
    def search(self, query: str, categories=(), top_k: int = 10, *, project=None, module_id=None) -> list[dict]:
        query = str(query).strip()
        if not query:
            raise ValueError("invalid_query")
        from evolvmem.project_modules import ProjectModuleStore
        modules = ProjectModuleStore(self.service.store)
        if project is not None:
            if not isinstance(project, str):
                raise ValueError("invalid_project")
            modules._project(project)
        if module_id is not None:
            if not isinstance(module_id, str):
                raise ValueError("invalid_module")
            module = modules.get(module_id)
            if module["status"] != "active" or (project is not None and module["project"] != project):
                raise ValueError("invalid_module")
        top_k = max(1, min(int(top_k), 20))
        store = CapabilityStore(self.service.store)
        index = self._index()
        # Encode the vector query once. Backfill only the lexical window when
        # current-source or explicit project/module filters remove early hits.
        budget = 500
        page_size = min(100, max(20, top_k * 3))
        vector = index.search(query, categories, min(200, max(60, top_k * 6))) if index is not None else []
        lexical, facts, module_facts = [], {}, {}
        for offset in range(0, budget, page_size):
            with stage('capability_lexical'):
                page = store.search(query, categories, min(page_size, budget - offset), offset=offset)
            lexical.extend(page)
            candidates = merge_ranked(lexical, vector, top_k=len(lexical) + len(vector))
            missing = [r["memory_id"] for r in candidates if r["memory_id"] not in facts]
            if missing:
                with stage('capability_facts'):
                    facts.update(recall_facts(self.service.store, missing))
            result = []
            for row in candidates:
                ident = row["memory_id"]
                fact = facts[ident]
                if not fact["recall_eligible"]:
                    continue
                projects = fact["source_projects"]
                if project is not None and project not in projects:
                    continue
                if ident not in module_facts:
                    with stage('capability_modules'):
                        module_facts[ident] = modules.modules_for_memory(ident, allowed_sources=fact["eligible_sources"])
                current_modules = module_facts[ident]
                if module_id is not None and not any(m["module_id"] == module_id for m in current_modules):
                    continue
                row["modules"] = [{k: m[k] for k in ("module_id", "project", "name")} for m in current_modules]
                row.update(source_projects=projects, source_project=projects[0] if len(projects) == 1 else "",
                           ownership_confirmed=fact["ownership_confirmed"],
                           provenance=fact, verification_status="unverified")
                result.append(row)
                if len(result) >= top_k:
                    break
            if len(result) >= top_k or len(page) < page_size:
                return result
        return result

    @stage('capability_selected_validation')
    def validate_current_results(self, results, *, project=None, module_id=None):
        """LAN admission supplement: current bounded proofs in one read snapshot.

        This does not change standalone recall semantics. At most 20 selected
        candidates are rechecked; malformed required Context layers reject the
        response instead of borrowing authority from an older full diagnosis.
        """
        if not results:
            return []
        if len(results) > 20:
            raise ValueError('candidate_limit')
        from evolvmem.project_modules import ProjectModuleStore
        store = self.service.store
        conn = store._connection()
        if conn.in_transaction:
            raise RuntimeError('candidate_transaction_unavailable')
        conn.execute('BEGIN')
        try:
            with stage('capability_selected_facts'):
                facts = recall_facts(store, [r['memory_id'] for r in results])
            modules = ProjectModuleStore(store)
            if module_id is not None:
                module = modules.get(module_id)
                if module['status'] != 'active' or (project is not None and module['project'] != project):
                    return []
            checked, context_ids = [], set()
            for row in results:
                current = CapabilityStore(store).get(row['memory_id'])
                fact = facts.get(row['memory_id'])
                if (not current or current['status'] != 'active' or not fact or not fact['recall_eligible']
                        or any(current[k] != row[k] for k in ('category', 'title', 'content', 'fingerprint'))):
                    continue
                projects = fact['source_projects']
                if project is not None and project not in projects:
                    continue
                with stage('capability_selected_modules'):
                    current_modules = modules.modules_for_memory(row['memory_id'], allowed_sources=fact['eligible_sources'])
                if module_id is not None and not any(m['module_id'] == module_id for m in current_modules):
                    continue
                context_ids.update(s['context_item_id'] for s in fact['eligible_sources']
                                   if s.get('source_kind') == 'context' and s.get('context_item_id'))
                row = dict(row, provenance=fact, source_projects=projects,
                           source_project=projects[0] if len(projects) == 1 else '',
                           ownership_confirmed=fact['ownership_confirmed'],
                           modules=[{k: m[k] for k in ('module_id', 'project', 'name')} for m in current_modules])
                checked.append(row)
            store.validate_item_structure(sorted(context_ids))
            self._validate_current_context_sources(sorted(context_ids))
            return checked
        finally:
            conn.rollback()  # Owned read snapshot only; never a resident write.

    def _validate_current_context_sources(self, ids):
        """Selected bytes and projection truth, within the final read snapshot.

        Never call the all-library projection evaluator. Native Context items
        need no legacy projection; migrated items must retain a unique mapping.
        Limit dependency expansion rather than accepting an unaudited source.
        """
        if not ids:
            return
        if len(ids) > 500:
            raise RuntimeError('source_dependency_limit')
        import hashlib
        from evolvmem.context_migration import LegacyMemoryMigrator
        from evolvmem.cutover_checks import duplicate_active_legacy_ids
        conn = self.service.store._connection()
        marks = ','.join('?' for _ in ids)
        layers = {}
        for row in conn.execute(f'SELECT item_id,layer,content,content_hash FROM context_layers WHERE item_id IN ({marks})', ids):
            if hashlib.sha256(row['content'].encode('utf-8')).hexdigest() != row['content_hash']:
                raise RuntimeError('source_body_hash_mismatch')
            layers.setdefault(row['item_id'], {})[row['layer']] = row['content']
        items = {row['id']: dict(row) for row in conn.execute(
            f'SELECT id,identity_key,status,supersedes,superseded_by FROM context_items WHERE id IN ({marks})', ids)}
        if set(items) != set(ids):
            raise RuntimeError('source_item_missing')
        mappings = {}
        for row in conn.execute(f'SELECT legacy_memory_id,context_item_id FROM legacy_memory_migrations WHERE context_item_id IN ({marks})', ids):
            mappings.setdefault(row['context_item_id'], []).append(row['legacy_memory_id'])
        migrated = {row[0] for row in conn.execute(
            f"SELECT DISTINCT item_id FROM context_sources WHERE source_kind='migration' AND item_id IN ({marks})", ids)}
        migrator = LegacyMemoryMigrator(self.service.store, self.service.config)
        for ident in ids:
            mapped = mappings.get(ident, [])
            if not mapped and ident not in migrated:
                # Native Context is independent; a surviving same-key legacy
                # projection proves that a mapping cannot silently disappear.
                if conn.execute('SELECT 1 FROM memories WHERE key=? LIMIT 1', (items[ident]['identity_key'],)).fetchone():
                    raise RuntimeError('source_mapping_missing')
                continue
            if len(mapped) != 1:
                raise RuntimeError('source_mapping_invalid')
            raw = conn.execute('SELECT * FROM memories WHERE id=?', (mapped[0],)).fetchone()
            if raw is None:
                raise RuntimeError('source_mapping_orphan')
            legacy, item = dict(raw), items[ident]
            if migrator.identity_key_for(legacy['key'], legacy['id']) != item['identity_key']:
                raise RuntimeError('source_mapping_identity_mismatch')
            original = migrator.source_text(legacy.get('value')).replace('\r\n', '\n')
            expected = migrator.layers_for(original, migrator.content_type_for(legacy))
            actual = layers[ident]
            # The migrated L2 retains the full original, unlike clipped layers.
            if actual != {'l0': expected.l0, 'l1': expected.l1, 'l2': original}:
                raise RuntimeError('source_projection_content_mismatch')
            # Same-key peers and already mapped normalized identities are the
            # relevant duplicate-active scope; never load unrelated bodies.
            peers = [dict(row) for row in conn.execute(
                'SELECT id,key,attribute,status,updated_at FROM memories WHERE key=? OR id IN ('
                'SELECT m.legacy_memory_id FROM legacy_memory_migrations m JOIN context_items i '
                'ON i.id=m.context_item_id WHERE i.identity_key=?) LIMIT 501',
                (legacy['key'], item['identity_key']))]
            if len(peers) > 500:
                raise RuntimeError('source_dependency_limit')
            expected_status = ('candidate' if legacy['id'] in duplicate_active_legacy_ids(peers)
                               else migrator.status_for(legacy.get('status')).value)
            if item['status'] != expected_status:
                raise RuntimeError('source_projection_status_mismatch')
            for field in ('supersedes', 'superseded_by'):
                value = legacy.get(field)
                target = None
                if value is not None and value != '':
                    target = self.service.store.resolve_legacy_mapping(migrator.legacy_id_for(value))
                    if target is None:
                        raise RuntimeError('source_projection_link_missing')
                if item[field] != target:
                    raise RuntimeError('source_projection_link_mismatch')

    def infer_categories(self, query: str) -> tuple[str, ...]:
        text = str(query).casefold()
        return tuple(
            category
            for category, words in CATEGORY_QUERY_TERMS.items()
            if any(word in text for word in words)
        )

    def render(self, query: str, *, max_chars: int, max_items: int = 12) -> tuple[str, tuple[int, ...]]:
        categories = self.infer_categories(query)
        hits = self.search(query, categories=categories, top_k=max_items)
        blocks = []
        selected = []
        used = 0
        for hit in hits:
            source = "\n来源项目：" + "、".join(hit["source_projects"]) if hit["source_projects"] else "\n项目归属：待确认"
            if hit.get("modules"):
                source += "\n业务模块：" + "、".join(m["project"] + "/" + m["name"] for m in hit["modules"])
            block = f"〔{hit['category']} · 开发知识 capability:{hit['memory_id']} · 方法结果未独立验证〕\n{hit['title']}\n{hit['content']}{source}"
            if blocks and used + len(block) + 2 > max_chars:
                continue
            if len(block) > max_chars:
                continue
            blocks.append(block)
            selected.append(int(hit["memory_id"]))
            used += len(block) + (2 if len(blocks) > 1 else 0)
            if used >= max_chars:
                break
        return "\n\n".join(blocks), tuple(selected)
