# Skill library

The administrator-only Web page at /#skills lists two independent snapshots.
It searches by skill name, purpose or source and displays stored instructions
and references as plain text. The catalog preserves official Git revisions
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
