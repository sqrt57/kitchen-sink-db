# SQL and hierarchical data on one engine: prior art

Ticket: `.scratch/core-architecture/issues/01-sql-over-kv-prior-art.md`
Researched: 2026-10-14. Sources are first-party docs, source-tree docs/RFCs, issue threads and one peer-reviewed paper. Each claim links to its source.

## Question

How do InterSystems IRIS (Caché), GT.M/YottaDB, the FoundationDB Record Layer, and CockroachDB/TiDB put SQL (and, where relevant, Globals) on top of a KV or Global store? How do transactions span both models? How are rows and indexes encoded as keys? How is the catalog stored? What trade-offs do they report? Also: systems that keep separate engines coordinated by a shared transaction manager and WAL.

## Summary

Two families appear:

1. **One ordered store, SQL as a mapping layer** (IRIS, YottaDB+Octo, FDB Record Layer, CockroachDB, TiDB). Rows become `prefix / table-or-index id / key columns -> encoded non-key columns`. Secondary indexes are more keys in the same store, written in the same transaction as the row. Cross-model transactions are free, because there is only one transaction system: SQL is just another writer of the same keys. In IRIS and YottaDB the "KV" is literally a Global, so SQL rows and application Globals are both Nodes.
2. **Separate engines, shared coordinator** (MySQL server binlog + InnoDB; SQL Server disk tables + In-Memory OLTP). Each engine keeps its own concurrency control and, in MySQL's case, its own log. Atomicity across them needs internal two-phase commit (MySQL) or a "cross-container" transaction with per-engine isolation rules (SQL Server). Both report real costs: extra fsyncs, recovery that has to reconcile two logs, and isolation-level restrictions when crossing engines.

For this project, family 1 maps directly onto the Global model: a SQL table is a Global with a known subscript layout, and the catalog can be one more Global.

## 1. InterSystems IRIS / Caché: SQL tables are Globals

**Row encoding.** A persistent class / SQL table stores each row as one Node of a data Global, subscripted by the ID (RowID), with non-key fields packed in a `$ListBuild` value:

```
^GlobalsTest.PresidentD(1)=$lb("",1732,"1","Washington,George")
```

