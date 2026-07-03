"""Quick smoke test for list_memories_paginated."""
import os
os.environ["AGENT_USE_SQLITE"] = "0"

from plastic_promise.core.context_engine import ContextEngine

engine = ContextEngine(use_sqlite=False)

# Empty engine
pages = list(engine.list_memories_paginated())
assert pages == [], f"Expected empty, got {pages}"

# Register 5 memories
for i in range(5):
    engine.register_memory({
        "id": f"mem_{i:04d}",
        "content": f"test memory {i}",
        "memory_type": "task",
        "source": "test",
    })

# Full iteration
pages = list(engine.list_memories_paginated(page_size=2))
assert len(pages) == 5, f"Expected 5, got {len(pages)}"

# Memory type filter
pages = list(engine.list_memories_paginated(memory_type="task"))
assert len(pages) == 5

pages = list(engine.list_memories_paginated(memory_type="experience"))
assert len(pages) == 0

# Source filter
pages = list(engine.list_memories_paginated(source="test"))
assert len(pages) == 5

pages = list(engine.list_memories_paginated(source="nonexistent"))
assert len(pages) == 0

print("All pagination tests passed!")
