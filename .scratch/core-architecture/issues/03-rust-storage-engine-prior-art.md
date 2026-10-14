# What can we learn from (or reuse among) Rust storage engines?

Type: research
Status: resolved
Blocked by: 

## Question

Survey Rust storage engines and their designs: sled, redb, fjall, and LMDB-style copy-on-write B+trees (heed/lmdb bindings), plus LSM designs in general. Compare B+tree and LSM for a single-node, larger-than-RAM, write-ahead-logged engine. For each, note maturity, Windows support, MVCC support, and whether it can be studied, forked or used as a reference while we write our own core.

## Answer

- **redb** (4.4.0, Oct 2026): copy-on-write B+trees inspired by LMDB, written in safe Rust. The file format is stable. It dropped mmap and has Windows CI plus Windows-specific fixes. MVCC gives one writer alongside concurrent readers, and it supports savepoints. It uses **no WAL**: commits flip a god byte between two commit slots and rely on Merkle checksums. It is the best B+tree code to study.
- **fjall** (3.1.12, Oct 2026): an LSM engine (one tree per keyspace) with a journal, snapshot reads, and single-writer or optimistic SSI transactions. It runs Windows CI. Its `lsm-tree` crate has no WAL, which is a clean boundary to study. It is the best LSM code to study. The author says feature work is winding down.
- **sled**: still beta. Stable is 0.34.7 and 1.0 is stuck in alpha. The on-disk format is unstable and the main branch is mid-rewrite. Use it only as a cautionary reference.
- **LMDB/heed**: the classic copy-on-write MVCC B+tree, with no WAL and no recovery step. It is C, needs an `unsafe` mmap, and requires a fixed map size. `mdb.master` now grows files incrementally on Windows. Read it for design, but don't adopt it.
- **No surveyed B+tree uses a WAL.** Shadow paging (LMDB/redb) avoids a WAL at the cost of a single writer and path copying. A WAL-based B+tree has to follow the ARIES-style buffer pool and checkpoint design instead.
- **B+tree vs LSM**: a B+tree gives predictable reads and cheap ordered scans, which suits `$ORDER` and SQL indexes. An LSM gives sequential writes and MVCC for free via sequence numbers, but adds compaction, read and space amplification, and tuning. For a learning-first engine with a mandated WAL, a B+tree with WAL and buffer pool keeps durability in one place.
- **Reuse**: everything is permissively licensed (MIT/Apache, OpenLDAP for LMDB). Study redb and fjall, and optionally use them as test-only oracles. Do not take a runtime dependency, per the decision to write the core ourselves.
- Open: whether the LMDB 0.9 release branch (vs `mdb.master`) grows files incrementally on Windows; no benchmarks were gathered.

Full findings: [research/rust-storage-engine-prior-art.md](../research/rust-storage-engine-prior-art.md)
