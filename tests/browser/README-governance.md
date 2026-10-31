# Desktop governance browser acceptance

Use a fresh disposable `knowledge_fixture.py` server. The fixture creates its own temporary database and substitutes only the external model provider. Browser actions use the real HTTP routes, SQLite persistence, provenance checks, value assessments and module store. Never set these tests to a production server.

```sh
EVOLVMEM_TEST_PORT=39489 EVOLVMEM_GOVERNANCE_FIXTURE=1 \
  .venv/bin/python tests/browser/knowledge_fixture.py
EVOLVMEM_TEST_URL=http://127.0.0.1:39489 EVOLVMEM_DESKTOP_ONLY=1 \
  node tests/browser/product_modules.e2e.cjs
EVOLVMEM_TEST_URL=http://127.0.0.1:39489 \
  node tests/browser/capability_governance.e2e.cjs
```

`PLAYWRIGHT_MODULE` and `CHROMIUM_EXECUTABLE` can point to an existing local Playwright installation/browser. For a remote fixture, forward only its local test port over SSH. Set `EVOLVMEM_SCREENSHOT_DIR` for artifacts; otherwise screenshots go to an OS temporary directory. The scripts write synthetic records, so start a new fixture for a complete repeat. Stop only the fixture/tunnel owned by this acceptance run.

`EVOLVMEM_DESKTOP_ONLY=1` tests 1280px and 1440px widths, skipping the old mobile loop. The default retains the historical multi-width checks. `EVOLVMEM_PRINCIPLES_ONLY=1` is an explicit read-only follow-up mode for diagrams after the complete workflow run; its output reports only the checks it executes.

The product suite checks current navigation, project creation, rule persistence, unsaved-draft protection, real history classification, the nine product diagrams, node details and keyboard actions, node/viewBox boundaries, and module-card label bounds. Screenshots include the new governance diagrams and their lower nodes, which are reachable by scrolling within the diagram panel.

The governance fixture supplies an assessed current source, a pending record with valid source/ownership, a fully unavailable source, a shared record with one current and one unavailable source, a bounded model-response failure, and an assigned archive explicitly enrolled for governance reads. Its suite checks independent source/value/verification labels, non-contamination of current projects, lazy module mounting, actual manual binding removal/restoration, pending classification, and a weak intake that remains pending after technical-category confirmation. It also verifies that a technical assessment failure is explained without declaring the content unsupported, and that an enrolled assigned source remains visible through the real HTTP review-group route. Module candidates remain inactive suggestions until explicit activation.

Acceptance on 2026-10-31: real Chromium 1248, product desktop suite 13 check groups and governance suite 7 check groups passed, with zero page JavaScript errors. These results establish the isolated desktop workflow, not production deployment, live identity-provider login, mobile behavior, large-data latency, or model quality. Anonymous/viewer/owner permission enforcement is separately covered through real local HTTP in `tests/test_project_module_web.py`.
