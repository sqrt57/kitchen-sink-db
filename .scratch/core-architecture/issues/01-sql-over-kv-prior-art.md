# How do existing systems put SQL and hierarchical data on one engine?

Type: research
Status: resolved
Blocked by: 

## Question

How do InterSystems Caché/IRIS, GT.M/YottaDB, FoundationDB (record/SQL layers) and CockroachDB/TiDB put SQL (and, where relevant, Globals) on top of a KV or Global store? How do they make transactions span both models? Cover: how rows and indexes are encoded as keys, how the catalog is stored, and the trade-offs each one reports. Also cover systems that keep separate engines coordinated by a shared transaction manager and WAL.

## Answer

- IRIS, YottaDB+Octo, the FDB Record Layer, CockroachDB and TiDB all put SQL on **one ordered store** as a key mapping. Cross-model atomicity comes for free because there is one transaction system and one log.
- IRIS stores a SQL row as a Global Node: `^T.D(id)=$lb(fields...)`. Indexes are `^T.I("Idx", collatedValue, id)=""`, with one Global per index by default for `CREATE TABLE`. SQL and `TSTART` transactions share the journal of SETs and KILLs.
- The NewSQL layout converges on: row `table/pk... -> packed non-key cols`, non-unique index `idx/vals.../pk -> ""`, unique index `idx/vals -> pk`. Key encoding is order-preserving and prefix-free. Collated or lossy key values are stored again in the value.
- In every system the catalog lives in the same store: class-definition Globals, Octo `%ydbocto*` Globals, FDB `FDBMetaDataStore`, CockroachDB system tables, TiDB `m_` keys.
- One KV per row (or per column group) beats one KV per column. CockroachDB measured about 28 B of MVCC and checksum overhead per key plus one write intent per key.
- Octo maps SQL over existing Globals with `GLOBAL`/`PIECE`/`DELIM` DDL. Mapped tables are READONLY, and SQL TCL was not implemented. IRIS documents friction between the SQL `SAVEPOINT` and M `TSTART` nesting models, so pick one model for both.
- Separate engines work but cost more. MySQL needs internal 2PC between the binlog and InnoDB redo and reconciles the two logs on recovery. SQL Server "cross-container" transactions mix locking and optimistic MVCC with per-engine isolation rules.
- CockroachDB removed interleaved (parent-prefixed) tables because of complexity and negligible benefit. Keys can nest, but don't build planner features around nesting early.
- Unconfirmed from primary pages: where IRIS stores its SQL catalog and what isolation levels IRIS offers. Octo's "transaction control not yet implemented" may be out of date.

Full findings: [research/sql-over-kv-prior-art.md](../research/sql-over-kv-prior-art.md)
