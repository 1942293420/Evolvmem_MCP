"""Read-only project directory and paged history for the Web workbench.

Registry membership, archive availability and event counts are separate facts.
These readers do not change MCP recall eligibility or create missing archives.
"""
from __future__ import annotations

from evolvmem.project_archive_store import ProjectArchiveStore


def page_number(value, name, *, maximum=None):
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        raise ValueError("invalid_" + name) from None
    number = max(1, number)
    return min(number, maximum) if maximum else number


def literal_pattern(value):
    return "%" + str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def project_archives(service, params=None):
    params = params or {}
    status = str(params.get("status", "active"))
    availability = str(params.get("archive", "all"))
    sort = str(params.get("sort", "recent"))
    if status not in {"active", "archived", "all"}:
        raise ValueError("invalid_project_status")
    if availability not in {"all", "available", "missing"}:
        raise ValueError("invalid_archive_filter")
    if sort not in {"recent", "name"}:
        raise ValueError("invalid_project_sort")
    requested = page_number(params.get("page", 1), "page")
    size = page_number(params.get("page_size", 20), "page_size", maximum=100)
    conn = service.store._connection()
    stats = dict(conn.execute(
        "SELECT count(*) AS registered,"
        "coalesce(sum(r.status='active'),0) AS active,"
        "coalesce(sum(r.status='archived'),0) AS archived,"
        "count(a.project) AS with_archive,count(*)-count(a.project) AS without_archive,"
        "coalesce(sum(r.status='active' AND a.project IS NOT NULL),0) AS active_with_archive,"
        "coalesce(sum(r.status='active' AND a.project IS NULL),0) AS active_without_archive "
        "FROM context_project_registry r LEFT JOIN project_archives a ON a.project=r.project"
    ).fetchone())
    clauses, values = [], []
    if status != "all":
        clauses.append("r.status=?")
        values.append(status)
    if availability != "all":
        clauses.append("a.project IS " + ("NOT NULL" if availability == "available" else "NULL"))
    query = str(params.get("q", "")).strip()
    if query:
        clauses.append("(r.project LIKE ? ESCAPE '\\' OR r.display_name LIKE ? ESCAPE '\\' OR "
                       "EXISTS(SELECT 1 FROM context_project_aliases x WHERE x.project=r.project AND x.alias LIKE ? ESCAPE '\\'))")
        values.extend([literal_pattern(query)] * 3)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    tables = " FROM context_project_registry r LEFT JOIN project_archives a ON a.project=r.project"
    total = conn.execute("SELECT count(*)" + tables + where, values).fetchone()[0]
    pages = max(1, (total + size - 1) // size)
    page = min(requested, pages)
    order = ("coalesce(nullif(r.display_name,''),r.project) COLLATE NOCASE,r.project" if sort == "name" else
             "coalesce(a.updated_at,r.updated_at) DESC,r.project")
    rows = conn.execute(
        "SELECT r.project,r.display_name,r.status,a.status AS archive_status,"
        "a.project IS NOT NULL AS has_archive,coalesce(a.updated_at,r.updated_at) AS updated_at,"
        "EXISTS(SELECT 1 FROM project_memory_documents d WHERE d.project=r.project) AS has_legacy_document"
        + tables + where + " ORDER BY " + order + " LIMIT ? OFFSET ?",
        [*values, size, (page - 1) * size],
    ).fetchall()
    aliases = {}
    if rows:
        marks = ",".join("?" for _ in rows)
        for row in conn.execute("SELECT project,alias FROM context_project_aliases WHERE project IN (" + marks + ") ORDER BY alias", [r["project"] for r in rows]):
            aliases.setdefault(row["project"], []).append(row["alias"])
    current_counts, historical_counts = {}, {}
    if rows:
        from evolvmem.project_event_provenance import annotate
        marks = ",".join("?" for _ in rows)
        event_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM project_events WHERE project IN (" + marks + ")", [r["project"] for r in rows])]
        support = annotate(service.store, event_rows)
        for event in event_rows:
            key = event["project"]
            historical_counts[key] = historical_counts.get(key, 0) + 1
            current_counts[key] = current_counts.get(key, 0) + int(support[event["event_id"]]["current_support"])
    items = []
    for row in rows:
        item = dict(row)
        item["display_name"] = item["display_name"] or item["project"]
        item["has_archive"] = bool(item["has_archive"])
        item["has_legacy_document"] = bool(item["has_legacy_document"])
        item["aliases"] = aliases.get(item["project"], [])
        item["event_count"] = current_counts.get(item["project"], 0)
        item["historical_event_count"] = historical_counts.get(item["project"], 0)
        item["unsupported_event_count"] = item["historical_event_count"] - item["event_count"]
        items.append(item)
    return dict(items=items, total=total, page=page, page_size=size, pages=pages, stats=stats)


def _registered_project(conn, name):
    value = str(name).strip()
    # A directory link is an exact canonical ID, even when another project has
    # that display name. Web browsing includes archived registry entries.
    exact = conn.execute("SELECT * FROM context_project_registry WHERE project=?", (value,)).fetchone()
    if exact is not None:
        return exact
    rows = conn.execute(
        "SELECT r.* FROM context_project_registry r WHERE lower(r.project)=lower(?) "
        "OR lower(r.display_name)=lower(?) OR EXISTS(SELECT 1 FROM context_project_aliases a "
        "WHERE a.project=r.project AND lower(a.alias)=lower(?))", (value, value, value)
    ).fetchall() if value else []
    if len(rows) != 1:
        raise ValueError("project_not_registered" if not rows else "project_ambiguous")
    return rows[0]


def project_archive(service, project, params=None):
    params = params or {}
    requested = page_number(params.get("events_page", 1), "events_page")
    size = page_number(params.get("events_page_size", 100), "events_page_size", maximum=100)
    conn = service.store._connection()
    registry = _registered_project(conn, project)
    project = registry["project"]
    archive = ProjectArchiveStore(service.store).get(project)
    if archive is None:
        result = dict(project=project, display_name=registry["display_name"] or project,
                      summary="", started_at=None, ended_at=None, started_at_basis="unknown",
                      development_days=None, revision=0, status=registry["status"],
                      created_at=registry["created_at"], updated_at=registry["updated_at"],
                      has_archive=False, archive_status=None)
    else:
        result = {**archive, "has_archive": True, "archive_status": archive["status"]}
    events = ProjectArchiveStore(service.store).events(project, include_historical=True)
    events.sort(key=lambda e: (e["occurred_at"] or e["recorded_at"], e["event_id"]), reverse=True)
    total = len(events)
    pages = max(1, (total + size - 1) // size)
    page = min(requested, pages)
    result.update(project_status=registry["status"], events_total=total, events_page=page,
                  events_page_size=size, events_pages=pages,
                  current_events_total=sum(e["current_support"] for e in events))
    result["events"] = events[(page - 1) * size:page * size]
    return result
