# MVCC and isolation levels: prior art

Question (ticket `.scratch/core-architecture/issues/02-mvcc-isolation-prior-art.md`): how do PostgreSQL (including SSI), InnoDB, SQLite (WAL mode) and the Cahill et al. SSI paper implement MVCC, snapshot isolation and serializable isolation? What anomalies does each level permit in practice? What does true serializability cost compared with SI?

Researched 2026-10-14. Every claim is cited. `[PG-iso]` style tags refer to the Sources list at the bottom.

## TL;DR

- All three engines give readers a **snapshot** so that readers never block writers. They differ in **where old versions live**:
  - PostgreSQL keeps every row version in the heap and garbage-collects with VACUUM.
  - InnoDB updates rows in place and rebuilds old versions from **undo logs**.
  - SQLite works at page granularity: unchanged pages stay in the database file, newer page images sit in the WAL, and each reader's "end mark" picks its version.
- **Snapshot Isolation (SI)** stops dirty reads, non-repeatable reads, phantoms and (with first-updater-wins) lost updates. It still allows **write skew** and the **read-only transaction anomaly**.
- PostgreSQL's REPEATABLE READ is true SI. InnoDB's REPEATABLE READ is not: its snapshot covers plain SELECTs only, while DML and locking reads act on the *latest* committed data. Within one transaction the result is a mix of snapshot reads and current reads.
- Serializable is implemented three different ways:
  - **PostgreSQL**: SSI. It runs on SI, tracks rw-antidependencies with non-blocking SIREAD "locks", and aborts when it finds two adjacent rw edges.
  - **InnoDB**: lock-based. Plain SELECT becomes `SELECT ... FOR SHARE` (with next-key/gap locks) when autocommit is off.
  - **SQLite**: only one writer at a time, which is trivially serializable. Its WAL readers get SI, and a read transaction that tries to upgrade after someone else committed gets `SQLITE_BUSY_SNAPSHOT`.
- **Cost of SSI vs SI** (PostgreSQL 9.1, Ports and Grittner VLDB 2012):
  - under 7% throughput loss on the realistic benchmarks, about 5% on CPU-bound DBT-2++, and 10-20% CPU overhead on a worst-case microbenchmark;
  - serialization failure rate under 0.25% (DBT-2++) and 0.03% vs 0.004% (RUBiS);
  - S2PL reaches only about half of SI throughput on RUBiS (208 vs 435 req/s).

  The real cost is **complexity and memory**. SIREAD locks must outlive commit. Bounding memory took granularity promotion, aggressive cleanup, summarization of committed transactions, and an SLRU spill to disk. The authors say SSI was "more challenging than a typical S2PL-based serializable mode."

## 1. Vocabulary: anomalies

- The ANSI SQL-92 levels are defined by three phenomena: dirty read, non-repeatable read and phantom. Berenson et al. showed these "fail to characterize several popular isolation levels", sharpened the definitions, and defined Snapshot Isolation [Berenson].
- The multiversion serialization graph (Adya) has three edge types: wr-dependency, ww-dependency and **rw-antidependency**. An rw-antidependency means "T1 writes a version, T2 read the previous version" [Ports §3.1]. A cycle means the execution is non-serializable.
- **Write skew**: two transactions read overlapping data and write disjoint rows. In the "doctors on call" example, both see two doctors on call and each removes one. "The write locks taken on update don't solve this problem, because the two transactions modify different rows" [Ports §2.1.1].
- **Read-only anomaly**: three transactions (REPORT, NEW-RECEIPT, CLOSE-BATCH). The anomaly "requires all three transactions, including T1 even though it is read-only" [Ports §2.1.2]. Under SI, even a read-only transaction can observe a state that no serial order produces.

## 2. PostgreSQL

### MVCC storage

