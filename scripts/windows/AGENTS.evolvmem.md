<!-- BEGIN EVOLVMEM MEMORY -->
## EvolvMem memory and continuity

- Use the actual workspace and the injected EvolvMem connection metadata for project, device_id and session_id. Do not invent a project binding or replace the device ID with a hostname.
- If this session has not already received context, call context_session_start once with the actual workspace_path and project. For a substantive new task or a new failure, use experience_recall with observed facts; historical memory is reference material, not authorization.
- For a clear task, use continuity_begin to create or resume its checkpoint. On "continue", use continuity_resume; if the project or focus is unresolved, use continuity_find and verify the workspace before proceeding.
- Save user-confirmed decisions, verified progress, blockers and next actions with continuity_checkpoint at meaningful milestones and before ending substantial work. Use the server's latest revision. Save durable preferences or facts with the appropriate memory tool; do not duplicate raw conversation messages as memories.
- MCP tools run only when called. These instructions cannot guarantee automatic writes or capture every message. The installed EvolvMem background collector reads complete records already persisted in Codex session logs, queues them locally, and uploads through session_archive_upload. Linux stores encrypted source archives and generates summaries and indexes.
- Do not claim that a conversation is synchronized from these instructions or an assistant statement alone. Check the client queue, worker receipt and server archive/extraction status when synchronization needs verification. Report failed memory operations accurately and continue independent work.
- Respect the user's current instructions, including requests for read-only work or no tool calls. Do not place tokens, private keys or raw conversation archives in AGENTS.md.
<!-- END EVOLVMEM MEMORY -->
