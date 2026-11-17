"""AST-based production write-boundary regression guard.

Parses production adapter modules and fails on new raw-store write bypasses:

1. ``instantiate`` — instantiating ``MemoryStore`` outside the allowlisted
   core (memory_store/legacy_projection/context_migration own the SQL;
   isolated and migration tests are not scanned).
2. ``private_access`` — touching ``._conn``/``._execute``.
3. ``raw_sql`` — string literals executing ``UPDATE memories`` or
   ``DELETE FROM memories``.
4. ``raw_mutation`` — calling add/replace/remove/archive/update_metadata/
   update_access/transaction/hard_delete on a variable that the same module
   assigned from ``MemoryStore(...)`` (no exemptions, ever).

Exemptions are function-scoped and document known residuals only:
the migration utility may bootstrap the legacy schema (the plan allows
MemoryStore in migration utilities).
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SCANNED_MODULES = (
    "evolvmem/mcp_server.py",
    "evolvmem/hooks.py",
    "evolvmem/kimi_hooks.py",
    "evolvmem/dsh_bridge.py",
    "evolvmem/web_server.py",
    "evolvmem/retriever.py",
    "evolvmem/forgetting.py",
    "evolvmem/consolidator.py",
    "scripts/extract_stale_sessions.py",
    "migrate_claude_mem.py",
)

_MUTATION_METHODS = frozenset({
    "add", "add_if_changed", "replace", "remove", "archive",
    "update_metadata", "update_access", "transaction", "hard_delete",
})

_RAW_SQL_RE = re.compile(
    r"\b(?:UPDATE\s+memories|DELETE\s+FROM\s+memories)\b", re.IGNORECASE
)

_PRIVATE_ATTRS = frozenset({"_conn", "_execute"})

# Function-scoped residuals; each entry must name its removal condition.
_EXEMPTIONS = {
    # 迁移工具获准使用 MemoryStore 建 legacy schema（不进行任何 mutation 调用）
    ("migrate_claude_mem.py", "instantiate"): {"_ensure_legacy_schema"},
}


def _target_name(node) -> str | None:
    """Dotted name of an assignment/call receiver ('self.store', 'store')."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _is_memorystore_call(node) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "MemoryStore"
    if isinstance(func, ast.Attribute):
        return func.attr == "MemoryStore"
    return False


def _traced_store_names(tree) -> set[str]:
    """Names bound to raw MemoryStore instances within the module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_memorystore_call(node.value):
            for target in node.targets:
                name = _target_name(target)
                if name:
                    names.add(name)
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_memorystore_call(node.value)
        ):
            name = _target_name(node.target)
            if name:
                names.add(name)
        elif isinstance(node, ast.With):
            for item in node.items:
                if item.optional_vars is not None and _is_memorystore_call(
                    item.context_expr
                ):
                    name = _target_name(item.optional_vars)
                    if name:
                        names.add(name)
    return names


def _walk_scoped(node, scope=()):
    """Yield (enclosing qualname, node); qualname '' means module level."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            yield from _walk_scoped(child, scope + (child.name,))
        else:
            yield ".".join(scope), child
            yield from _walk_scoped(child, scope)


def _exempted(rel: str, rule: str, qualname: str) -> bool:
    return qualname in _EXEMPTIONS.get((rel, rule), set())


def _violations(source: str, rel: str) -> list[str]:
    """Boundary violations in one module source, as human-readable strings."""
    tree = ast.parse(source, filename=rel)
    traced = _traced_store_names(tree)
    findings: list[str] = []
    for qualname, node in _walk_scoped(tree):
        where = qualname or "<module>"
        if isinstance(node, ast.Call) and _is_memorystore_call(node):
            if not _exempted(rel, "instantiate", qualname):
                findings.append(
                    f"{rel}:{node.lineno} {where} instantiates MemoryStore"
                )
        elif isinstance(node, ast.Attribute) and node.attr in _PRIVATE_ATTRS:
            if not _exempted(rel, "private_access", qualname):
                findings.append(
                    f"{rel}:{node.lineno} {where} accesses .{node.attr}"
                )
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _RAW_SQL_RE.search(node.value)
        ):
            if not _exempted(rel, "raw_sql", qualname):
                findings.append(
                    f"{rel}:{node.lineno} {where} holds UPDATE/DELETE "
                    f"memories SQL"
                )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATION_METHODS
            and _target_name(node.func.value) in traced
        ):
            findings.append(
                f"{rel}:{node.lineno} {where} calls raw store mutation "
                f"{node.func.attr}()"
            )
    return findings


