# Skill library

The administrator-only Web page at /#skills lists two independent snapshots.
It displays a responsive full-width card grid. Cards use Chinese purpose titles
and summaries, with original names retained. Details default to a Chinese usage
explanation; original instructions and references remain available as inert plain
text. Search matches Chinese titles, summaries and content as well as original
names, descriptions and sources. The catalog preserves official Git revisions
and available upstream license files. Skill text and assets are inert data.

Official refresh tracks five fixed GitHub directories. It includes the
separately versioned Vercel Web Interface Guidelines document. The refresher
checks main revisions, reuses unchanged entries, and replaces the catalog
only after a complete valid fetch. Run it on the Linux owner host:

    python -B -m evolvmem.skill_library official

Windows refresh collects real Skill directories in Codex, Codex system,
Agents and active local Hermes skill roots. It skips links and backups,
limits supporting files to text in skill directories, and screens common
credential patterns. The verified Windows installation has a separate
scheduled job; it uses an existing OpenSSH identity in batch mode and uploads
a bounded snapshot to the Linux owner host. The server writes both the
private EvolvMem copy and the existing integration-platform artifact
directory atomically, then verifies the destination bytes. Platform readers
use the Windows mirror only. The collector reads installed skill roots and
does not consume any platform-origin input.

Linux runs its official refresh daily at 03:40 Asia/Shanghai using
evolvmem-skill-library.timer. The Windows scheduled job runs daily at 03:50
local time and on the owner's logon, with missed runs caught up when available.
The Windows last-success receipt lives under the current user's local
EvolvMem SkillLibrary folder. Linux logs the GitHub refresh service separately.
Invalid snapshots cannot replace valid data. An upstream fetch failure retains the previous official snapshot.

## Display translations

Chinese explanations are separate display-only metadata in
`presentation.zh-CN.json` beside the catalog snapshots. They do not rewrite or
execute any original skill files. The schema is `{schema:1, locale:"zh-CN",
entries:{<skill-id>:{title_zh,summary_zh,content_zh,source_sha256}}}`.
The hash binds each explanation to the UTF-8 content of its original SKILL.md.
A source update with different content suppresses the old explanation until
it is reviewed again; the UI falls back to the original and marks the Chinese
explanation as needing an update. Invalid or absent optional metadata does not
hide the source catalog. Search only uses matching explanations.

The platform receives a separate identical presentation file alongside its
Windows snapshot and validates the same hash. Scheduled raw-source sync never
overwrites this file and never consumes platform input. Presentation data for
private skills is held outside the public source repository. Usage explanations
are concise Chinese descriptions, not executable translations or full copies
of the original instructions.


## Purpose categories

The EvolvMem gallery combines a purpose category with the keyword and source
filters. Category facets count matches after keyword/source filtering and before
the category filter, so other categories remain available. A Skill can belong to
up to three categories; category counts are not summed into the unique total.

The optional private `categories.zh-CN.json` sidecar uses
`{schema:1, entries:{<id>:{source_sha256,category_ids}}}`. Category IDs are
frontend, development, testing, business, feishu, ziniao, visual, system, skills.
Every classification binds to the exact SKILL.md content hash. Missing, invalid
or stale classifications appear under uncategorized, retaining the original
Skill and its content. Native sync never overwrites this sidecar. Classification
does not rewrite source files, descriptions or the separate Chinese explanations.
