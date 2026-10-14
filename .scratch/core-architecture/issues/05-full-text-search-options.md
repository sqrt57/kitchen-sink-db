# Should full-text search use tantivy or a native inverted index?

Type: research
Status: resolved
Blocked by: 

## Question

Compare using tantivy with building a native inverted index stored in the engine's own key space. Criteria: transactional consistency with SQL/Global writes (visibility under MVCC, rollback), WAL integration, Unicode tokenization and stemming support, ranking, and implementation size. How do PostgreSQL (tsvector/GIN) and SQLite FTS5 integrate FTS with transactions?

## Answer

- Recommendation: build a native inverted index in the engine's own key space, and reuse crates only for linguistics: `unicode-segmentation` (UAX #29) and `rust-stemmers` (Snowball, 18 languages, the same stemmer tantivy uses).
- PostgreSQL: GIN is an ordinary WAL-logged access method that stores no visibility information. It returns candidate TIDs, the executor applies MVCC to the heap row, and VACUUM later removes dead TIDs. Rollback therefore needs no special handling. `fastupdate` buffers inserts in a pending list that every search must scan.
- PostgreSQL ranking (`ts_rank`, `ts_rank_cd`) does not use the index. It reads each matching row's tsvector ("can be I/O bound") and uses no corpus IDF.
- SQLite FTS5 stores the index in ordinary shadow tables (`%_data`, `%_idx`, `%_docsize`, ...) in the same file, so the pager's journal/WAL makes it atomic and durable along with every other table.
- FTS5 buffers postings in an in-memory hash during a transaction. It flushes the hash into a new immutable segment b-tree at `xSync` (and at savepoints), discards it on rollback, and merges segments in the background. Deletes become tombstones. It ranks with BM25 (k1=1.2, b=0.75), using stats kept in the shadow tables.
- tantivy is a separate storage engine with its own commit point (an atomic `meta.json` write), its own lockfile and its own files. Its snapshots are tantivy commits, so it cannot join our WAL or MVCC snapshots and gives no read-your-own-writes. It can only be an eventually-consistent derived index, recovered by replaying our WAL from an LSN stored in `PreparedCommit::set_payload`.
- tantivy's strengths are BM25 plus phrase, fuzzy and facet queries. Its default tokenizer only splits on non-alphanumeric characters (no UAX #29), so the native option can match or beat its tokenization with the same crates.
- Native option costs: we must build postings with positions, BM25 stats kept transactionally, cleanup of dead postings (vacuum, or segments plus tombstones), and handling of hot-key contention on popular terms. Full FTS5 is about 0.8 MB of C; a minimal version is much smaller.
- Visibility design is still open and depends on the "shared KV core" decision: either check candidate rows against the snapshot (the PostgreSQL way) or rely on MVCC-versioned posting keys.
- Not checked: GIN's exact WAL record types, tantivy's mmap behavior on Windows, which Unicode normalization crate to use.

Full findings: [research/full-text-search-options.md](../research/full-text-search-options.md)
