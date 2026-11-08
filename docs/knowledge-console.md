# Personal knowledge console

Each account has two separately readable resources: history and experience.

## Choosing a knowledge library

The **User libraries** overview is the only place to choose whose knowledge to read. It is the landing page and the brand link returns there. Its cards open history or experience for the selected owner. Selecting a library never changes the signed-in account shown in the header.

History and experience share a static “currently viewing” owner label, an access-mode badge, and a link back to the overview. Navigation between them retains the owner and read-only inspection mode. There is no second owner dropdown. The overview, user/permission administration, and working-principles pages do not display an individual owner's context.

An administrator's own card opens ordinary self browsing, retaining self history management. Other users' cards explicitly open administrator read-only inspection. Ordinary members see only their own and granted libraries; all existing directed and resource-specific authorization checks remain in force.

- History reads an explicitly connected conversation candidate. It shows a project introduction, a short chronological list, and original question/answer blocks. Unassigned conversation blocks remain separate. Only the resource owner can soft-delete and restore their own history; immutable source data is retained.
- Experience reads structured cases from the account's Context source using a read-only snapshot. Verified, candidate and inactive records retain their existing meanings. A verified case must be current, active and have positive evidence recorded by the existing experience service.
- An administrator can create, bind and disable Web accounts. Feishu OAuth is verified before the console records an identity as pending; an unbound identity cannot read knowledge. New Web accounts do not automatically receive LAN credentials or a source database.
- Directed grants specify viewer, owner, history permission and experience permission. The reverse direction is independent. Normal browsing, including the administrator's ordinary browsing, obeys grants. Explicit administrator `inspect=1` is a read-only review mode for all libraries. Cross-user mutations and cross-user trash reads are forbidden.

## Reading extracted experience

The main `/#experiences` entry defaults to history extraction with `review=accepted`. **已整理完成**, **全部资料**, and **处理中** are direct view switches; the last combines pending session review, pending acceptance, and requested revisions. Existing Core experience remains available under **其他已有经验** with its original verification states. Switching sources clears incompatible category or verification filters.

Progress and eight-category counts refer to the selected library's entire visible history extraction, independent of search and pagination. The result count refers to the current filters. All statistics use the same authorized owner, tombstone, supersession and deleted-item boundaries as the cards; no data is written or status promoted by reading them.

Cards show the problem and first solution step. The first result opens automatically, and selecting another card shows the full problem, solution, key changes, result, applicability and quoted original sources. Search, project, category, owner and inspection state stay in the URL. Empty results provide a clear-filter or all-materials route. Keyboard navigation and the 390, 768 and 1440 pixel layouts are covered by the synthetic browser suite.

**整理已验收** describes content review, not independent validation. Extracted records remain `candidate`, `unverified`, with `success_count=0`; source-project associations describe provenance rather than the method's applicability. Core evidence rules and all existing account/grant checks remain unchanged.

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

The dedicated account, LAN, policy and Web HTTP tests use temporary SQLite databases and synthetic identities. They cover colliding resource IDs, directed grants and revocation, pending login binding, account disable/enable, CSRF, explicit inspection, attachment isolation, source immutability and bounded body parsing. Run:

```sh
python -B -m pytest -q tests/test_knowledge_console.py tests/test_console_lan_access.py tests/test_console_web_integration.py tests/test_product_policy.py tests/test_history_candidate_read.py tests/test_history_candidate_view.py tests/test_history_management.py
```

`tests/browser/launch_console_fixture.py` starts a loopback-only real WebAuth/HTTP fixture with synthetic session cookies. `tests/browser/product_modules.e2e.cjs` requires `EVOLVMEM_SYNTHETIC_FIXTURE` and a fixture URL. Use an ordinary HTTP test hostname to exercise browsers without `crypto.randomUUID`, and supply `PLAYWRIGHT_MODULE`/`CHROMIUM_EXECUTABLE` for the local test environment. The browser suite checks the current navigation, history, experience, users, directed permissions, deletion/restore, and responsive layouts. The fixture does not validate a live OAuth provider login.

`tests/browser/library_navigation_unify.e2e.cjs` checks the single library-selection entry, static owner context, global-page context removal, preservation of the signed-in actor, self management, and read-only navigation for ordinary granted readers and administrator inspection.
