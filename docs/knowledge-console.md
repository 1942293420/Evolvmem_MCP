# Personal knowledge console

Each account has two separately readable resources: history and experience.

## Choosing a knowledge library

The **User libraries** overview is the only place to choose whose knowledge to read. It is the landing page and the brand link returns there. Its cards open history or experience for the selected owner. Selecting a library never changes the signed-in account shown in the header.

History and experience share a static “currently viewing” owner label, an access-mode badge, and a link back to the overview. Navigation between them retains the owner and read-only inspection mode. There is no second owner dropdown. The overview, user/permission administration, and working-principles pages do not display an individual owner's context.

An administrator's own card opens ordinary self browsing, retaining self history management. Other users' cards explicitly open administrator read-only inspection. Ordinary members see only their own and granted libraries; all existing directed and resource-specific authorization checks remain in force.

- History reads an explicitly connected conversation candidate. It shows a project introduction, a short chronological list, and original question/answer blocks. Unassigned conversation blocks remain separate. Only the resource owner can soft-delete and restore their own history; immutable source data is retained.
- Experience is a manually maintained catalog with three categories: **前端经验**, **业务经验**, and **系统环境经验**. Each entry contains **问题**, **解决办法**, and **可复用经验**, without a separate title. Only the resource owner can create, edit, soft-delete and restore entries. Reading an entry does not mark it verified or alter existing Core evidence.
- An administrator can create, bind and disable Web accounts. Feishu OAuth is verified before the console records an identity as pending; an unbound identity cannot read knowledge. New Web accounts do not automatically receive LAN credentials or a source database.
- Directed grants specify viewer, owner, history permission and experience permission. The reverse direction is independent. Normal browsing, including the administrator's ordinary browsing, obeys grants. Explicit administrator `inspect=1` is a read-only review mode for all libraries. Cross-user mutations and cross-user trash reads are forbidden.

## Console layout

The console work area follows the available browser width on every page, with
responsive side gutters. History and experience split the work area between
their list and detail panels on wider screens. At 780 CSS pixels and below
they stack in a single column, so a narrow window or zoomed-in view keeps
both panels within the browser width.

## Maintaining experience

Search stays above the category filters. Selecting a result updates the
detail panel while keeping the result list, its scroll position, and desktop
keyboard focus in place. On narrow screens, the detail offers a return to the
selected list item. Closing an edited experience asks whether to continue
editing or discard the changes.

The main `/#experiences` entry opens the selected user's manual catalog. It provides the three categories, search, a list and a detail view. Details contain only the problem, solution and reusable experience. The owner can add or edit an entry and move it to the recycle bin; restoring it returns it to its category. There is no automatic extraction, model selection, completion-review filter or eight-category classification in this flow.

Users can also request one entry in conversation. For example, “请记住：以后部署前端先核对线上首页引用的资源版本” asks the assistant to save the stated practice. The Codex `UserPromptSubmit` hook supplies a reminder for a possible instruction in the current message. The assistant checks the user's intent and determines the content and category from the current context before calling `manual_experience_save`. The tool writes the same personal catalog as the Web form and returns a record ID. A retry uses the same request ID. `manual_experience_search` looks up active entries in the authenticated caller's catalog.

Each chat entry covers one issue. Write each field on one line: 问题 (trigger and failure, up to 40 characters), 解决办法 (concrete actions, up to 90), and 可复用经验 (one reusable rule, up to 50). Keep the conditions needed to apply the rule and leave out titles, task history, and repeated wording. MCP rejects a field over the limit or containing a line break. Shorten the wording and retry with the same request ID. The Web editor and previously saved entries retain their existing format.

Examples, quoted text, questions about the feature, and historical messages do not authorize a write. “写入全局.md” and “写入项目.md” can also specify an actual rules file. The assistant resolves that file from the configured scope and current workspace and reports file edits separately from the database receipt. An unresolved filename needs clarification before editing. The hook does not edit files or scan the archive for instructions.

The Windows installer already registers `UserPromptSubmit`. Linux Codex can register the `scripts/linux/codex-memory-intent.py` command in its `UserPromptSubmit` hook configuration. Review and trust a new hook in Codex `/hooks` before first use. The MCP tools also accept an explicit request from clients that have no hook.

Category and library counts describe this manual catalog, not the earlier extracted materials. Search and category selection must use the same owner, access mode and deletion boundary as the list and details. Reading a library or an empty catalog does not initialize or migrate a source database.

Writes use the authenticated account and the configured owner source; a readable cross-user grant never grants write access. Administrator inspection is also read-only, including the administrator's access to another owner's catalog. Every mutation rechecks the account and owner boundaries, preserves deleted records for recovery, and rejects stale revisions rather than silently replacing a newer edit. POST continues to require the existing session CSRF token.

The previous HY4/DeepSeek extraction materials, earlier previews and review audit records remain in their original storage. This change does not delete, reclassify, accept or copy them into the manual catalog. The console does not expose the old extraction workflow as an alternate experience source. Further extraction is a separate operation that requires a new explicit user request; browsing or saving a manual entry never calls a model.

Raw conversation capture, project archiving and task continuation remain separate from experience extraction. Their existing authorization and ownership checks are unchanged. Stopping experience extraction does not stop raw-history capture or remove project history.

## Web experience search

The experience search box combines literal matches in the problem, solution
and reusable rule with cosine similarity from the existing loopback Chinese
embedding service. Literal hits sort first. The semantic query expands the
short topic “布局” to include responsive layout concepts. The list and category
counts derive from the same ranked results. The recycle bin uses literal
matching.

