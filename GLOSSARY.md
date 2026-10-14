# Glossary

**Global**: A named, persistent, sparse, hierarchical array, written `^Name(sub1, sub2, ...)`. The unit of hierarchical storage.
_Avoid_: tree, document, hierarchical table

**Subscript**: One key component in the path to a Node of a Global; a string or a number, ordered by M collation.
_Avoid_: key segment, path element

**Node**: One position in a Global, addressed by the Global's name plus zero or more Subscripts. It may hold a value, have child Nodes, or both.
_Avoid_: entry, element, cell

**Embedded mode**: The database runs inside the host application's process, which is the only process allowed to open the database files.
_Avoid_: library mode, in-process mode

**Server mode**: The database runs as a standalone process, and clients connect to it over the network.
_Avoid_: daemon mode, standalone mode
