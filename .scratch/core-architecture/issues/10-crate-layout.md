# How is the Rust workspace split into crates?

Type: grilling
Status: open
Blocked by: 06

## Question

What crates make up the workspace, and where do the seams between them go? For example: storage, WAL, transactions, SQL front end, MUMPS front end, protocol, server, client, CLI. What does Embedded mode's public API look like compared with the Rust client for Server mode? Can they share one interface?
