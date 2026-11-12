# User-library navigation consolidation

The user-library overview is the single owner-selection entry. History and experience show a static owner context and an explicit access badge; account administration and working principles remain global. Selecting an owner does not change the login actor. Administrator self browsing retains management, while viewing another account remains explicit read-only inspection.

The browser regression was observed failing on the unchanged UI: 16 expected failures and 3 existing protections passing. After implementation, 21 focused scenarios passed with no browser JavaScript errors. The existing product-module suite also passed all eight scenario groups, including six pages at 390, 768, 1024, and 1440 pixels, independent directed grants, revocation, self deletion/restoration, and member access denial. Desktop and mobile screenshots were reviewed. Every test browser was closed by its runner.

The first combined Python regression run reported 155 passes and one failure in `test_retired_history_http_is_closed_for_all_identities`. The unchanged baseline `81b185ec07673ba0eb205f9465de0b3cc76562e1` reproduced that failure in a separate source archive and temporary registry. Its fixture builds the handler before changing the product mode; a mode mismatch correctly closes session storage and returns 503. Aligning the temporary startup policy with that test's intended compatibility mode passed the same assertion without source changes. This change does not modify the backend, its tests, authentication, or policy enforcement.

The final run of the same nine Python suites used explicit temporary startup policy and registry paths, with every assertion retained: **156 passed in 14.92 seconds**. No tests were skipped to obtain this result.

Production installation is limited to reviewed static assets, the related documentation, and the focused test. The Web host loads static assets per request, so no restart is required. Existing login sessions, account mappings, directed grants, data-source mappings, and original history/experience stores are preserved.
