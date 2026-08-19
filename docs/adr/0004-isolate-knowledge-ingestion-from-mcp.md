---
status: accepted
---

# Isolate knowledge ingestion from the MCP runtime

Large uploads, parsing, semantic batching, Wiki maintenance, and index rebuilds run behind a separate loopback knowledge-ingestion runtime instead of inside the latency-sensitive MCP process. Dashboard V2 remains the only operator frontend, but it talks to the isolated backend with project-scoped authorization so ingestion failure cannot stall memory recall or Hook submission.