The Web console checks library access before reading any notes. Only active
manual notes of that owner enter the semantic path. A bounded process-local
cache keys document vectors by source, owner, note ID and encoded content hash.
Edits are reflected by current source rows. Search performs no index writes or
schema migration and sends text only to the configured loopback endpoint.
When the model is unavailable, busy, or the cold search budget is exceeded,
the console shows keyword results. Libraries above 512 active notes also use
keyword search. The existing MCP search contract remains literal.

## Trust and deployment boundaries

`ConsoleService` stores account mappings, grants and audit records in a separate metadata SQLite database. Actor identity comes exclusively from a trusted WebAuth session, never from a user ID or namespace in request data. Source directories and history environment mappings can only be installed by trusted server configuration. Every list, search, detail and attachment request checks authorization before opening a source. Denied resource counts and connection states are not returned to ordinary readers.

Web endpoints live under `/api/knowledge-console`. POST requires the session CSRF token. Source reads bypass the old single-owner response cache and snapshot selection. Request bodies reject duplicate/invalid lengths, transfer encoding, oversized and truncated payloads. No private source paths, tokens or OAuth configuration belong in the repository.

The production operator explicitly bootstraps the configured owner using the existing verified Web owner OpenID. Bootstrap is not performed by a read request. Connect each library with `ConsoleService.configure_library` using trusted server paths; no source-attachment HTTP endpoint exists. Existing source connections and tombstones must be retained when replacing a history candidate.

To permit pending member logins, set the existing Web auth configuration's `owner_only` to false only after the console and legacy route gate are installed. Keep the existing enterprise restriction. Existing LAN bearer validation remains in place; the console additionally checks the registered account's enabled state. A missing or damaged registry fails closed.

## Product policy

Set `EVOLVMEM_PRODUCT_POLICY_FILE` explicitly in every Web, LAN, standalone MCP and hook launcher. A console deployment uses:

```json
{"console_enabled":true,"legacy_web_enabled":false,"ai_processing_enabled":false}
```

The explicit file is read at runtime and fails closed if unavailable or malformed. An installation that has never configured the policy keeps compatibility defaults. Old Web business routes return `legacy_web_disabled`; the new shell loads only its current modules. AI workers and the final provider boundary enforce the pause. Pending work is retained rather than falsely marked completed or failed. Kimi and DSH archive raw input before attempting AI processing. The LAN raw collector remains independent of old organization scheduling, with a cross-process scan lease.

The legacy capability-index recovery loop also pauses when the old library is disabled. Context recovery and physical-integrity checks remain available for current raw archive and continuation operations; this does not delete either index.

Deploying a Python file cannot update functions already loaded in a long-lived process. Restart the actual Web/LAN/DSH units and reconnect older standalone MCP clients. Preserve original databases, raw archives and continuation jobs. Do not infer successful OAuth login or archive synchronization from a code change alone.

## Eight-hour login persistence

The Web host stores its own sessions with an absolute expiry of 28800 seconds from login. Restarting the host preserves the remaining lifetime and CSRF token; normal requests never renew it. Browser cookies are random opaque values with HttpOnly and SameSite=Lax (and Secure for HTTPS); metadata stores only their SHA256 hashes, the trusted auth-configuration scope, identity, CSRF and expiry. Feishu access/refresh tokens are discarded after identity verification.

In console mode `web_sessions` shares the account registry, so account disabling or login rebinding deletes the affected identity's sessions in the same transaction. Creating a session rechecks the current enabled/pending identity inside that same database write transaction. Re-enabling an account cannot restore its deleted sessions. Logout revokes the exact cookie hash across configuration scopes. Pending logins have no knowledge access until an administrator binds them. Storage errors fail closed without an in-memory fallback. Compatibility hosting uses a separate `web-sessions.db` in its configured data directory.

The initial upgrade from the former memory-only implementation may require one final login. Later restarts preserve valid issued sessions. Keep the private account/session metadata in operational backups; never publish it with source exports. `tests/test_web_sessions.py` and `tests/test_web_login_persistence.py` cover storage, actual HTTP OAuth login/restart, fixed expiry, revocation and token exclusion using synthetic identities.

## Verification

The dedicated account, LAN, policy and Web HTTP tests use temporary SQLite databases and synthetic identities. They cover colliding resource IDs, directed grants and revocation, pending login binding, account disable/enable, CSRF, explicit inspection, attachment isolation, source immutability and bounded body parsing. Manual-experience checks also cover the three accepted categories, owner-only writes, stale-revision rejection, deletion/recovery and isolation from old extraction materials. Run:

```sh
python -B -m pytest -q tests/test_knowledge_console.py tests/test_console_lan_access.py tests/test_console_web_integration.py tests/test_product_policy.py tests/test_history_candidate_read.py tests/test_history_candidate_view.py tests/test_history_management.py
```

`tests/browser/launch_console_fixture.py` starts a loopback-only real WebAuth/HTTP fixture with synthetic session cookies. `tests/browser/product_modules.e2e.cjs` requires `EVOLVMEM_SYNTHETIC_FIXTURE` and a fixture URL. Use an ordinary HTTP test hostname to exercise browsers without `crypto.randomUUID`, and supply `PLAYWRIGHT_MODULE`/`CHROMIUM_EXECUTABLE` for the local test environment. The browser suite checks the current navigation, history, manual experience, users, directed permissions, deletion/restore, responsive layouts, and the updated manual-experience workflow diagram. The fixture does not validate a live OAuth provider login.

`tests/browser/library_navigation_unify.e2e.cjs` checks the single library-selection entry, static owner context, global-page context removal, preservation of the signed-in actor, self management, and read-only navigation for ordinary granted readers and administrator inspection.
