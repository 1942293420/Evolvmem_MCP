# Project business-module workbench

The desktop project detail now shows explicitly registered business modules separately from the capability library's technical categories. An empty registry says that no business modules have been defined. It never creates modules from frontend/backend labels, candidates, or an empty result.

Project controls create, edit, deactivate, and reactivate modules. Pending suggestions remain pending until the user chooses **确认使用**. Mutations submit the latest fetched revision. A conflict re-reads the server, preserves the form draft, and asks the user to review it; it never retries a write automatically. Archived project views hide these controls.

`window.EvolvMemModules.mountMemory(element, memory_id)` is exported by the existing `project-archives.js`; no additional script is required. It returns a Promise and is safe to call again for the same container. Expanded capability cards should mount it lazily. The module also exports `mountProject(element, project, {readOnly})` for project details.

Memory views combine the server's current `modules`, `sources`, and complete `bindings` arrays. A writable view permits a user to choose a current source, one of its confirmed projects, an active registered module, and a quote selected directly from the displayed source text. The quote cannot be typed freely. Pending capability records can be classified and are labelled as classification only. Archived or ignored records hide mutation controls. Invalid historical sources remain explained but cannot be selected to confirm a module. The server remains authoritative for source validity, quote verification, ownership, permissions, and revision conflicts.

Binding creation/restoration uses the matching capability source binding's actual revision, or zero only when no binding exists. Removal uses the exact returned binding revision, including bindings originating from a unit. A conflict does not optimistically add or remove a module. Module activation changes refresh visible memory panels. Page-lifetime caches retain drafts across route or DOM remounts; they hold no localStorage records and evict disconnected clean entries when the cache exceeds 80. Reads and writes time out after 15 seconds; failed writes are never automatically replayed.

Every mutation handler and the request layer checks `EvolvAuth.canWrite === true`. Read-only views provide no write controls. All server/user text is escaped before HTML interpolation. Native controls have visible labels, keyboard focus styling, inline status feedback, and disabled submission while saving.

Project event history remains visible when `current_support` is false. The list labels it **历史来源已变更，不计入当前摘要**, and expanding it shows a localized source diagnostic. Directory/detail counts distinguish current supported events from all retained history. Older responses that lack these fields keep the prior historical count presentation.

## Isolated browser verification

`tests/browser/project_modules.e2e.cjs` runs real Chromium against `project_modules_fixture.cjs`, an in-memory local HTTP fixture. It never reads production credentials or databases. Set `PLAYWRIGHT_MODULE` and optionally `CHROMIUM_EXECUTABLE` for an existing local Playwright installation; `EVOLVMEM_STATIC_DIR` overrides the static directory and `EVOLVMEM_SCREENSHOT_DIR` writes screenshots.

```sh
node tests/browser/project_modules.e2e.cjs
node tests/browser/project_library.e2e.cjs
```

Coverage: desktop create/edit/status changes, pending activation, module CAS with preserved draft, source quote selection, binding restore/removal CAS, live deactivation, empty registry, capability pending labels, changed/unavailable historical sources, repeat mount draft/cache preservation, HTML escaping, revoked and initial read-only permission, and existing project directory navigation. These are synthetic fixture checks; the root integration run separately verifies the real authenticated backend.