- Every heap tuple header carries `t_xmin` (inserting XID, 4 B), `t_xmax` (deleting XID, 4 B), `t_cid` (command id, 4 B) and `t_ctid` (6 B, pointer to "this or a newer row version"). The fixed header is 23 bytes [PG-page].
- "an UPDATE or DELETE of a row does not immediately remove the old version of the row ... the row version must not be deleted while it is still potentially visible to other transactions" [PG-vacuum]. `VACUUM` reclaims dead versions. A visibility map tracks all-visible pages, which also enables index-only scans [PG-vacuum].
- XIDs are 32 bits, so tables must be vacuumed and rows **frozen** at least every ~2 billion transactions. Otherwise the cluster refuses new XIDs [PG-vacuum].
- MVCC's headline property: "reading never blocks writing and writing never blocks reading", and PostgreSQL keeps it "even when providing the strictest level ... through the use of ... SSI" [PG-mvcc].

### Levels

| Level | Dirty | Non-repeatable | Phantom | Serialization anomaly |
|---|---|---|---|---|
| Read uncommitted (= RC in PG) | no | possible | possible | possible |
| Read committed (default) | no | possible | possible | possible |
| Repeatable read (= SI) | no | no | no (in PG) | possible |
| Serializable (SSI) | no | no | no | no |

Source: [PG-iso].

- **Read Committed** takes a new snapshot per statement. When an UPDATE/DELETE is blocked by a concurrent writer, it waits and then *re-evaluates the WHERE clause against the new row version*. The docs call this "unsuitable for commands that involve complex search conditions" [PG-iso].
- **Repeatable Read** takes one snapshot at the first non-control statement. If it tries to modify a row that a concurrent transaction changed and committed, it fails with `could not serialize access due to concurrent update` (first-updater-wins, so no lost updates). "read-only transactions will never have serialization conflicts" [PG-iso]. It still permits write skew and the read-only anomaly [PG-iso; Ports §2].

### Serializable = SSI

- Serializable is "Repeatable Read plus monitoring" for read/write dependencies. The error is `could not serialize access due to read/write dependencies among transactions`, SQLSTATE 40001 [PG-iso].
- Predicate locks show up as `SIReadLock`. They "do not cause any blocking and therefore can not play any part in causing a deadlock". They "often need to be kept past transaction commit" [PG-iso; README-SSI].
- Locks start at tuple level and are **promoted** to page or relation level under memory pressure, at the price of more false positives. "A sequential scan will always necessitate a relation-level predicate lock" [PG-iso; README-SSI].
- Abort rule: roll back "only ... if Tout commits before Tpivot and Tin". The victim is chosen so an immediate retry won't fail the same way ("safe retry") [README-SSI; Ports §5.4].
- Memory is fixed at startup (`max_pred_locks_per_transaction` etc.). Older committed transactions get summarized into SLRU storage that can spill to disk. Under memory pressure the system degrades by raising false positives, not by refusing transactions [README-SSI; Ports §6].
- Read-only optimizations:
  - A `READ ONLY` transaction can often skip predicate locks altogether.
  - `SERIALIZABLE READ ONLY DEFERRABLE` waits for a **safe snapshot** and then runs as plain SI with no abort risk. This is "the only case where Serializable transactions block but Repeatable Read transactions don't" [PG-iso; Ports §4].
- Exclusions and limits:
  - SSI does not cover system catalogs, temporary tables or sequences.
  - Performance degrades as the number of active transactions grows, so use pooling [README-SSI].
  - Non-serializable transactions are unaffected by SSI and do not take SIREAD locks [README-SSI].
  - Serializable can still raise errors that would not occur serially, e.g. unique violations [PG-iso].
- Operational guidance [PG-iso]:
  - declare transactions READ ONLY;
  - keep transactions short;
  - avoid "idle in transaction";
  - drop `SELECT FOR UPDATE` and explicit locks that SSI already covers;
  - retry 40001 generically.

## 3. InnoDB (MySQL 8.4)

### MVCC storage

