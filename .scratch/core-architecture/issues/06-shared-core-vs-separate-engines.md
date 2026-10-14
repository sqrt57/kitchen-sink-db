# One shared KV core, or separate SQL and Global engines?

Type: grilling
Status: open
Blocked by: 01, 03

## Question

Should SQL tables and Globals share a single ordered KV core (one WAL, one MVCC, one lock manager)? Or should they be separate engines coordinated by a shared transaction manager and WAL? Which option keeps cross-model transactions simple and leaves room for hierarchical fields in SQL tables later?
