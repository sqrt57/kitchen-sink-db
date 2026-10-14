# How do real engines implement MVCC and the isolation levels?

Type: research
Status: resolved
Blocked by: 

## Question

How do PostgreSQL (including SSI), InnoDB, SQLite (WAL mode) and the Cahill et al. SSI paper implement MVCC, snapshot isolation and serializable isolation? What anomalies does each level permit in practice? What does true serializability cost (memory, aborts, complexity) compared with SI?

## Answer

- **Version storage differs.** PostgreSQL keeps every row version in the heap (xmin/xmax) and garbage-collects with VACUUM; it must freeze rows because XIDs are 32-bit. InnoDB updates rows in place and rebuilds old versions from undo logs, which a purge thread later discards. SQLite versions whole pages: new page images go to the WAL, and each reader's "end mark" picks its snapshot.
- **Long readers pin old data in all three.** In PostgreSQL that means dead tuples, in InnoDB undo growth, and in SQLite a WAL that keeps growing because checkpoints can't finish. We need an "oldest active snapshot" horizon from day one.
- **SI blocks the classic anomalies but not all.** It prevents dirty and non-repeatable reads and phantoms, and prevents lost updates when the first updater wins (PostgreSQL REPEATABLE READ raises "could not serialize access due to concurrent update"). It still allows write skew and the read-only transaction anomaly.
- **InnoDB's REPEATABLE READ is not true SI.** Only plain SELECTs read the snapshot. DML and locking reads act on the latest committed data and take next-key/gap locks. MySQL itself warns against mixing the two kinds of statement in one transaction.
- **Serializable is built three different ways.** PostgreSQL uses SSI: non-blocking SIREAD predicate locks plus aborts on "dangerous structures". InnoDB uses 2PL: plain SELECT becomes FOR SHARE. SQLite allows a single writer, and a WAL reader that tries to upgrade after another commit gets SQLITE_BUSY_SNAPSHOT. SQLite's `BEGIN CONCURRENT` (branch only) checks for conflicts optimistically at page level.
- **How SSI works (Cahill et al.):** every serialization anomaly contains two adjacent rw-antidependencies, T1->T2->T3, with T3 the first to commit. SSI tracks only rw edges, so it accepts some false positives. Its SIREAD locks must outlive commit until all concurrent transactions finish.
- **SSI runtime cost is small.** In the PostgreSQL 9.1 benchmarks it lost under 7% vs SI (about 5% when CPU-bound; 10-20% CPU on a microbenchmark). Serialization failures stayed under 0.25%. On RUBiS, 2PL managed only about 48% of SI throughput (208 vs 435 req/s).
- **SSI's real cost is memory and complexity.** PostgreSQL needed a new predicate lock manager, lock granularity promotion, aggressive cleanup, and summarization of committed transactions spilling to disk via SLRU. The authors call it harder than an S2PL-based serializable mode. Read-only DEFERRABLE transactions wait for a safe snapshot: median 2 s, worst case 20 s.
- **Suggested staging for us:** single writer with SI readers first, then multi-writer SI with first-updater-wins, then key-range SSI with bounded memory. Key-range locks fit naturally if Globals and table keys share one key order.
- Caveat: the Cahill paper itself was not accessible (ACM 403); its claims are quoted via Grittner's 2009 pgsql-hackers post and the Ports & Grittner VLDB 2012 paper.

Full findings: branch `research/mvcc-isolation-prior-art`, file `research/mvcc-isolation-prior-art.md`