- Hidden columns on every clustered-index row [InnoDB-mv]:
  - `DB_TRX_ID` (6 B, last inserting or updating trx; a delete is an update that sets a delete bit);
  - `DB_ROLL_PTR` (7 B, points to the undo record that rebuilds the previous version);
  - `DB_ROW_ID` (6 B, used only when there is no PK).
- Clustered-index records are **updated in place**. Older versions are rebuilt from the **undo log** chain [InnoDB-mv].
- Insert undo can be discarded at commit. Update undo must stay until no snapshot could need it. A **purge** thread physically removes delete-marked rows and their index entries. Purge can lag and bloat tables [InnoDB-mv].
- Secondary indexes have no hidden system columns and are not updated in place: old entries are delete-marked, new ones inserted, and both purged later. If a secondary record is delete-marked, or its page was "updated by a newer transaction", InnoDB must look up the clustered row and walk undo. This also disables covering-index reads [InnoDB-mv].
- Long transactions, read-only ones included, stop undo from being discarded: "the rollback segment may grow too big" [InnoDB-mv].

### Levels

- **Default is REPEATABLE READ** [InnoDB-iso].
- **REPEATABLE READ**:
  - Plain SELECTs read "the snapshot established by the first read" [InnoDB-iso; InnoDB-cr].
  - Locking reads, UPDATE and DELETE use "the most recent state of the database". They take **next-key or gap locks** over scanned ranges, except for unique-index equality lookups, which lock only the record [InnoDB-iso].
  - The snapshot "applies to SELECT statements ... not necessarily to DML". A `DELETE`/`UPDATE` can affect rows committed by others that the transaction's own SELECTs cannot see, and those rows then become visible to it [InnoDB-cr].
  - MySQL itself advises: "It is not recommended to mix locking statements ... with non-locking SELECT statements in a single REPEATABLE READ transaction" [InnoDB-iso].
  - Consequence (our reading of [InnoDB-cr]): InnoDB RR is **not** textbook SI. Read-modify-write through a plain SELECT can lose updates, because the UPDATE applies to the latest version and there is no first-updater-wins abort.
- **READ COMMITTED** takes a fresh snapshot per consistent read. It locks no gaps, except for FK and duplicate checks, so "phantom row problems may occur". It uses "semi-consistent" reads for UPDATE [InnoDB-iso].
- **READ UNCOMMITTED** allows dirty reads [InnoDB-iso].
- **SERIALIZABLE** is RR plus implicit `SELECT ... FOR SHARE` when autocommit is off, i.e. classic **lock-based** serializability with readers blocking writers. Autocommit SELECTs stay non-locking consistent reads [InnoDB-iso].
- Cahill implemented SSI in InnoDB as part of his PhD work [Grittner-2009]. Mainline MySQL never adopted it: the 8.4 docs describe only the locking SERIALIZABLE [InnoDB-iso].

## 4. SQLite (WAL mode)

- Writers append pages to the WAL and commit by appending a commit record. The original DB file is untouched until a checkpoint [SQLite-wal].
- Each reader records an **end mark** at the start of its read transaction and ignores WAL frames after it. A shared-memory **wal-index** makes page lookup fast. Because it uses shared memory, all processes must be on the same host [SQLite-wal].
- "There can only be a single writer at a time." Readers and writers do not block each other [SQLite-iso; SQLite-wal].
- Isolation [SQLite-iso]:
  - Between connections it is serializable, because writes are serialized.
  - In WAL mode, readers get **snapshot isolation**.
  - A read transaction that tries to upgrade to a write after another connection committed gets `SQLITE_BUSY_SNAPSHOT`.
  - `BEGIN IMMEDIATE` takes the write lock up front and avoids this.
  - There is no isolation between operations on the same connection.
