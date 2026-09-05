"""Resolve experience evidence to a concrete local transcript event."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from evolvmem.extraction_policy import contains_sensitive_text


_TASK_ID = re.compile(r"[A-Za-z0-9_.:-]{1,500}\Z")
_NATIVE_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z")
_CODEX_FILENAME = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl\Z")
_FIX_HEADING = re.compile(r"^(#{1,6})\s*验证\s*$", re.MULTILINE)
_ANY_HEADING = re.compile(r"^(#{1,6})\s+.*$", re.MULTILINE)
_TOOL_TYPES = {
    "function_call_output", "custom_tool_call_output", "tool_result", "tool.result", "tool/result",
    "tool-response", "tool_response",
}
_USER_TYPES = {"turn.prompt", "user_message", "user.message", "user/message"}
_STRUCTURAL_KEYS = {"type", "role", "name", "id", "call_id", "tool_call_id"}
_MAX_LINE_BYTES = 1024 * 1024
_MAX_FIX_BYTES = 1024 * 1024
_MAX_SNAPSHOT_CHARS = 800


@dataclass(frozen=True, slots=True)
class ResolvedExperienceSource:
    source_kind: str
    source_ref: str
    digest: str
    snapshot: str
    task_id: str | None = None


class ExperienceSourceResolver:
    """Read-only resolver restricted to known adapter and fix-record roots."""

    def __init__(
        self, *, codex_roots=None, kimi_roots=None, dsh_roots=None,
        fix_root=None, max_recent_transcripts=12, recent_window_seconds=86400,
    ):
        home = Path.home()
        self.codex_roots = self._roots(
            codex_roots, (Path(os.environ.get("CODEX_HOME") or home / ".codex") / "sessions",))
        self.kimi_roots = self._roots(
            kimi_roots, (home / ".kimi-code" / "sessions",))
        self.dsh_roots = self._roots(
            dsh_roots, (home / ".dsh" / "sessions",))
        self.fix_root = Path(
            fix_root if fix_root is not None else home / "fix-records" / "records"
        ).expanduser().resolve()
        if (type(max_recent_transcripts) is not int or max_recent_transcripts < 1
                or not isinstance(recent_window_seconds, (int, float))
                or recent_window_seconds <= 0):
            raise ValueError("invalid recent transcript window")
        self.max_recent_transcripts = max_recent_transcripts
        self.recent_window_seconds = recent_window_seconds

    @staticmethod
    def _roots(given, defaults):
        values = defaults if given is None else given
        return tuple(Path(value).expanduser().resolve() for value in values)

    def resolve(self, *, source_kind, source_ref, task_id, quote):
        if source_kind not in {"tool_result", "user_confirmation", "historical_record"}:
            raise ValueError("verifiable source required")
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 1000:
            raise ValueError("evidence quote required")
        quote = quote.strip()
        if source_kind == "historical_record":
            if task_id in (None, "", "current"):
                raise ValueError("historical_record requires explicit task_id")
            return self._resolve_fix_record(source_ref, quote)
        current = task_id in (None, "", "current")
        if current:
            task_id = "current"
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise ValueError("invalid task_id for transcript resolution")
        if source_ref:
            path, line_number = self._parse_line_ref(source_ref)
            adapter = self._adapter_for(path)
            bound_task = self._bind_task(adapter, path, task_id)
            for number, raw, aliases in self._canonical_lines(path, adapter, source_kind):
                if line_number in aliases:
                    return self._resolve_event(
                        path, number, raw, adapter, source_kind, quote, bound_task)
                if number > line_number:
                    break
            raise ValueError("source line does not exist or is too large")
        return self._locate_event(source_kind, task_id, quote)

    def validate_stored(self, *, source_kind, source_ref, task_id):
        """Validate the task binding of a previously resolved source row."""
        if source_kind == "historical_record":
            path = Path(source_ref).resolve()
            return self._inside(path, self.fix_root) and path.suffix.casefold() == ".md"
        try:
            path, _ = self._parse_line_ref(source_ref)
            adapter = self._adapter_for(path)
        except ValueError:
            return False
        return self.task_matches(adapter, path, task_id)

    @staticmethod
    def task_matches(adapter, path, task_id):
        """Return whether a transcript path canonically belongs to task_id."""
        if adapter == "codex":
            match = _CODEX_FILENAME.fullmatch(path.name)
            if match:
                return task_id == match.group(1)
            return task_id in path.stem
        if adapter == "kimi":
            session = path.parents[2].name
            return session in {task_id, f"session_{task_id}"}
        if adapter == "dsh":
            session = path.parent.name
            return session in {task_id, f"session-{task_id}"}
        return False

    @staticmethod
    def _native_task_id(adapter, path):
        if adapter == "codex":
            match = _CODEX_FILENAME.fullmatch(path.name)
            return match.group(1) if match else None
        if adapter == "kimi" and path.parent.name == "main" and path.parents[1].name == "agents":
            session, prefix = path.parents[2].name, "session_"
        elif adapter == "dsh":
            session, prefix = path.parent.name, "session-"
        else:
            return None
        if not session.startswith(prefix) and not _NATIVE_UUID.fullmatch(session):
            return None
        task_id = session.removeprefix(prefix)
        if task_id != "current" and not task_id.startswith("ws_") and _TASK_ID.fullmatch(task_id):
            return task_id
        return None

    def _bind_task(self, adapter, path, task_id):
        if task_id.startswith("ws_"):
            raise ValueError("workspace identity is not a native task_id")
        native_id = self._native_task_id(adapter, path)
        if task_id == "current":
            if native_id is None:
                raise ValueError("source lacks canonical native task_id")
        elif not self.task_matches(adapter, path, task_id):
            raise ValueError("task_id does not match source transcript")
        if adapter == "codex" and native_id:
            try:
                first = json.loads(self._read_line(path, 1))
            except json.JSONDecodeError as error:
                raise ValueError("invalid session metadata") from error
            if first.get("type") == "session_meta" and first.get("payload", {}).get("id") != native_id:
                raise ValueError("session metadata does not match native task_id")
        return native_id or task_id

    def _parse_line_ref(self, source_ref):
        if not isinstance(source_ref, str):
            raise ValueError("invalid source_ref")
        path_text, marker, line_text = source_ref.rpartition("#")
        if not marker or not line_text.isdigit() or int(line_text) < 1:
            raise ValueError("source_ref must be <absolute-jsonl-path>#<line>")
        path = Path(path_text)
        if not path.is_absolute():
            raise ValueError("source_ref path must be absolute")
        return path.resolve(), int(line_text)

    def _adapter_for(self, path):
        for adapter, roots in (
            ("codex", self.codex_roots), ("kimi", self.kimi_roots),
            ("dsh", self.dsh_roots),
        ):
            if any(self._inside(path, root) for root in roots):
                if adapter == "kimi" and path.name != "wire.jsonl":
                    continue
                if adapter == "dsh" and path.name not in {
                    "session.jsonl", "session.jsonl.zstd"}:
                    continue
                if adapter == "codex" and path.suffix != ".jsonl":
                    continue
                return adapter
        raise ValueError("source is outside approved transcript roots")

    @staticmethod
    def _inside(path, root):
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _candidate_paths(self, task_id):
        paths = []
        for root in self.codex_roots:
            if root.is_dir():
                paths.extend(root.glob(f"**/*{task_id}*.jsonl"))
        kimi_ids = (task_id,) if task_id.startswith("session_") else (
            task_id, f"session_{task_id}")
        for root in self.kimi_roots:
            if root.is_dir():
                for session_id in kimi_ids:
                    paths.extend(root.glob(
                        f"*/{session_id}/agents/main/wire.jsonl"))
        dsh_ids = (task_id,) if task_id.startswith("session-") else (
            task_id, f"session-{task_id}")
        for root in self.dsh_roots:
            if root.is_dir():
                for session_id in dsh_ids:
                    paths.extend(root.glob(f"*/{session_id}/session.jsonl"))
                    paths.extend(root.glob(f"*/{session_id}/session.jsonl.zstd"))
        unique = []
        for path in paths:
            resolved = path.resolve()
            if resolved not in unique:
                unique.append(resolved)
        if len(unique) > 12:
            raise ValueError("too many transcripts match task_id")
        return unique

    def _locate_event(self, source_kind, task_id, quote):
        hits = []
        paths = self._recent_paths() if task_id == "current" else self._candidate_paths(task_id)
        for path in paths:
            adapter = self._adapter_for(path)
            bound_task = self._bind_task(adapter, path, task_id)
            for line_number, raw, _ in self._canonical_lines(path, adapter, source_kind):
                try:
                    result = self._resolve_event(
                        path, line_number, raw, adapter, source_kind, quote, bound_task)
                except ValueError:
                    continue
                hits.append(result)
                if len(hits) > 1:
                    raise ValueError(
                        "multiple source events contain quote; supply source_ref")
        if not hits:
            raise ValueError("matching source event not found for task_id")
        return hits[0]

    def _recent_paths(self):
        """Only inspect recent native transcripts within the configured roots."""
        cutoff = time.time() - self.recent_window_seconds
        candidates = {}
        for adapter, roots, patterns in (
            ("codex", self.codex_roots, ("**/rollout-*.jsonl",)),
            ("kimi", self.kimi_roots, ("*/*/agents/main/wire.jsonl",)),
            ("dsh", self.dsh_roots, ("*/*/session.jsonl", "*/*/session.jsonl.zstd")),
        ):
            for root in roots:
                for pattern in patterns:
                    for path in root.glob(pattern):
                        path = path.resolve()
                        if not self._inside(path, root) or not self._native_task_id(adapter, path):
                            continue
                        try:
                            modified = path.stat().st_mtime
                        except OSError:
                            continue
                        if modified >= cutoff and path.is_file():
                            candidates[path] = modified
        return sorted(candidates, key=lambda path: (candidates[path], str(path)), reverse=True)[:self.max_recent_transcripts]

    def _canonical_lines(self, path, adapter, source_kind):
        """Collapse an adjacent Codex user/mirror pair to response_item once.

        Pair only equal complete messages of opposite native types. Repeated
        user messages, even identical ones, remain separate source events.
        """
        pending = None
        for number, raw in self._iter_lines(path):
            descriptor = self._codex_user_descriptor(raw) if (
                adapter == "codex" and source_kind == "user_confirmation") else None
            if pending is not None:
                previous_number, previous_raw, previous = pending
                if (descriptor is not None and number == previous_number + 1
                        and descriptor[0] != previous[0] and descriptor[1] == previous[1]):
                    canonical = (number, raw) if descriptor[0] == "response_item" else (previous_number, previous_raw)
                    yield canonical[0], canonical[1], (previous_number, number)
                    pending = None
                    continue
                yield previous_number, previous_raw, (previous_number,)
                pending = None
            if descriptor is not None:
                pending = number, raw, descriptor
            else:
                yield number, raw, (number,)
        if pending is not None:
            number, raw, _ = pending
            yield number, raw, (number,)

    def _codex_user_descriptor(self, raw):
        try:
            event = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
            return None
        payload = event["payload"]
        kind = event.get("type")
        if kind == "response_item" and payload.get("role") == "user" and payload.get("type") == "message":
            return kind, self._content_text(payload.get("content", []))
        if kind == "event_msg" and payload.get("type") == "user_message" and isinstance(payload.get("message"), str):
            return kind, payload["message"]
        return None

    def _resolve_event(self, path, line_number, raw, adapter, source_kind, quote, task_id=None):
        try:
            event = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("source line is not valid JSON") from error
        wanted = "tool" if source_kind == "tool_result" else "user"
        texts = [self._content_text(node) for node in self._qualifying_nodes(event, wanted)]
        texts = [value for value in texts if quote in value]
        if not texts:
            raise ValueError(f"quote is not in a native {wanted} event")
        content = min(texts, key=len)
        if contains_sensitive_text(content):
            raise ValueError("source event contains sensitive content")
        canonical_ref = f"{path.resolve()}#{line_number}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return ResolvedExperienceSource(
            source_kind=source_kind, source_ref=canonical_ref,
            digest=digest, snapshot=self._excerpt(content, quote), task_id=task_id,
        )

    def _qualifying_nodes(self, value, wanted):
        if isinstance(value, dict):
            kind = str(value.get("type", "")).casefold()
            role = str(value.get("role", "")).casefold()
            if wanted == "tool" and (kind in _TOOL_TYPES or role == "tool"):
                yield value
                return
            if wanted == "user" and (
                kind in _USER_TYPES or role == "user"
            ):
                yield value
                return
            # Assistant nodes are terminal: nested text is never user/tool proof.
            if role == "assistant" or kind in {"assistant_message", "content.part"}:
                return
            for child in value.values():
                yield from self._qualifying_nodes(child, wanted)
        elif isinstance(value, list):
            for child in value:
                yield from self._qualifying_nodes(child, wanted)

    def _content_text(self, value):
        parts = []

        def visit(node, key=""):
            if isinstance(node, str):
                if key not in _STRUCTURAL_KEYS:
                    parts.append(node)
            elif isinstance(node, dict):
                for child_key, child in node.items():
                    visit(child, child_key)
            elif isinstance(node, list):
                for child in node:
                    visit(child, key)

        visit(value)
        return "\n".join(parts)

    @staticmethod
    def _excerpt(content, quote):
        if len(content) <= _MAX_SNAPSHOT_CHARS:
            return content
        start = max(0, content.find(quote) - 200)
        return content[start:start + _MAX_SNAPSHOT_CHARS]

    def _resolve_fix_record(self, source_ref, quote):
        if not isinstance(source_ref, str) or not source_ref:
            raise ValueError("historical_record source_ref required")
        path = Path(source_ref)
        if not path.is_absolute():
            raise ValueError("historical_record path must be absolute")
        path = path.resolve()
        if not self._inside(path, self.fix_root) or path.suffix.casefold() != ".md":
            raise ValueError("historical_record must be under fix-records/records")
        if not path.is_file() or path.stat().st_size > _MAX_FIX_BYTES:
            raise ValueError("historical_record is missing or too large")
        text = path.read_text(encoding="utf-8")
        match = _FIX_HEADING.search(text)
        if match is None:
            raise ValueError("historical_record has no 验证 section")
        end = len(text)
        for heading in _ANY_HEADING.finditer(text, match.end()):
            if len(heading.group(1)) <= len(match.group(1)):
                end = heading.start()
                break
        verification = text[match.end():end].strip()
        if quote not in verification:
            raise ValueError("quote is not in historical_record 验证 section")
        if contains_sensitive_text(verification):
            raise ValueError("historical_record verification contains sensitive content")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return ResolvedExperienceSource(
            source_kind="historical_record", source_ref=str(path),
            digest=digest, snapshot=self._excerpt(verification, quote),
        )

    def _iter_lines(self, path):
        if not path.is_file():
            raise ValueError("source transcript does not exist")
        if path.name.endswith(".zstd"):
            executable = shutil.which("zstd")
            if executable is None:
                raise ValueError("zstd reader unavailable for DSH transcript")
            process = subprocess.Popen(
                [executable, "-dc", "--", str(path)], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace")
            assert process.stdout is not None
            try:
                for number, line in enumerate(process.stdout, 1):
                    if len(line.encode("utf-8")) > _MAX_LINE_BYTES:
                        continue
                    yield number, line.rstrip("\n")
            finally:
                process.stdout.close()
                process.wait(timeout=5)
            return
        with path.open(encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                if len(line.encode("utf-8")) > _MAX_LINE_BYTES:
                    continue
                yield number, line.rstrip("\n")

    def _read_line(self, path, line_number):
        for number, raw in self._iter_lines(path):
            if number == line_number:
                return raw
            if number > line_number:
                break
        raise ValueError("source line does not exist or is too large")
