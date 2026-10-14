# Kitchen-sink DB: core architecture

Label: wayfinder:map

## Destination

A **core-engine architecture spec plus a phased roadmap** for a Rust database with SQL tables and MUMPS-style Globals, cross-model transactions, and a WAL, starting with a thin embedded vertical slice. Front-ends get only a tech-direction sketch in the roadmap.

## Notes

- Domain: database engine design. Vocabulary lives in `GLOSSARY.md`; ADRs in `docs/adr/`. Every grilling session also calls the `domain-modeling` skill.
- Purpose: mainly learning and craft, but good enough for real users to adopt. Write the core ourselves (storage, WAL, MVCC, transactions). Reuse crates where it's safe (parsing helpers, networking, UI frameworks).
- "Minimal" means a thin vertical slice first: an embedded engine with Globals plus basic SQL tables, cross-model transactions, WAL, and a CLI. Server mode, full-text search, JSON, backups, TUI, Web and GUI come in later phases.
- A single shared KV core under both models is **not** a premise. That's an open decision.
- Embedded mode: only one process may open the database files at a time. Multi-client access goes through Server mode.
- Single node, data may be larger than RAM, no replication.
- MUMPS dialect: a global-access language plus local variables and simple control flow. No full M routines.
- Server mode uses a **custom protocol**. One connection, session and transaction can carry both SQL and MUMPS-dialect statements.
- Constraints on future work that's out of scope:
  - Key/storage design must not rule out hierarchical fields in SQL tables later.
  - The protocol is specified independently of any language, so non-Rust clients can be written from the spec alone.
  - The SQL layer must not make a pgwire adapter impossible later.
- Platforms: Windows first, Linux second.

## Decisions so far

<!-- one line per resolved ticket: [title](issues/NN-slug.md): gist -->

- [How do existing systems put SQL and hierarchical data on one engine?](issues/01-sql-over-kv-prior-art.md): Every SQL-plus-hierarchy system maps rows, indexes and the catalog onto keys in one ordered store with one log, so cross-model atomicity comes free; separate-engine designs (MySQL, SQL Server) pay with two-phase commit across two logs or per-engine isolation rules.
- [How do real engines implement MVCC and the isolation levels?](issues/02-mvcc-isolation-prior-art.md): PG keeps versions in the heap (VACUUM), InnoDB rebuilds from undo, SQLite versions pages via the WAL; SI still allows write skew; Serializable is SSI in PG, 2PL in InnoDB, a single writer in SQLite; SSI costs <7% throughput and <0.25% aborts, its real cost is memory and complexity.
- [What can we learn from (or reuse among) Rust storage engines?](issues/03-rust-storage-engine-prior-art.md): No surveyed B+tree uses a WAL (redb/LMDB use copy-on-write commits); study redb (B+tree) and fjall (LSM), sled is beta; a B+tree with WAL and buffer pool fits our core best; no runtime dependency.
- [How do we get durable, correct file I/O on Windows (and Linux)?](issues/04-windows-durable-io.md): WAL with FILE_FLAG_WRITE_THROUGH (Linux O_DSYNC) and 4 KiB-aligned writes, FlushFileBuffers/fsync at checkpoints, checksums since sector writes aren't atomic, flush errors fatal, Embedded ownership via File::try_lock plus restrictive Windows share_mode, no mmap buffer pool.
- [Should full-text search use tantivy or a native inverted index?](issues/05-full-text-search-options.md): Native inverted index in our key space (FTS5/GIN-style: same txn, WAL, rollback) with unicode-segmentation + rust-stemmers and our own BM25; tantivy can't join our WAL/MVCC.

## Not yet specified

- WAL record format, checkpointing, and recovery procedure.
- The custom wire protocol: framing, auth, how SQL and MUMPS statements and results are carried.
- JSON type: storage representation, operators, indexing.
- How far the SQL dialect goes: which Postgres-inspired types, statements and functions are in which phase.
- Simple backup mechanism (online vs offline, consistency with the WAL).
- The command set for admin and maintenance tasks (CLI).
- How the catalog and schema are stored (and whether the catalog itself lives in Globals).

## Out of scope

- Hierarchical fields in SQL tables (hybrid model): deferred by the user; only constrains key design (see Notes).
- Incremental and continuous backups: deferred by the user.
- Client libraries for C/C++, .NET, Python, JS/TS: deferred; only the Rust client is in scope.
- PostgreSQL wire protocol compatibility: nice to have, not a requirement.
- Full M routines / stored M programs: the dialect stops at subset "c" (see Notes).
- Replication and clustering: single node only.
- UX design for TUI, Web and GUI: only the tech stack is decided here.
