# How are Subscripts collated and encoded into keys?

Type: grilling
Status: open
Blocked by: 06

## Question

How are Subscripts (strings and numbers in M collation order) encoded as bytes so byte order matches collation? How are SQL keys (if they share the space) encoded? How do we leave room for hierarchical fields in SQL tables later? Are there alternative collations (e.g. Unicode-aware)?
