# Rust storage engine prior art: sled, redb, fjall, LMDB/heed, LSM vs B+tree

Ticket: `.scratch/core-architecture/issues/03-rust-storage-engine-prior-art.md`
Researched: 2026-10-14. Versions and dates come from the crates.io API and GitHub on that day.

## Question

Survey Rust storage engines and their designs: sled, redb, fjall, and LMDB-style copy-on-write B+trees (heed/lmdb bindings), plus LSM designs in general. Compare B+tree and LSM for a single-node, larger-than-RAM, write-ahead-logged engine. For each, note maturity, Windows support, MVCC support, and whether we can study it, fork it, or use it as a reference while we write our own core.

## Summary table

| Engine | Structure | Latest version (date) | Maturity | Windows CI | MVCC / txns | WAL? | License | Best use for us |
|---|---|---|---|---|---|---|---|---|
| sled | Lock-free Bw-tree-like B+tree on a log-structured store | stable 0.34.7; 1.0.0-alpha.124 (2024-10-11) | Beta. On-disk format unstable, main branch mid-rewrite | yes | Optimistic serializable txns. MVCC not advertised | Log-structured, no separate WAL | MIT/Apache-2.0 | Cautionary reference only |
| redb | Copy-on-write B+trees (LMDB-inspired), no mmap | 4.4.0 (2026-10-13) | "Stable and maintained", file format stable | yes | Readers run concurrently with one writer (MVCC). Savepoints | **No WAL.** Shadow paging plus double commit slots | MIT/Apache-2.0 | Best B+tree code to study. Forkable |
| fjall (+ `lsm-tree`) | LSM-tree, one tree per keyspace, journal | 3.1.12 (2026-10-03) | v3 released 2026-01. Author says feature work is winding down | yes | Snapshot reads. Single-writer or optimistic SSI txns | Yes (journal). `lsm-tree` alone has none | MIT/Apache-2.0 | Best LSM code to study. `lsm-tree` is a WAL-less building block |
| LMDB via heed | Copy-on-write B+tree, mmap, C library | heed 0.22.1 | LMDB is decades-old and battle-tested. heed is used by Meilisearch | yes (heed tests) | Readers never block. One writer (MVCC) | **No WAL**, no recovery step | LMDB: OpenLDAP license. heed: MIT | Classic design reference. Not Rust-native and needs `unsafe` |

## sled