"The subscript of ^GlobalsTest.PresidentD is the IDKey." ([IRIS: Storage Globals](https://docs.intersystems.com/irislatest/csp/docbook/DocBook.UI.Page.cls?KEY=GOBJ_storageglobals))

**Global roles.** A storage definition names a DataLocation ("the global where class data will be stored"), an IdLocation (holds the ID counter at its root, often the same Global as the data), an IndexLocation, and a StreamLocation for stream (BLOB/CLOB) properties. (same source)

**Index encoding.** A standard index is a Node with an empty value, subscripted by index name, the collated indexed value, then the ID:

```
^GlobalsTest.PresidentI("DOBIndex",1732,1)=""
^GlobalsTest.PresidentI("NameIndex"," WASHINGTON,GEORGE",1)=""
```

"The first subscript of ^GlobalsTest.PresidentI is the name of the index." Note the leading space in `" WASHINGTON,GEORGE"`: string index values are stored in a collation form (SQLUPPER), not the raw value. (same source)

**Bitmap indexes** store compressed bitstrings per chunk of IDs, e.g. a bitmap extent index `^Ebnm.BndZ.2(1)=$zwc(412,1,0)/*$bit(2..4)*/`. (same source)

**Hashed Global names.** With `USEEXTENTSET=1` the system generates short hashed names and gives each index its own Global (`^Ebnm.EKUy.1(1)=$lb(...)`). "For classes defined using an SQL CREATE TABLE statement, the default for the USEEXTENTSET parameter is 1." `DEFAULTGLOBAL` lets the user choose the root (`^GT.Pres.1`, `^GT.Pres.2`, ...). (same source) A separate Global per index makes per-index operations (drop, rebuild, mapping to another database) whole-Global operations.

**Transactions across models.** There is one transaction mechanism. ObjectScript `TSTART`/`TCOMMIT` and SQL `START TRANSACTION`/`COMMIT` are documented as functionally equivalent, and the docs show `TSTART` wrapping embedded `&sql(UPDATE ...)` ([IRIS: Transaction Processing](https://docs.intersystems.com/irislatest/csp/docbook/DocBook.UI.Page.cls?KEY=GAPPS_tp)). Rollback works because "Each instance of InterSystems IRIS maintains a journal that logs all SET and KILL operations made during transactions", and recovery "rolls back those that were not committed" (same source). So SQL row writes and direct Global SETs are the same thing to the journal.

**Reported trade-off.** The nesting models differ: ObjectScript nests by repeated `TSTART`, SQL uses `SAVEPOINT`, and "any COMMIT statement ends all active transactions, including those started with SAVEPOINT" (same source). Some reference pages go further and say the two should not be mixed in one transaction ([SAVEPOINT (SQL)](https://docs.intersystems.com/irisforhealthlatest/csp/docbook/DocBook.UI.Page.cls?KEY=RSQL_savepoint), [$TLEVEL](https://docs.intersystems.com/irisforhealthlatest/csp/docbook/DocBook.UI.Page.cls?KEY=RCOS_vtlevel)). The lesson: a single engine makes cross-model atomicity easy, but the two front-ends need one agreed nesting/savepoint model.

**Durability.** The journal records "all global update operations"; a separate write image journal (WIJ) records "database modifications that are not yet written to the database" (torn-page protection for the block store). ([IRIS: Journaling](https://docs.intersystems.com/irislatest/csp/docbook/DocBook.UI.Page.cls?KEY=GCDI_journal))

**Catalog.** Not verified from a primary page in this session. IRIS's SQL schema is projected from class definitions, which are themselves stored in system Globals; treat details as unconfirmed.

## 2. GT.M / YottaDB + Octo: SQL as an overlay on existing Globals

**Engine transactions.** YottaDB transactions are database-wide: "a transaction is a sequence of commands that begins with a TSTART command, ends with a TCOMMIT command". Concurrency is optimistic: "At TCOMMIT time, the process checks whether any blocks have changed since it read them"; on conflict the process restarts at `TSTART`, and after a third failed commit it runs alone. Locks inside TP are discouraged ("be SERIAL with no LOCK operations within the transaction"), and non-idempotent side effects must be guarded by `0=$TRESTART`. Journal records are written before database updates. ([YottaDB Programmer's Guide: Language Features](https://docs.yottadb.com/ProgrammersGuide/langfeat.html)) The C API has the same shape: the application passes a callback to `ydb_tp_s()`, which YottaDB re-invokes on restart; `$zmaxtptime` aborts long transactions. ([YottaDB Multi-Language Programmer's Guide](https://docs.yottadb.com/MultiLangProgGuide/MultiLangProgGuide.html))

**Octo (SQL).** "a SQL database engine whose tables are stored in YottaDB global variables". It keeps its own metadata and cross-references in Globals whose names start with `%ydbocto` and `%ydbAIM`, which must be mapped to a region with `NULL_SUBSCRIPTS=ALWAYS`. ([Octo: Introduction](https://docs.yottadb.com/Octo/intro.html)) So the catalog lives in Globals.

**Row encoding / mapping DDL.** `CREATE TABLE` is extended to describe an existing Global layout ([Octo: Grammar](https://docs.yottadb.com/Octo/grammar.html)):

- `GLOBAL`: "a global name followed by an optional list of subscripts". Default for read-write tables: `^%ydboctoD_$zysuffix(tablename)(keys("colname"))`, one subscript per key column.
- `PIECE` / `DELIM`: a column is `$PIECE(node, delim, n)`; default delimiter `"|"`, default piece = column ordinal. `DELIM ""` takes the whole node.
- `EXTRACT`: a column computed from an M expression over `keys(...)` / `values(...)`.
- `READONLY` tables map onto existing application Globals, forbid INSERT/UPDATE/DELETE, and `DROP TABLE` leaves the Global alone. `READWRITE` tables own their Global; if there is no key column, Octo adds a hidden `%YO_KEYCOL`.

**Reported trade-offs.** Mapping SQL over arbitrary legacy Globals forces SQL to be read-only over them (writes would need to know every application invariant). Octo's intro states transaction control (TCL) was not yet implemented at the time of writing ([Octo: Introduction](https://docs.yottadb.com/Octo/intro.html)), i.e. SQL statements are not yet first-class participants in user transactions even though the engine has TP.

## 3. FoundationDB Record Layer

**Layout.** "the keys of each record store start with a unique binary prefix which defines a FoundationDB subspace"; "Each index is stored in a dedicated subspace within the record store"; "Every record store tracks the highest version it has been accessed with in a small header within a single key-value pair." Keys use the FDB tuple encoding. ([Chrysafis et al., *FoundationDB Record Layer*, SIGMOD 2019](https://arxiv.org/html/1901.04452v2)) The primary key is a clustered index: "The actual record is stored under this key"; secondary indexes "concatenate the primary key onto the end of the result of the expression" and map to an empty value. ([Record Layer Overview](https://foundationdb.github.io/fdb-record-layer/Overview.html))

**Transactions.** "Index maintenance occurs in the same transaction as the record change itself." (paper) Records are Protobuf messages.

**Catalog.** Metadata (Protobuf descriptors + index definitions) can be stored in FDB via `FDBMetaDataStore` ([Overview](https://foundationdb.github.io/fdb-record-layer/Overview.html)). "The metadata is versioned in single-stream, non-branching, monotonically increasing fashion"; on open, the store header version is compared with the metadata version to detect stale caches or pending upgrades (paper).

**Living within KV limits.** FDB limits: 10,000,000 bytes per transaction, no transactions over five seconds, keys up to 10,000 bytes, values up to 100,000 bytes ([FDB Known Limitations](https://apple.github.io/foundationdb/known-limitations.html)). The Record Layer copes by:
- splitting large records "across a set of contiguous keys";
- continuations that split long scans across transactions;
- building new indexes online across many transactions, with the index in a write-only state (maintained by writes, not used by queries) until it becomes readable. (paper)

**Reported trade-offs** (paper): no in-memory query operators, so sorts/joins must be served by indexes; "Bugs due to incorrect manual conflict ranges are very hard to find"; cached read versions can cause stale reads and more failed transactions.

## 4. CockroachDB

**Row encoding.** Key = `/TableID/IndexID/<pk columns>/<FamilyID>`; e.g. `/Table/51/1/1/0`. Value = checksum, type tag, and a `TUPLE` of non-key columns (column-ID delta + type tag + datum). The key encoding is order-preserving (descending columns invert bytes) and prefix-free, so KV order equals SQL order. NULL columns are simply absent, so `ADD COLUMN` needs no rewrite. A family-0 "sentinel" KV is always written so every row has at least one key. Composite types (collated strings, decimals where `1.0 = 1.000`) are stored in the key and again in the value because key encoding is lossy. ([CockroachDB tech note: Structured data encoding](https://github.com/cockroachdb/cockroach/blob/master/docs/tech-notes/encoding.md))

**Index encoding.** Non-unique index key = index columns + primary key columns (`/Table/51/3/"Alice"/1/0`), value mostly empty. Unique index key = index columns only (PK appended only when an indexed column is NULL), value = PK + stored columns, so a uniqueness violation is a KV conflict. (same source)

**Column families trade-off.** Originally one KV per column; the RFC moved to one KV per family because each key carried ~28 bytes of MVCC timestamp + checksum and its own write intent. Grouping columns "cut[s] this overhead in half"; cost: `UPDATE` must read all columns of a touched family. ([RFC: SQL column families](https://github.com/cockroachdb/cockroach/blob/master/docs/RFCS/20151214_sql_column_families.md))

**Transactions.** The SQL layer "converts all SQL statements into key-value (KV) operations" executed in a `client.Txn`; every statement is a transaction; writes are MVCC "write intents" plus a transaction record; default isolation is SERIALIZABLE with client retries. ([CockroachDB: Transaction Layer](https://docs.cockroachlabs.com/docs/stable/architecture/transaction-layer))

**Catalog.** System tables such as `system.namespace` are themselves SQL tables in KV; the encoding note warns that some are written "using raw KV puts" without the sentinel, so decoders must tolerate that ([encoding tech note](https://github.com/cockroachdb/cockroach/blob/master/docs/tech-notes/encoding.md)). Schema changes run as background jobs over multiple schema versions; "Most schema changes should not be performed within an explicit transaction with multiple statements" (exceptions: `CREATE TABLE`, `CREATE INDEX`). ([CockroachDB: Online Schema Changes](https://docs.cockroachlabs.com/docs/stable/online-schema-changes)) The multi-version protocol exists for a distributed cluster; a single-node engine does not need it.

**Hierarchical keys tried and removed.** CockroachDB had *interleaved tables*: child rows stored under the parent's key prefix (a SQL analogue of Global sub-Nodes). They were removed: the join code was "complex, undertested, and planned only in precise circumstances", writes were not actually batched, and benchmarks "found that they indeed provide only negligible performance improvements"; the feature was "very heavy for both customers and implementers". ([cockroach#52009](https://github.com/cockroachdb/cockroach/issues/52009)) Relevant to the "hierarchical fields in SQL later" constraint: co-locating child data under a parent key is cheap to encode but expensive to support across planner, schema change and backup.

## 5. TiDB

**Encoding** ([TiDB Computing](https://docs.pingcap.com/tidb/stable/tidb-computing)):

```
row:               t{TableID}_r{RowID}                       -> [col1, col2, ...]
unique/PK index:   t{TableID}_i{IndexID}_{indexedValues}         -> RowID
non-unique index:  t{TableID}_i{IndexID}_{indexedValues}_{RowID} -> null
```

An integer primary key is used directly as the RowID. Same unique vs non-unique split as CockroachDB.

**Catalog.** Database/table metadata is serialized under keys with an `m_` prefix in the same KV store; a global schema version (`/tidb/ddl/global_schema_version`) is bumped per DDL state change and watched by each server to support online schema change. (same source)

## 6. Separate engines, shared transaction coordination

### MySQL: server binlog + InnoDB, internal two-phase commit

MySQL has an engine-independent server log (the binary log) and per-engine logs (InnoDB redo). Keeping them consistent required XA: "InnoDB support for two-phase commit in XA transactions ensures that the binary log and InnoDB data files are synchronized", always on in 8.0. Both logs must be fsynced (`sync_binlog=1`, the default and "the slowest"). On crash recovery the server scans the last binlog for XIDs, "tells InnoDB to complete any prepared transactions that were successfully written to the binary log, and truncates the binary log". If the binlog turns out shorter than InnoDB's committed state, replication must be rebuilt from a fresh snapshot. ([MySQL 8.0: The Binary Log](https://dev.mysql.com/doc/refman/8.0/en/binary-log.html))

Trade-off: two logs means two fsyncs per commit (mitigated by group commit), a prepare phase, and recovery logic that reconciles logs. This is the cost the single-store approach avoids.

### SQL Server: disk-based tables + In-Memory OLTP ("cross-container" transactions)

A transaction touching both kinds of table "runs across two transaction management containers. One container manages disk-based tables, and the other manages memory-optimized tables." Disk tables use locking; memory-optimized tables use optimistic MVCC with commit-time validation (errors 41302/41305/41325 require client retry). Each container may run at a different isolation level within one transaction; explicit transactions under READ COMMITTED must access memory-optimized tables with a `WITH (SNAPSHOT)` hint or `MEMORY_OPTIMIZED_ELEVATE_TO_SNAPSHOT`. Commit writes changes to durable tables to the (shared database) log. Cross-database and distributed transactions are not supported for memory-optimized tables. ([SQL Server: Transactions with Memory-Optimized Tables](https://learn.microsoft.com/en-us/sql/relational-databases/in-memory-oltp/transactions-with-memory-optimized-tables))

Trade-off: one log keeps durability simple, but two concurrency-control schemes leak into the user's isolation semantics.

## Implications for kitchen-sink-db

- A single ordered store with SQL encoded as Globals (IRIS model) gives cross-model transactions, one WAL and one MVCC for free. Every system in family 1 does this; none of them coordinates two engines.
- Row = `table-id / pk... -> packed non-key columns`; non-unique index = `index-id / values... / pk... -> ""`; unique index = `index-id / values... -> pk`. IRIS and both NewSQL systems converge on this.
- Key encoding must be order-preserving and prefix-free (CockroachDB), and collation for index keys may differ from stored values (IRIS `SQLUPPER`, CockroachDB composite encoding). For Globals this means SQL key columns need an encoding consistent with M collation of Subscripts, or a separate SQL collation with a distinct key prefix.
- The catalog lives in the same store in every case (IRIS class-definition Globals, Octo `%ydbocto*` Globals, FDB `FDBMetaDataStore`, CockroachDB system tables, TiDB `m_` keys), so DDL is transactional with data for free on a single node.
- Packing many columns into one value (IRIS `$ListBuild`, CockroachDB families, TiDB row value) beats one key per column; CockroachDB measured the per-key overhead.
- Pick one savepoint/nesting model shared by SQL and the MUMPS dialect (IRIS shows the friction when they differ).
- Optimistic restarts (YottaDB, Hekaton, FDB) push retry into the client; that must be visible in the protocol and CLI if chosen.
- Two-engine designs (MySQL, SQL Server) work but pay with 2PC between logs or with per-engine isolation rules; avoid unless one engine must be special-purpose.
- Parent/child key interleaving (CockroachDB) was removed for complexity, not encoding difficulty: keep keys able to nest, but don't plan planner features around it early.

## Gaps

- IRIS SQL catalog storage (class dictionary Globals) and IRIS isolation levels were not confirmed from a primary page in this session.
- YottaDB Octo's current TCL status may have changed since the intro page was written.
- FDB Record Layer byte-level key layout for store header and index-state subspaces is not in the sources read.