- Long readers block checkpoints. With overlapping readers the WAL "will grow without bound" (checkpoint starvation) [SQLite-wal].
- `BEGIN CONCURRENT` is documented on the `begin-concurrent` branch, not in mainline docs. It allows several writers in WAL/wal2 mode with **optimistic page-level** validation at COMMIT: it checks whether any page the transaction *read* was modified since BEGIN. On conflict it returns `SQLITE_BUSY_SNAPSHOT`, and commits are still serialized. Because of page granularity, monotonic rowids and timestamp indexes cause false conflicts [SQLite-bc].

## 5. Cahill, Röhm, Fekete: SSI (SIGMOD 2008; TODS 2009; Cahill PhD 2009)

- Goal: "automatically detects and prevents snapshot isolation anomalies at runtime for arbitrary applications", while keeping "readers do not block writers and vice versa" [Cahill abstract, quoted in Grittner-2009].
- Theory:
  - Every cycle contains at least two rw-antidependencies (Adya).
  - Fekete et al. 2005 showed two of them are **adjacent**: T1 -rw-> T2 -rw-> T3, with T3 committing first. Ports and Grittner strengthen this: T3 is the *first* in the cycle to commit [Ports §3.2, Theorem 1].
- Mechanism: transactions take **SIREAD** locks on what they read. A SIREAD/write conflict flags an rw edge. If a transaction has both an incoming and an outgoing rw edge (it is a "pivot"), one transaction aborts. Only rw edges are tracked, not wr or ww. SIREAD locks must persist until all concurrent transactions finish [Ports §3.3].
- False positives: not every dangerous structure is part of a cycle.
  - Commit-ordering optimizations (Cahill thesis; PG) reduce them.
  - PSSI removes them by full cycle testing, cutting aborts by up to 40% on a stress microbenchmark. PG rejected PSSI because tracking wr and ww edges costs memory, and the observed failure rates were "well under 1%" [Ports §3.3.1].
- Prototype: Berkeley DB, "only 692 lines of code ... modified out of ... over 200,000". Throughput "approaches that of snapshot isolation in most cases" and is "significantly better" than 2PL in many cases [Grittner-2009, quoting the paper].
- Caveat from the paper: Berkeley DB versioned and locked **pages**, which made the job simpler. "In databases that version and lock at row-level granularity ... additional effort would be required to avoid phantoms" [Grittner-2009, quoting the paper]. This is why PostgreSQL had to build a new predicate lock manager [Ports §1].

## 6. What true serializability costs (SSI vs SI vs S2PL)

All figures are from [Ports §6, §8] (PostgreSQL 9.1).

| Benchmark | SI | SSI | S2PL |
|---|---|---|---|
| SIBENCH (microbench, CPU) | 100% | close to SI; 10-20% CPU overhead from read tracking | clear penalty (updates block queries) |
| DBT-2++ in-memory (25 WH) | 100% | ~5% slower | worse than SSI for all mixes; much worse when read-heavy |
| DBT-2++ disk-bound (150 WH) | 100% | "indistinguishable"; failure rate < 0.25% | less pronounced gap |
| RUBiS | 435 req/s, 0.004% failures | 422 req/s, 0.03% | 208 req/s, 0.76% (deadlocks) |
| Deferrable read-only txn | - | wait for safe snapshot: median 1.98 s, p90 6 s, max 20 s | - |

- **Throughput**: under 7% loss vs SI on realistic workloads. SSI beats S2PL by a wide margin on read-heavy workloads because it never blocks [Ports §1, §8].
- **Aborts**: a small rise in serialization failures. Apps must retry on 40001 anyway, since SI can already abort on write-write conflicts [Ports §3; PG-iso].
- **Memory**: the hard part. "a transaction's locks cannot be released until that transaction and all concurrent transactions commit". One long transaction "can easily prevent thousands of transactions from being cleaned up". Mitigations:
  1. safe snapshots and deferrable transactions;
  2. granularity promotion;
  3. aggressive cleanup;
  4. summarization of committed transactions into a dummy holder plus one 64-bit commit-seq-no per transaction, spillable to disk.

  The price is a higher false-positive rate under pressure [Ports §6].
