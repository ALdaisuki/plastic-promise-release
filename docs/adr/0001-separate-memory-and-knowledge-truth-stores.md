---
status: accepted
---

# Separate memory and knowledge truth stores

Plastic Promise keeps `plastic_memory.db` authoritative for user facts, preferences, decisions, experience, proposals, and trust, while a separate `plastic_knowledge.db` owns sources, versions, evidence, domains, claims, artifacts, and ingestion jobs. The stores share project and trace identities and are fused by `context_supply`; this avoids document-scale writes and generation rebuilds disrupting the latency-sensitive memory path.
