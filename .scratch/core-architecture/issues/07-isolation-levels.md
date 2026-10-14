# Which isolation levels do we support, and what do they mean?

Type: grilling
Status: open
Blocked by: 02

## Question

Which isolation levels does the engine support (Read Uncommitted, Read Committed, Repeatable Read, Snapshot, Serializable)? What exact semantics does each one have? Do any of them alias each other (as in Postgres)? Is Serializable true serializability (SSI or locking) or SI? How do the levels behave for MUMPS-dialect access to Globals inside the same transaction?