- **Complexity**: the work needed a new predicate lock manager, plus integration with 2PC, subtransactions and replication. The authors conclude: "implementing SSI proved more challenging than a typical S2PL-based serializable mode" [Ports §1, §9].

## 7. Implications for kitchen-sink-db (our inference, not sourced)

- Globals are an ordered KV space and SQL tables will sit on ordered keys too. A cross-model predicate lock is then naturally a **key-range SIREAD lock** over the shared key order. That fits the Cahill/PG design better than the page-level approach. It also fits better than SQLite's single writer, which is the simplest design that is correct for a thin embedded slice.
- A sensible staging:
  1. single writer + SI readers (SQLite-like; serializable for free);
  2. multi-writer SI with first-updater-wins (PG RR semantics, *not* InnoDB's mixed RR);
  3. SSI with bounded memory from the start (fixed tables + summarization), using PG's safe-retry victim rule.
- Version storage is the big fork. Version chains in place with VACUUM (PG) vs undo logs with purge (InnoDB). Either way, a long reader pins garbage: dead tuples, undo, or WAL growth in SQLite. So we need an "oldest active snapshot" horizon and diagnostics from day one.

## Sources

- [PG-iso] PostgreSQL docs, "13.2 Transaction Isolation": https://www.postgresql.org/docs/current/transaction-iso.html
- [PG-mvcc] PostgreSQL docs, "13.1 Introduction" (MVCC): https://www.postgresql.org/docs/current/mvcc-intro.html
- [PG-page] PostgreSQL docs, "Database Page Layout": https://www.postgresql.org/docs/current/storage-page-layout.html
- [PG-vacuum] PostgreSQL docs, "Routine Vacuuming": https://www.postgresql.org/docs/current/routine-vacuuming.html
- [README-SSI] PostgreSQL source, `src/backend/storage/lmgr/README-SSI`: https://raw.githubusercontent.com/postgres/postgres/master/src/backend/storage/lmgr/README-SSI
- [Ports] D. R. K. Ports, K. Grittner, "Serializable Snapshot Isolation in PostgreSQL", PVLDB 5(12), 2012: https://arxiv.org/abs/1208.4179
- [Cahill] M. J. Cahill, U. Röhm, A. D. Fekete, "Serializable Isolation for Snapshot Databases", SIGMOD 2008, pp. 729-738, DOI 10.1145/1376616.1376690 (TODS version: 10.1145/1620585.1620587). The ACM PDF was not reachable (403/login wall). Quotes come via [Grittner-2009] and the summary in [Ports §3].
- [Grittner-2009] K. Grittner, pgsql-hackers, "Serializable Isolation without blocking" (quotes the Cahill paper): https://www.postgresql.org/message-id/4A0019EE.EE98.0025.0%40wicourts.gov
- [Berenson] H. Berenson et al., "A Critique of ANSI SQL Isolation Levels", SIGMOD 1995: https://arxiv.org/abs/cs/0701157
- [InnoDB-iso] MySQL 8.4 Reference Manual, "Transaction Isolation Levels": https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-isolation-levels.html
- [InnoDB-cr] MySQL 8.4 Reference Manual, "Consistent Nonlocking Reads": https://dev.mysql.com/doc/refman/8.4/en/innodb-consistent-read.html
- [InnoDB-mv] MySQL 8.4 Reference Manual, "InnoDB Multi-Versioning": https://dev.mysql.com/doc/refman/8.4/en/innodb-multi-versioning.html
- [SQLite-iso] SQLite, "Isolation In SQLite": https://sqlite.org/isolation.html
- [SQLite-wal] SQLite, "Write-Ahead Logging": https://sqlite.org/wal.html
- [SQLite-bc] SQLite `begin-concurrent` branch doc: https://www.sqlite.org/src/doc/begin-concurrent/doc/begin_concurrent.md
