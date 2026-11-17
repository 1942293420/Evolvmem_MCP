# Experience v2 SQL Migrations (Append-Only)

- **Control Plane (`em_control`)**: PostgreSQL control-plane migrations (`001`–`008` and service provisioning scripts) live append-only in `evolvmem/library_catalog/sql/` and are applied by `evolvmem/experience_v2/control/migrations.py` per the PostgreSQL Migration Plan (`Task 2` / Stage 1).
- **Personal Experience Store (`experience_v2`, Stage 2+)**: Per-user experience database schema migrations (`001_*.sql`, `002_*.sql`, ...) are appended in this directory (`evolvmem/experience_v2/migrations/`). Published migration files must never be modified in place.
