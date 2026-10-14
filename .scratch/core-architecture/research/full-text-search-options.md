# Full-text search: tantivy or a native inverted index?

Ticket: `.scratch/core-architecture/issues/05-full-text-search-options.md`
Researched: 2026-10-14. Versions checked: tantivy 0.26.2 (docs.rs), PostgreSQL current docs, SQLite FTS5 `master` source.

## Question

Compare tantivy with a native inverted index stored in the engine's own key space, on:
transactional consistency with SQL/Global writes (MVCC visibility, rollback), WAL integration,
Unicode tokenization and stemming, ranking, and implementation size. How do PostgreSQL
(tsvector/GIN) and SQLite FTS5 integrate FTS with transactions?

## Short answer

Build a **native inverted index in the engine's key space**, modelled on FTS5 (in-transaction
buffer of pending postings, flushed into ordinary engine pages on commit, so the WAL and
rollback come for free) and on PostgreSQL (index holds no visibility information; candidates are
checked against the base row's MVCC visibility). Borrow **crates for the linguistics only**:
`unicode-segmentation` (UAX #29 word boundaries) and `rust-stemmers` (Snowball). Implement BM25
ourselves; it is a small formula over statistics we have to store anyway.

tantivy is an excellent search library, but it is a second storage engine with its own commit
point (`meta.json`), its own lock, and its own files. It cannot join our WAL or our MVCC snapshots,
so transactional FTS would need a two-engine commit protocol plus crash reconciliation. That
contradicts the project's "write the core ourselves" goal and its cross-model transaction premise.

## How PostgreSQL integrates FTS with transactions

- **The index is an ordinary access method; it knows nothing about visibility.** "The access method is
  _not_ involved in actually fetching those tuples from the index's parent table, nor in determining
  whether they pass the scan's visibility test or other conditions."
  ([Index scanning](https://www.postgresql.org/docs/current/index-scanning.html))
  GIN returns heap TIDs; the executor applies MVCC to the heap tuple. Rollback needs nothing special:
  index entries pointing at aborted tuples are simply invisible and later removed by VACUUM.
- **Vacuum cleans the index.** GIN vacuum "traverses entry tree leafs in logical order by rightlinks and
  removes deletable TIDs from posting lists"; "Vacuum never deletes tuples or pages from the entry tree."
  ([GIN README](https://github.com/postgres/postgres/blob/master/src/backend/access/gin/README))
- **WAL**: GIN changes are WAL-logged like any core index (README lists "Write-Ahead Logging (WAL)
  (Recoverability from crashes.)"). The index lives in the same page/buffer/WAL machinery as tables.
- **Write amplification and the pending list.** With `fastupdate`, new entries go to "a temporary,
  unsorted list of pending entries", merged into the main structure on vacuum/autoanalyze,
  `gin_clean_pending_list()`, or when it exceeds `gin_pending_list_limit`. Searches "must scan the list
  of pending entries in addition to searching the regular index".
  ([GIN](https://www.postgresql.org/docs/current/gin.html))
- **Lossiness / recheck.** GIN stores "only the words (lexemes) of tsvector values, and not their weight
  labels. Thus a table row recheck is needed when using a query that involves weights." GiST is lossy
  (fixed-length signatures) and always may need a recheck.
  ([Text search indexes](https://www.postgresql.org/docs/current/textsearch-indexes.html))
  The AM contract: if the index reports lossy, "the core system's index-scan machinery will then apply
  the index conditions again to the heap tuple". ([Index scanning](https://www.postgresql.org/docs/current/index-scanning.html))
- **Ranking is not index-assisted.** `ts_rank`/`ts_rank_cd` take the row's tsvector; "Ranking can be
  expensive since it requires consulting the tsvector of each matching document, which can be I/O bound
  and therefore slow." No global corpus statistics (no IDF) are used; normalization is by document length
  / unique-word count. ([Controlling text search](https://www.postgresql.org/docs/current/textsearch-controls.html))

## How SQLite FTS5 integrates FTS with transactions

- **Index stored in ordinary tables of the same database file.** "Between 3 and 5 real tables are created
  ... known as 'shadow tables'": `%_data` ("most of the full-text index data"), `%_idx`, `%_config`,
  `%_docsize`, `%_content`. ([FTS5 §9](https://www.sqlite.org/fts5.html)) Because they are normal
  b-tree tables, the pager's journal/WAL makes them atomic and durable together with every other table.
- **LSM-like segments.** Entries live in "a series of immutable b-tree like structures referred to as
  'segment b-trees'"; "Each time a write to the FTS5 table is committed, one or more (but usually just one)
  new segment b-trees are added containing both the new entries and tombstones for any deleted entries."
  Readers query each segment and merge, "giving priority to newer data". Merging is controlled by
  `automerge`, `crisismerge`, `usermerge`. ([FTS5](https://www.sqlite.org/fts5.html))
- **In-transaction buffer.** Postings accumulate in an in-memory hash (`Fts5Index.pHash`,
  `nPendingData`), flushed by `fts5IndexFlush` when it exceeds `nHashSize` or when rowids arrive
  out of order (`sqlite3Fts5IndexBeginWrite`). ([fts5_index.c](https://github.com/sqlite/sqlite/blob/master/ext/fts5/fts5_index.c))
- **Hooked into the virtual-table 2PC.** `xSync` is "the start of a two-phase commit"; "the xSync method
  on all virtual tables is invoked prior to invoking the xCommit method on any virtual table. If any of the
  xSync methods fail, the entire transaction is rolled back." ([Virtual tables](https://www.sqlite.org/vtab.html))
  FTS5's `fts5SyncMethod` calls `sqlite3Fts5FlushToDisk` (writes the pending hash into `%_data` inside
  the still-open transaction). `fts5RollbackMethod` -> `sqlite3Fts5StorageRollback` -> `sqlite3Fts5IndexRollback`,
  which discards the hash and invalidates cached structure because "the contents of the %_data table may
  have changed on disk". `fts5SavepointMethod` flushes to disk at each savepoint so that
  `xRollbackTo` can rely on the pager to undo shadow-table writes.
  ([fts5_main.c](https://github.com/sqlite/sqlite/blob/master/ext/fts5/fts5_main.c))
- **External content** tables have no `%_content`; the application must keep source table and index in
  sync (typically with triggers). ([FTS5](https://www.sqlite.org/fts5.html))
- **Tokenizers**: `unicode61` (default; tokens are Unicode categories L*, N*, Co; case folding per
  Unicode 6.1; diacritic removal), `ascii`, `porter` ("designed for use with English language terms only"),
  `trigram` (substring matching, needs >=3 chars). No UAX #29 segmentation, no CJK word segmentation.
- **Ranking**: built-in `bm25()` with k1=1.2, b=0.75 hard-coded, per-column weights; better matches get
  numerically smaller scores. Corpus stats (row count, average lengths) are kept in the `%_data` "averages"
  record and `%_docsize`. ([FTS5](https://www.sqlite.org/fts5.html))
- **Size**: core FTS5 C sources (excluding Tcl/test harness files) are roughly 0.8 MB; `fts5_index.c`
  alone is ~8,700 lines and `fts5_main.c` ~3,600 lines (sizes from the GitHub contents API, 2026-10-14).

Note the difference: SQLite has a single writer and no MVCC snapshots inside a database, so FTS5 only
needs atomicity. Our engine has MVCC, so we must also answer the PostgreSQL question (visibility).

## tantivy

- **Architecture**: "A tantivy index is a collection of smaller independent immutable segments." "On
  commit, one segment per indexing thread is written to disk, and the `meta.json` is then updated
  atomically." Deletes are term-based and written as new alive-bitset files on commit; background
  threads merge segments. ([ARCHITECTURE.md](https://github.com/quickwit-oss/tantivy/blob/main/ARCHITECTURE.md))
- **Commit/rollback** (0.26.2): `commit()` blocks until changes "are published and persisted";
  `rollback()` "cancels all of the updates that happened after the last commit"; `prepare_commit()` gives
  a `PreparedCommit` with `commit()`/`abort()` and `set_payload()`. `delete_term` "only affects documents
  that were added in previous commits". Opstamps are an increasing `u64` "that can be used by the client to
  align commits with its own document queue."
  ([IndexWriter](https://docs.rs/tantivy/latest/tantivy/indexer/struct.IndexWriter.html),
  [PreparedCommit](https://docs.rs/tantivy/latest/tantivy/indexer/struct.PreparedCommit.html))
  `IndexMeta.payload` is a small string stored with the commit, "entirely unused by tantivy"
  ([IndexMeta](https://docs.rs/tantivy/latest/tantivy/index/struct.IndexMeta.html)) - this is the hook for
  recording "our WAL LSN up to which this index is current".
- **Single writer per index** via a lockfile: `writer_with_num_threads` returns `DirectoryLockBusy` if the
  lockfile exists; a stale lock can remain after a panic.
  ([Index](https://docs.rs/tantivy/latest/tantivy/index/struct.Index.html))
- **Storage is pluggable but file-shaped.** The `Directory` trait requires `get_file_handle`, `delete`,
  `exists`, `open_write`, `atomic_read`, `atomic_write` ("reads can never observe a partially written
  file"), `sync_directory`, `watch`. Files are immutable once opened for read.
  ([Directory](https://docs.rs/tantivy/latest/tantivy/directory/trait.Directory.html))
  One could implement `Directory` over our page store, but that only stores blobs: tantivy's commit
  point is still its own `atomic_write("meta.json")`, outside our transaction.
- **Visibility**: a `Searcher` is a snapshot of committed segments. There is no notion of our MVCC
  snapshot, so uncommitted rows of the current transaction are invisible (no read-your-own-writes), and
  rows committed in our engine but not yet in tantivy (or vice versa after a crash) diverge.
- **Tokenization**: `SimpleTokenizer` splits on `!c.is_alphanumeric()` (Unicode-aware chars, but no UAX #29;
  a run of CJK ideographs becomes one token) ([source](https://docs.rs/tantivy/latest/src/tantivy/tokenizer/simple_tokenizer.rs.html)).
  Filters: `LowerCaser`, `Stemmer`, `StopWordFilter`, `AsciiFoldingFilter`, `RemoveLongFilter`,
  `AlphaNumOnlyFilter`, `SplitCompoundWords`; `NgramTokenizer`, `RegexTokenizer`.
  ([tokenizer](https://docs.rs/tantivy/latest/tantivy/tokenizer/index.html)) Stemmer languages: 18
  (Arabic, Danish, Dutch, English, Finnish, French, German, Greek, Hungarian, Italian, Norwegian, Portuguese,
  Romanian, Russian, Spanish, Swedish, Tamil, Turkish), via optional dependency `rust-stemmers`.
  ([Language](https://docs.rs/tantivy/latest/tantivy/tokenizer/enum.Language.html))
- **Ranking**: BM25, "not configurable" per ARCHITECTURE.md; plus phrase, fuzzy, facets, fast fields -
  far richer query features than we would build early.

## Reusable linguistics crates (for the native option)

- `rust-stemmers`: "rust implementations for some stemmer algorithms written in the snowball language";
  MIT / BSD-3-Clause. Same stemmer tantivy uses. ([docs](https://docs.rs/rust-stemmers/latest/rust_stemmers/))
- `unicode-segmentation`: "Grapheme Cluster, Word and Sentence boundaries according to Unicode Standard
  Annex #29"; `unicode_words()` yields word segments containing alphabetic/numeric characters.
  ([docs](https://docs.rs/unicode-segmentation/latest/unicode_segmentation/))
  (Case folding/normalization: `str::to_lowercase` in std; NFKC would need e.g. `unicode-normalization` -
  not verified here.)

## Comparison

| Criterion | tantivy | Native inverted index in our key space |
|---|---|---|
| Atomic with SQL/Global writes | No. Separate commit point (`meta.json`). Needs 2PC-ish glue: commit our txn, then tantivy; on recovery replay from our WAL using the LSN stored in `payload`. Index is eventually consistent. | Yes. Postings are ordinary key/value writes in the same transaction. |
| MVCC visibility | Snapshot of tantivy commits only; no read-your-own-writes; can lag/lead our snapshot. Must post-filter hits against our MVCC anyway. | Same as PostgreSQL: index entries carry no visibility; fetch candidate row and apply our snapshot (or store versioned postings). |
| Rollback | Separate `rollback()`; cross-engine rollback after tantivy commit is impossible, so ordering + idempotent replay required. | Free: undo/WAL of the KV layer. Discard in-memory pending buffer like FTS5. |
| WAL integration | None; its own fsync'd files. Our WAL must remain the source of truth and tantivy a rebuildable derived index. | Free: index pages are logged like any other. |
| Crash recovery | Reindex from WAL tail (or full rebuild) on mismatch. | Normal recovery. |
| Vacuum/cleanup | Its own merges and alive bitsets. | Must remove dead postings (PG-style vacuum) or use segment+tombstone merges (FTS5-style). |
| Unicode tokenization | Alphanumeric split, no UAX #29; extendable with custom tokenizers. | Use `unicode-segmentation` (UAX #29) - better than tantivy's default. |
| Stemming | 18 Snowball languages via `rust-stemmers`. | Same crate, same 18 languages. |
| Ranking | BM25 + rich query features (phrase, fuzzy, facets). | BM25 is a small formula; needs per-doc length and per-term doc frequency + global doc count/avg length kept transactionally (FTS5 keeps these in `%_docsize` and the averages record). Phrase queries need positions in postings. |
| Implementation size | Small glue, but a large dependency (second storage engine, merge threads, mmap files, own lockfile). | Medium: FTS5 is ~0.8 MB of C for a full-featured version; an MVP (term -> sorted doc-id postings with positions, AND/OR/phrase, BM25) is much smaller. |
| Fit with project goals | Conflicts with "write the core ourselves" and "one process opens the files" (extra lockfile/files to manage). | Fits: exercises the same KV/WAL/MVCC core; also usable from Globals. |

## Design sketch for the native option (to be decided in later tickets)

- Key layout in our key space, e.g. `fts/<index-id>/term/<term>/<doc-id>` -> positions, plus
  `fts/<index-id>/doc/<doc-id>` -> length and `fts/<index-id>/stats` -> doc count / total length.
  A Global-style layout (`^FTS(index, term, docid) = positions`) maps directly onto MUMPS Globals.
- Per-transaction pending buffer (FTS5's `pHash`), flushed into the KV store at commit (or spilled earlier
  when large), discarded on rollback, flushed at savepoints.
- Visibility: either check each candidate row against the snapshot (PostgreSQL) or rely on the KV layer
  versioning the posting keys themselves, if the shared KV core is MVCC (ties to the open "shared KV core"
  decision).
- Hot-term write contention: stats keys and popular-term posting lists are hotspots under concurrent
  writers; FTS5 avoids it with single-writer + per-commit segments, PostgreSQL with the pending list.
  Choosing between "per-commit segments + merge" and "per-key postings" is a later design ticket.
- tantivy remains a possible later option as an explicitly *asynchronous, rebuildable* secondary index
  (store our commit LSN in `PreparedCommit::set_payload`), if rich search features are ever wanted.

## Open points not verified here

- Exact WAL record types for GIN (only the README's high-level statement was checked).
- tantivy behaviour on Windows (mmap + file deletion while open) - not researched.
- A Unicode normalization crate choice (NFKC/case folding) for the native tokenizer.
