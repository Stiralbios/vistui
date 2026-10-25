# Phase 3 Planning Questions

## Project status

Phase 2 (Core CRUD for User POST, ChatGroup, Chat, Message, Event, Topic, Fact) is complete.
The last commit is `feat: implement core CRUD for ChatGroup, Chat, Message, Event, Topic, Fact`.
All Phase 2 routers are wired in `sources/backend/main.py`, the `0002_create_core_tables.py` migration exists,
and tests/factories have been created for every entity.

The project is ready to move into **Phase 3: Real search infrastructure** — filters, BM25/vector search APIs,
ranking formula integration, and memory retrieval endpoints.

## Unresolved questions for Phase 3

1. **Phase 3 scope**
   The plan lists filters, BM25/vector search APIs, ranking formula integration, and memory retrieval endpoints.
   Which of these belong in the *next* milestone, and which can wait for Phase 4 or later?

2. **Search stack**
   Since we already use ParadeDB, do we want to adopt `pg_search` + `sqlalchemy-paradedb` for BM25/hybrid search now,
   or stay with Postgres `tsvector` + `pgvector` only?

3. **Embeddings for Phase 3**
   Message/Event/Topic/Fact models already have `embedding Vector(768)` columns, but nothing populates them yet.
   Should Phase 3 use mock/fixed embeddings, or do we integrate a real embedding provider (and which model/dimension)?

4. **Ranking formula**
   `ChatGroup.ranking_formula` is currently a free string. What should it express?
   A simple weighted sum? A Python expression evaluated safely? A SQL expression? Or is it a placeholder for later?

5. **Memory retrieval contract**
   What should the retrieval API look like? For example:
   - Given a `chat_id` and a user message, return ranked events/topics/facts?
   - A generic `POST /api/v1/search` endpoint?
   - Something else?

6. **`POST /api/v1/users` intent**
   `plan1.md` said no public registration, but Phase 2 added an authenticated `POST /api/v1/users`.
   Is this meant for an admin/seeder creating users, or do we want public registration?

7. **Dependency cleanup**
   `plan2.md` mentions `sqlalchemy-pgvector-utils`, which does not appear to be a real package.
   Should that reference be removed and `sqlalchemy-paradedb` added instead?

## Notes

- External research recommends `pgvector` for vector columns and `sqlalchemy-paradedb` for ParadeDB BM25/hybrid integration.
- ParadeDB `pg_search` and `pgvector` are complementary (BM25 lexical + vector semantic, combinable via RRF).
