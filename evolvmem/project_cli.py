"""Operator CLI for the project registry, bindings, and the review queue.

    python -m evolvmem.project_cli [--data-dir DIR] projects list
    python -m evolvmem.project_cli [--data-dir DIR] projects register <name>
    python -m evolvmem.project_cli [--data-dir DIR] projects archive <name> --expected-revision N
    python -m evolvmem.project_cli [--data-dir DIR] aliases list|add|remove ...
    python -m evolvmem.project_cli [--data-dir DIR] bindings list|bind|revoke|set-default ...
    python -m evolvmem.project_cli [--data-dir DIR] resolutions list-pending|accept|reject ...
    python -m evolvmem.project_cli [--data-dir DIR] rollup run [--project P]
    python -m evolvmem.project_cli [--data-dir DIR] fingerprint <workspace_path>
    python -m evolvmem.project_cli [--data-dir DIR] bootstrap-key

Output convention: unlike ``evolvmem.cutover_cli`` (which gates JSON behind a
``--json`` flag), every command here always prints exactly one JSON object to
stdout — except ``rollup run``, which prints one JSON line per rolled
project. Failures print ``{"error": <stable code>}`` to stderr and exit 2,
mirroring argparse's own usage-error exit code. Nothing content-bearing ever
crosses the output boundary: no memory text, no absolute paths, no key
material, no tracebacks — only stable codes, project/alias names, HMAC
fingerprints, row revisions, and rollup outcome codes. The review queue in
particular projects identifiers and decision metadata only, never evidence
payloads.

Every write runs inside one ``ContextStore.transaction()`` through
``ProjectStore``; reads open the same store and query safe columns directly.
``--data-dir`` defaults to the configured data directory; ``fingerprint`` and
``bootstrap-key`` never open the database.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys

from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.cutover_cli import _scrubbed_environment
from evolvmem.embedding import EmbeddingEngine
from evolvmem.project_store import ProjectResolutionRow, ProjectStore, ProjectStoreError
from evolvmem.workspace_identity import (
    WorkspaceIdentityError,
    WorkspaceIdentityProvider,
)

# Binding rows created from this CLI carry the same method stamp the store
# tests use for operator-driven binds.
_BIND_METHOD = "cli"


def _load_rollup_llm():
    """Reuse the configured extraction provider for manual project rollups."""
    from evolvmem.kimi_hooks import _load_llm_callable

    return _load_llm_callable(log_errors=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evolvmem.project_cli",
        description="Project registry, workspace binding, and resolution review CLI",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Config().data_dir,
        help="evolvmem data directory (default: the configured one)",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    projects = sub.add_parser("projects", help="project registry administration")
    psub = projects.add_subparsers(dest="action", required=True)
    psub.add_parser("list", help="list all projects with status and revision").set_defaults(
        handler=_cmd_projects_list
    )
    register = psub.add_parser("register", help="register a project (idempotent)")
    register.add_argument("name")
    register.set_defaults(handler=_cmd_projects_register)
    archive = psub.add_parser("archive", help="archive a project (revision CAS)")
    archive.add_argument("name")
    archive.add_argument("--expected-revision", type=int, required=True)
    archive.set_defaults(handler=_cmd_projects_archive)

    aliases = sub.add_parser("aliases", help="globally unique project aliases")
    asub = aliases.add_subparsers(dest="action", required=True)
    asub.add_parser("list", help="list aliases with revision").set_defaults(
        handler=_cmd_aliases_list
    )
    add = asub.add_parser("add", help="add an alias for a registered project")
    add.add_argument("alias")
    add.add_argument("project")
    add.set_defaults(handler=_cmd_aliases_add)
    remove = asub.add_parser("remove", help="remove an alias (revision CAS)")
    remove.add_argument("alias")
    remove.add_argument("--expected-revision", type=int, required=True)
    remove.set_defaults(handler=_cmd_aliases_remove)

    bindings = sub.add_parser("bindings", help="workspace fingerprint bindings")
    bsub = bindings.add_subparsers(dest="action", required=True)
    bsub.add_parser("list", help="list bindings with state and revision").set_defaults(
        handler=_cmd_bindings_list
    )
    bind = bsub.add_parser("bind", help="bind a workspace fingerprint to a project")
    bind.add_argument("fingerprint")
    bind.add_argument("project")
    bind.add_argument("--default", action="store_true")
    bind.set_defaults(handler=_cmd_bindings_bind)
    revoke = bsub.add_parser("revoke", help="revoke a binding (revision CAS)")
    revoke.add_argument("fingerprint")
    revoke.add_argument("project")
    revoke.add_argument("--expected-revision", type=int, required=True)
    revoke.set_defaults(handler=_cmd_bindings_revoke)
    set_default = bsub.add_parser(
        "set-default", help="make an active binding the default (revision CAS)"
    )
    set_default.add_argument("fingerprint")
    set_default.add_argument("project")
    set_default.add_argument("--expected-revision", type=int, required=True)
    set_default.set_defaults(handler=_cmd_bindings_set_default)

    resolutions = sub.add_parser("resolutions", help="resolution review queue")
    rsub = resolutions.add_subparsers(dest="action", required=True)
    pending = rsub.add_parser("list-pending", help="list pending review rows")
    pending.add_argument("--limit", type=int, default=100)
    pending.set_defaults(handler=_cmd_resolutions_list_pending)
    accept = rsub.add_parser(
        "accept", help="accept a resolution into a project (revision CAS)"
    )
    accept.add_argument("item_id", type=int)
    accept.add_argument("project")
    accept.add_argument("--expected-revision", type=int, required=True)
    accept.set_defaults(handler=_cmd_resolutions_accept)
    reject = rsub.add_parser("reject", help="reject a resolution (revision CAS)")
    reject.add_argument("item_id", type=int)
    reject.add_argument("--expected-revision", type=int, required=True)
    reject.set_defaults(handler=_cmd_resolutions_reject)

    fingerprint = sub.add_parser(
        "fingerprint", help="print a workspace path's HMAC fingerprint"
    )
    fingerprint.add_argument("workspace_path")
    fingerprint.set_defaults(handler=_cmd_fingerprint)

    rollup = sub.add_parser("rollup", help="rolling project summaries")
    rollup_sub = rollup.add_subparsers(dest="action", required=True)
    run = rollup_sub.add_parser(
        "run",
        help="refresh rolling project summaries with the configured extraction "
        "provider (one JSON line per project)",
    )
    run.add_argument("--project", default=None, help="roll up only this project")
    run.set_defaults(handler=_cmd_rollup_run)

    bootstrap = sub.add_parser(
        "bootstrap-key", help="explicitly create the owner-only workspace key"
    )
    bootstrap.set_defaults(handler=_cmd_bootstrap_key)
    return parser


# ---- output helpers: JSON only, never content or paths ----


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _emit_error(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)


# ---- store helpers ----


@contextmanager
def _open_store(args):
    config = Config(data_dir=args.data_dir)
    with ContextStore(config) as store:
        yield store


def _project_store(store: ContextStore) -> ProjectStore:
    """Fresh ProjectStore on the store's borrowed connection.

    The CLI never resolves projects, so the resolver's generic-name list is
    irrelevant here; the constructor argument is satisfied with an empty tuple.
    """
    return ProjectStore(
        store._connection(),
        store._require_transaction,
        generic_names=(),
    )


def _identity_provider(args) -> WorkspaceIdentityProvider:
    return WorkspaceIdentityProvider(key_path=args.data_dir / "workspace.key")


# ---- projects ----


def _cmd_projects_list(args) -> int:
    with _open_store(args) as store:
        rows = store._connection().execute(
            "SELECT project, status, revision FROM context_project_registry "
            "ORDER BY project"
        ).fetchall()
    _emit(
        {
            "projects": [
                {
                    "project": row["project"],
                    "status": row["status"],
                    "revision": row["revision"],
                }
                for row in rows
            ]
        }
    )
    return 0


def _cmd_projects_register(args) -> int:
    with _open_store(args) as store:
        with store.transaction():
            _project_store(store).register_project(args.name)
    _emit({"ok": True, "project": args.name})
    return 0


def _cmd_projects_archive(args) -> int:
    with _open_store(args) as store:
        with store.transaction():
            _project_store(store).archive_project(
                args.name, expected_revision=args.expected_revision
            )
    _emit({"ok": True, "project": args.name, "status": "archived"})
    return 0


# ---- aliases ----


def _cmd_aliases_list(args) -> int:
    with _open_store(args) as store:
        rows = store._connection().execute(
            "SELECT alias, project, revision FROM context_project_aliases "
            "ORDER BY alias"
        ).fetchall()
    _emit(
        {
            "aliases": [
                {
                    "alias": row["alias"],
                    "project": row["project"],
                    "revision": row["revision"],
                }
                for row in rows
            ]
        }
    )
    return 0


def _cmd_aliases_add(args) -> int:
    with _open_store(args) as store:
        with store.transaction():
            _project_store(store).add_alias(args.alias, args.project)
    _emit({"ok": True, "alias": args.alias, "project": args.project})
    return 0


def _cmd_aliases_remove(args) -> int:
    with _open_store(args) as store:
        with store.transaction():
            _project_store(store).remove_alias(
                args.alias, expected_revision=args.expected_revision
            )
    _emit({"ok": True, "alias": args.alias, "removed": True})
    return 0


# ---- bindings ----


def _cmd_bindings_list(args) -> int:
    with _open_store(args) as store:
        rows = store._connection().execute(
            "SELECT workspace_fingerprint, project, state, is_default, method,"
            " revision FROM context_project_workspace_bindings "
            "ORDER BY workspace_fingerprint, project"
        ).fetchall()
    _emit(
        {
            "bindings": [
                {
                    "workspace_fingerprint": row["workspace_fingerprint"],
                    "project": row["project"],
                    "state": row["state"],
                    "is_default": bool(row["is_default"]),
                    "method": row["method"],
                    "revision": row["revision"],
                }
                for row in rows
            ]
        }
    )
    return 0


def _cmd_bindings_bind(args) -> int:
    with _open_store(args) as store:
        with store.transaction():
            _project_store(store).bind_workspace(
                args.fingerprint,
                args.project,
                method=_BIND_METHOD,
                make_default=args.default,
            )
    _emit(
        {
            "ok": True,
            "workspace_fingerprint": args.fingerprint,
            "project": args.project,
            "is_default": bool(args.default),
        }
    )
    return 0


def _cmd_bindings_revoke(args) -> int:
    with _open_store(args) as store:
        with store.transaction():
            _project_store(store).revoke_binding(
                args.fingerprint,
                args.project,
                expected_revision=args.expected_revision,
            )
    _emit(
        {
            "ok": True,
            "workspace_fingerprint": args.fingerprint,
            "project": args.project,
            "state": "revoked",
        }
    )
    return 0


def _cmd_bindings_set_default(args) -> int:
    with _open_store(args) as store:
        with store.transaction():
            _project_store(store).set_default_binding(
                args.fingerprint,
                args.project,
                expected_revision=args.expected_revision,
            )
    _emit(
        {
            "ok": True,
            "workspace_fingerprint": args.fingerprint,
            "project": args.project,
            "is_default": True,
        }
    )
    return 0


# ---- resolutions review queue ----


def _resolution_payload(row: ProjectResolutionRow) -> dict:
    """Review metadata projection: identifiers, states, and revision only."""
    return {
        "item_id": row.item_id,
        "resolution_state": row.resolution_state,
        "review_state": row.review_state,
        "proposed_project": row.proposed_project,
        "resolved_project": row.resolved_project,
        "confidence": row.confidence,
        "method": row.method,
        "revision": row.revision,
    }


def _cmd_resolutions_list_pending(args) -> int:
    with _open_store(args) as store:
        rows = _project_store(store).list_pending_resolutions(limit=args.limit)
    _emit({"resolutions": [_resolution_payload(row) for row in rows]})
    return 0


def _cmd_resolutions_accept(args) -> int:
    with _open_store(args) as store:
        with store.transaction():
            _project_store(store).accept_resolution(
                args.item_id, args.project, expected_revision=args.expected_revision
            )
    _emit({"ok": True, "item_id": args.item_id, "resolved_project": args.project})
    return 0


def _cmd_resolutions_reject(args) -> int:
    with _open_store(args) as store:
        with store.transaction():
            _project_store(store).reject_resolution(
                args.item_id, expected_revision=args.expected_revision
            )
    _emit({"ok": True, "item_id": args.item_id, "review_state": "rejected"})
    return 0


# ---- rollup ----


def _cmd_rollup_run(args) -> int:
    """Print one JSON line per project: project/status/reason/context_id.

    The CLI never carries content across its output boundary. The generator
    uses the configured extraction provider through the shared narrow LLM
    adapter; unavailable credentials preserve the explicit
    ``llm_unavailable`` degradation.
    """
    if args.project is not None and not args.project.strip():
        _emit_error({"error": "invalid_project"})
        return 2
    config = Config(data_dir=args.data_dir)
    llm = _load_rollup_llm()
    service = ContextService(
        config,
        embedding_engine=EmbeddingEngine(config) if llm is not None else None,
    )
    try:
        service.initialize(mode=ContextMode.SHADOW, adapter="project_cli")
        if args.project is not None:
            reports = (service.rollup_project(args.project.strip(), llm=llm),)
        else:
            reports = service.rollup_projects(llm=llm)
    finally:
        service.close()
    for report in reports:
        _emit(
            {
                "project": report.project,
                "status": report.status,
                "reason": report.reason,
                "context_id": report.context_id,
            }
        )
    return 0


# ---- workspace identity helpers ----


def _cmd_fingerprint(args) -> int:
    identity = _identity_provider(args).resolve(args.workspace_path)
    _emit({"fingerprint": identity.fingerprint, "kind": identity.kind})
    return 0


def _cmd_bootstrap_key(args) -> int:
    status = _identity_provider(args).bootstrap_key()
    _emit({"state": status.state})
    return 0 if status.state == "ready" else 2


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # An explicit --data-dir must always win over EVOLVMEM_* process overrides
    # (Config.__post_init__ would otherwise clobber the constructor argument).
    with _scrubbed_environment():
        try:
            return args.handler(args)
        except WorkspaceIdentityError as exc:
            payload: dict[str, object] = {"error": exc.code}
            if exc.code == "workspace_key_missing":
                payload["hint"] = "run bootstrap-key first"
            _emit_error(payload)
            return 2
        except ProjectStoreError as exc:
            _emit_error({"error": exc.code})
            return 2


if __name__ == "__main__":
    sys.exit(main())
