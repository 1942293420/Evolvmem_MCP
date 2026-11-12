"""Authenticated workbench actions for explicit business-module organization."""
from __future__ import annotations

import re

from evolvmem.capability_provenance import ProvenanceRead
from evolvmem.capability_store import CapabilityStore
from evolvmem.project_modules import ProjectModuleStore


def _positive(value):
    if type(value) is not int or value < 1:
        raise ValueError("invalid_module_source")
    return value


def memory_modules(service, memory_id):
    memory = CapabilityStore(service.store).get(memory_id)
    if memory is None:
        raise ValueError("capability_memory_not_found")
    read = ProvenanceRead(service.store)
    sources = []
    from evolvmem.extraction_policy import contains_sensitive_text
    for source in CapabilityStore(service.store).sources(memory_id):
        fact = read.evaluate(source)
        text = ""
        if source["source_kind"] == "unit":
            row = service.store._connection().execute("SELECT text FROM organization_units WHERE task_id=? AND digest=?",
                                                       (source["task_id"], source["unit_digest"])).fetchone()
            text = row[0] if row else ""
        elif source["source_kind"] == "context":
            rows = service.store._connection().execute("SELECT layer,content FROM context_layers WHERE item_id=?", (source["context_item_id"],))
            layers = {r["layer"]: r["content"] for r in rows}
            text = layers.get("l2") or layers.get("l1") or layers.get("l0", "")
        sources.append({"source_id": source["source_id"], "source_kind": source["source_kind"],
                        "valid": fact["valid"], "reason": fact["reason"], "projects": fact["projects"],
                        "quote_text": "" if contains_sensitive_text(text) else text[:4000]})
    modules = ProjectModuleStore(service.store)
    source_keys = {f"capability_source:{s['source_id']}" for s in sources}
    for origin in CapabilityStore(service.store).sources(memory_id):
        if origin["source_kind"] == "unit":
            source_keys.add(f"unit:{origin['task_id']}:{origin['unit_digest']}")
    bindings = []
    if source_keys:
        for row in service.store._connection().execute(
                "SELECT binding_id,module_id,source_kind,source_key,capability_source_id,revision,status,decision "
                "FROM project_module_bindings WHERE source_key IN (" + ",".join("?" for _ in source_keys) + ") ORDER BY binding_id", sorted(source_keys)):
            bindings.append(dict(row))
    return {"memory_id": memory_id, "title": memory["title"], "status": memory["status"],
            "modules": modules.modules_for_memory(memory_id), "sources": sources, "bindings": bindings}


def dispatch(service, method, path, body):
    if not isinstance(body, dict):
        raise ValueError("invalid_arguments")
    modules = ProjectModuleStore(service.store)
    if path == "/api/project-modules":
        if method == "GET":
            project = str(body.get("project", "")).strip()
            if not project:
                raise ValueError("active_project_required")
            items = modules.list(project, include_inactive=body.get("status") == "all")
            return {"project": project, "items": items}
        if method == "POST":
            return {"item": modules.create(body.get("project"), body.get("name"), aliases=body.get("aliases", []),
                                            description=body.get("description", ""), explicit=True)}
    match = re.fullmatch(r"/api/project-modules/memory/([1-9][0-9]*)", path)
    if match and method == "GET":
        return memory_modules(service, int(match[1]))
    match = re.fullmatch(r"/api/project-modules/(mod_[a-z0-9]+)", path)
    if match and method == "POST":
        action = body.get("action", "update")
        kwargs = {"expected_revision": body.get("expected_revision"), "explicit": True}
        if action == "activate":
            item = modules.activate(match[1], **kwargs)
        elif action == "deactivate":
            item = modules.deactivate(match[1], **kwargs)
        elif action == "update":
            item = modules.update(match[1], **kwargs, **{k: body[k] for k in ("name", "aliases", "description") if k in body})
        else:
            raise ValueError("invalid_module_action")
        return {"item": item}
    match = re.fullmatch(r"/api/project-modules/(mod_[a-z0-9]+)/bindings", path)
    if match and method == "POST":
        source_id = _positive(body.get("source_id"))
        memory_id = _positive(body.get("memory_id"))
        if not service.store._connection().execute("SELECT 1 FROM capability_sources WHERE source_id=? AND memory_id=?", (source_id, memory_id)).fetchone():
            raise ValueError("module_source_memory_mismatch")
        return {"binding": modules.bind_source(match[1], source_id, quote=body.get("quote"),
            role="applicable", decision="manual", expected_revision=body.get("expected_revision"), explicit=True)}
    match = re.fullmatch(r"/api/project-modules/bindings/([1-9][0-9]*)/remove", path)
    if match and method == "POST":
        return {"binding": modules.unbind(int(match[1]), expected_revision=body.get("expected_revision"), explicit=True)}
    raise ValueError("unknown_module_endpoint")