- Maturity: the README says sled is "quite young, should be considered unstable" and adds "if reliability is your primary constraint, use SQLite. sled is beta." It also warns that "the on-disk format is going to change in ways that require manual migrations before the 1.0.0 release!" ([README](https://github.com/spacejam/sled))
- Releases: the newest stable version is 0.34.7. The 1.0 line has stayed in alpha (latest 1.0.0-alpha.124, 2024-10-11) ([crates.io API](https://crates.io/api/v1/crates/sled)). The main branch holds "a large in-progress rewrite" built on the `marble` storage engine (komora project) ([README](https://github.com/spacejam/sled)). Commits were still landing in April 2026 ([GitHub commits](https://api.github.com/repos/spacejam/sled/commits?per_page=3)).
- Space: the README admits "sled uses too much space sometimes" ([README](https://github.com/spacejam/sled)).
- Transactions: "serializable (ACID) transactions" across keyspaces. They are optimistic, so closures may be retried ([README](https://github.com/spacejam/sled)).
- Windows: CI runs `cargo test` on `ubuntu-latest`, `macos-latest` and `windows-latest` ([test.yml](https://raw.githubusercontent.com/spacejam/sled/main/.github/workflows/test.yml)).
- Verdict: interesting for lock-free techniques and as a lesson in scope creep. A poor base to fork or copy from, because the format is unstable and the design is being replaced.

## redb

- Design: "a set of copy-on-write B+trees", inspired by LMDB ([README](https://github.com/cberner/redb)).
- Maturity: "Stable and maintained" and "The file format is stable" ([README](https://github.com/cberner/redb)). Latest version 4.4.0 (2026-10-13). The 2.6.x line still gets patches ([crates.io API](https://crates.io/api/v1/crates/redb)). Format changes happened at 2.0 and 3.0 (v2 support was dropped, with `Database::upgrade()` as the migration path). 4.0 had no format change ([CHANGELOG](https://raw.githubusercontent.com/cberner/redb/master/CHANGELOG.md)).
- No mmap: the mmap backend was removed in 0.14.0 because it could not be proven sound. Only a safe `File` backend with a user-space cache remains ([CHANGELOG](https://raw.githubusercontent.com/cberner/redb/master/CHANGELOG.md)). This matters for us: we want larger-than-RAM data, Windows support, and safe Rust.
- Commit protocol ([design.md](https://github.com/cberner/redb/blob/master/docs/design.md)):
  - A one-byte "god byte" and two commit slots. "redb relies on the fact that this is a single byte to perform atomic commits."
  - The default commit is 1PC+C: one `fsync`, plus a checksum. "All data is checksumed when written, using a non-cryptographic Merkle tree with XXH3_128". 2PC is available for untrusted input.
  - Non-durable commits are tracked by an in-memory flag. A crash rolls them back to the last durable commit.
  - Allocation uses a buddy allocator inside regions, so the file grows region by region.
  - Recovery falls back to the secondary slot and rebuilds the allocator state by walking the trees. Optional "quick-repair" persists the allocator state on each commit instead (added in 2.3.0).
- No write-ahead log: atomicity comes from shadow paging and the commit slots ([design.md](https://github.com/cberner/redb/blob/master/docs/design.md)).
- MVCC: "MVCC support for concurrent readers & writer, without blocking" ([README](https://github.com/cberner/redb)). A reader pins a root. Freed pages are "only reused after all read transactions that could reference it have completed" ([design.md](https://github.com/cberner/redb/blob/master/docs/design.md)). Savepoints work by keeping a snapshot root plus a copy of the allocator state.
- Windows: CI matrix includes `windows-latest` ([ci.yml](https://api.github.com/repos/cberner/redb/contents/.github/workflows/ci.yml)). The changelog shows active Windows fixes: dynamic file growth (0.9.0), file shrinking (0.15.0), a hang on truncated files (4.2.0) and a hang on zero-byte writes (4.3.0) ([CHANGELOG](https://raw.githubusercontent.com/cberner/redb/master/CHANGELOG.md)).
- License: MIT or Apache-2.0, so we can fork or copy code with attribution.
- Verdict: the most relevant code to read for a B+tree page layout, a page allocator, checksums, and a Windows file backend in safe Rust. Its durability model (shadow paging, one writer, no WAL) is a different design from the WAL-based engine our map assumes.

## fjall and lsm-tree

- Design: "Log-structured, embeddable key-value storage engine written in Rust", "similar to RocksDB". "Each keyspace is its own physical LSM-tree". The engine has a journal plus one memtable per keyspace ([README](https://github.com/fjall-rs/fjall)).
- Durability: by default, writes reach OS buffers but not disk. `persist()` forces durability ([README](https://github.com/fjall-rs/fjall)).
- Transactions: `SingleWriterTxDatabase` is serializable because only one write transaction runs at a time. `OptimisticTxDatabase` gives multi-writer serializable transactions through OCC ([README](https://github.com/fjall-rs/fjall)). Fjall 3.0 made these separate exported types ([Fjall 3.0 post](https://fjall-rs.github.io/post/fjall-3/)).
- MVCC: the underlying `lsm-tree` provides "Multi-versioning of KVs, enabling snapshot reads" ([lsm-tree README](https://github.com/fjall-rs/lsm-tree)). In v3, tree state is a copy-on-write `SuperVersion`, and old versions are freed only once no snapshot holds them ([Fjall 3.0 post](https://fjall-rs.github.io/post/fjall-3/)).
- What v3 added ([Fjall 3.0 post](https://fjall-rs.github.io/post/fjall-3/)):
  - Blocks with embedded binary-search indexes, prefix truncation and an optional hash index.
  - XXH3-128 checksums on by default.
  - Partitioned filters, and leveled compaction that promotes data straight to L6.
  - Key-value separation with blob GC folded into compaction.
  - Compaction filters, added in 3.1.
  - Fuzz and mutation testing, and a V2-to-V3 migration tool.
  - The author also says "active development on new features will mostly wind down going into 2026."
- `lsm-tree` "only provides a primitive LSM-tree, not a full storage engine" and "does not ship with a write-ahead log" ([lsm-tree README](https://github.com/fjall-rs/lsm-tree)). That separation is exactly the boundary we would need if our own WAL fronted an LSM.
- Maturity: version 3.1.12 (2026-10-03) ([crates.io API](https://crates.io/api/v1/crates/fjall)). Breaking changes bump the major version and come with a migration path ([README](https://github.com/fjall-rs/fjall)).
- Windows: tests run on `ubuntu-latest`, `windows-latest` and `macos-latest` ([test.yml](https://raw.githubusercontent.com/fjall-rs/fjall/main/.github/workflows/test.yml)).
- License: MIT or Apache-2.0.
- Verdict: the best Rust LSM to study for SSTable/block format, compaction, snapshot versioning and OCC transactions.

## LMDB and heed

- Design, quoted from [lmdb.h](https://raw.githubusercontent.com/LMDB/lmdb/mdb.master/libraries/liblmdb/lmdb.h):
  - "Data pages use a copy-on-write strategy so no active data pages are ever overwritten"
  - "The database structure is multi-versioned so readers run with no locks; writers cannot block readers"
  - It "eliminates the need of any special recovery procedures after a system crash."
- Caveats listed in the same header:
  - "Avoid long-lived transactions", because they stop freed pages from being reused, so the file grows.
  - No remote filesystems.
  - A broken lock file causes sync problems.
- heed: "Rust-centric LMDB abstractions with minimal overhead". It builds the `mdb.master` branch, or `mdb.master3` for encryption at rest ([README](https://github.com/meilisearch/heed)). `EnvOpenOptions::open` is `unsafe` because "Memory map constructors are marked `unsafe` because of the potential for Undefined Behavior (UB)" if the file is modified underneath ([docs.rs, heed 0.22.1](https://docs.rs/heed/latest/heed/struct.EnvOpenOptions.html)).
- Windows:
  - heed's CI runs its tests on `windows-latest` ([rust.yml](https://raw.githubusercontent.com/meilisearch/heed/main/.github/workflows/rust.yml)).
  - Historically, on Windows the data file was preallocated to the full map size ([openldap-technical thread](https://lists.openldap.org/hyperkitty/list/openldap-technical@openldap.org/thread/CEQQQPEX524CW2ATKBPB5OQDIZ4H5UU7)).
  - The `mdb.master` source now says: "We use native NT APIs to setup the memory map, so that we can let the DB file grow incrementally instead of always preallocating the full size." ([mdb.c, mdb.master](https://raw.githubusercontent.com/LMDB/lmdb/mdb.master/libraries/liblmdb/mdb.c)).
  - I did not confirm whether the 0.9 release branch has this.
- Map size: the map size must be set up front, and `MDB_MAP_FULL` is returned when it is exceeded ([lmdb.h](https://raw.githubusercontent.com/LMDB/lmdb/mdb.master/libraries/liblmdb/lmdb.h)).
- Verdict: the canonical design to read for a copy-on-write B+tree with MVCC. It is C, so it is not a fork candidate for a "write the core ourselves" Rust project. redb is effectively its safe-Rust descendant.

## B+tree vs LSM for our engine (single node, larger than RAM, WAL)

- LSM origin: the LSM-tree batches index changes in memory and merges them into disk components to cut random I/O versus B-trees on insert-heavy workloads ([O'Neil et al., Acta Informatica 33(4), 1996](https://www.cs.umb.edu/~poneil/lsmtree.pdf)).
- The trade-off vocabulary, quoted from the [RocksDB Tuning Guide](https://github.com/facebook/rocksdb/wiki/RocksDB-Tuning-Guide):
  - Write amplification: "the ratio of bytes written to storage versus bytes written to the database".
  - Read amplification: "the number of disk reads per query".
  - Space amplification: "the ratio of the size of database files on disk to data size".
  - Leveled compaction can have high write amplification. Universal compaction lowers it but "always increases space amplification".
- My synthesis, not a quote from any source:
  - **B+tree:**
    - Reads are predictable: one root-to-leaf path, and range scans are cheap. Both matter for ordered Global traversal (`$ORDER`/`$QUERY`) and for SQL index scans.
    - In-place updates need a WAL plus a buffer pool and checkpoints (ARIES style). The alternative is shadow paging (LMDB/redb), which needs no WAL but allows only one writer and makes every commit copy the path to the root.
    - There are no background compaction stalls.
    - Simpler to get correct, and easier to teach.
  - **LSM:**
    - Sequential writes and good compression.
    - The memtable plus the WAL *is* the write path, so a WAL comes naturally.
    - MVCC falls out of sequence numbers on keys.
    - The cost is compaction complexity, read amplification (bloom filters needed), and tuning.
    - Range scans must merge several levels.
  - For a learning-focused engine whose map already commits to a WAL and cross-model transactions, a **B+tree with WAL + buffer pool** keeps durability in one place (the WAL). redb and LMDB show the shadow-paging alternative but do not demonstrate WAL integration. Neither surveyed B+tree uses a WAL.
  - Hierarchical Global keys (prefix-heavy, ordered) fit either structure if keys are order-preserving encodings. Prefix truncation (fjall v3 blocks) and prefix compression in B+tree pages both help here.

## Reuse stance

- Study, don't depend on them: the map says we write storage, WAL, MVCC and transactions ourselves. All four projects are permissively licensed (MIT/Apache-2.0, or OpenLDAP for LMDB), so we may copy and adapt code with attribution if useful.
- redb: the study target for a B+tree page format, a buddy allocator, Merkle checksums, a safe file I/O backend, and Windows file-growth fixes.
- fjall/`lsm-tree`: the study target if we pick an LSM, or for an LSM-backed secondary structure. Read it for block format, compaction, `SuperVersion` snapshotting, and SSI via OCC.
- LMDB: read `lmdb.h`/`mdb.c` for the classic copy-on-write MVCC and reader-table design. Do not adopt it: it is C, needs `unsafe` mmap, and uses a fixed map size.
- sled: avoid as a reference for durability or format. It is still beta and mid-rewrite.
- Differential testing: redb or fjall could serve as an oracle in tests. A test-only dependency doesn't conflict with writing the core ourselves.

## Open points not resolved here

- Whether the LMDB 0.9 release branch (as opposed to `mdb.master`) grows files incrementally on Windows.
- Quantitative benchmarks: none were taken from primary sources. Vendor benchmarks were deliberately skipped.
