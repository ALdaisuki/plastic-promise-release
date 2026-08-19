---
status: accepted
---

# Separate recall tools and fuse at context supply

`memory_recall` retains its meaning as memory retrieval and a new `knowledge_search` interface retrieves source-grounded knowledge. `context_supply` and the passive Hook router may combine both result types under one project scope, but every item remains typed so knowledge, memory, principles, and code cannot silently masquerade as each other.