def test_scan_scope_covers_every_adapter():
    """The scan list is frozen; shrinking it fails loudly."""
    assert set(_SCANNED_MODULES) == {
        "evolvmem/mcp_server.py",
        "evolvmem/hooks.py",
        "evolvmem/kimi_hooks.py",
        "evolvmem/dsh_bridge.py",
        "evolvmem/web_server.py",
        "evolvmem/retriever.py",
        "evolvmem/forgetting.py",
        "evolvmem/consolidator.py",
        "scripts/extract_stale_sessions.py",
        "migrate_claude_mem.py",
    }


def test_production_modules_have_no_raw_write_bypasses():
    failures = []
    for rel in _SCANNED_MODULES:
        path = ROOT / rel
        assert path.exists(), f"scanned module missing: {rel}"
        failures.extend(_violations(path.read_text(encoding="utf-8"), rel))
    assert not failures, "raw legacy write bypasses:\n" + "\n".join(failures)


def test_maintenance_never_triggers_hard_delete():
    """Forgetting/consolidation must never reach the irreversible delete."""
    for rel in ("evolvmem/forgetting.py", "evolvmem/consolidator.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "hard_delete"
        ]
        assert not offenders, f"{rel} references hard_delete at {offenders}"


class TestGuardSelfCheck:
    """The guard proves it catches each violation class on synthetic sources."""

    def test_catches_memorystore_instantiation(self):
        src = "def f():\n    store = MemoryStore(config)\n"
        assert _violations(src, "evolvmem/hooks.py") == [
            "evolvmem/hooks.py:2 f instantiates MemoryStore"
        ]

    def test_catches_private_conn_and_execute(self):
        src = (
            "def f(store):\n"
            "    store._execute('SELECT 1')\n"
            "    store._conn.commit()\n"
        )
        findings = _violations(src, "evolvmem/retriever.py")
        assert any("._execute" in f for f in findings)
        assert any("._conn" in f for f in findings)

    def test_catches_update_and_delete_memories_sql(self):
        for sql in ("UPDATE memories SET status='archived'",
                    "DELETE FROM memories WHERE id=1"):
            src = f'SQL = "{sql}"\n'
            assert _violations(src, "evolvmem/forgetting.py"), sql

    def test_catches_mutation_calls_on_raw_store(self):
        src = (
            "store = MemoryStore(config)\n"
            "store.archive(1)\n"
            "store.update_metadata(1, importance=5.0)\n"
        )
        findings = _violations(src, "evolvmem/hooks.py")
        assert any("archive()" in f for f in findings)
        assert any("update_metadata()" in f for f in findings)

    def test_catches_with_statement_raw_store(self):
        src = (
            "def f(config):\n"
            "    with MemoryStore(config) as store:\n"
            "        store.remove(1)\n"
        )
        assert any("remove()" in f
                   for f in _violations(src, "evolvmem/hooks.py"))

    def test_allows_facade_and_untraced_calls(self):
        src = (
            "def work(facade, vidx):\n"
            "    facade.archive(1)\n"
            "    facade.update_access(1)\n"
            "    facade.update_metadata(1, importance=5.0)\n"
            "    vidx.add(1, [0.0])\n"
        )
        assert _violations(src, "evolvmem/consolidator.py") == []

    def test_exemptions_stay_function_scoped(self):
        # 同一个文件里，豁免函数之外的 MemoryStore 实例化仍然违规
        src = "def helper(config):\n    store = MemoryStore(config)\n"
        assert _violations(src, "migrate_claude_mem.py")
        src_ok = "def _ensure_legacy_schema(config):\n    store = MemoryStore(config)\n"
        assert _violations(src_ok, "migrate_claude_mem.py") == []
