# Agentic AI Architecture Designer - Reference Guide

## AI Agent Architecture Fundamentals

### Core Agent Components
- **Perception Module**: Processes input data and environment observations
- **Reasoning Engine**: Makes decisions based on available information
- **Memory System**: Stores and retrieves relevant information over time
- **Action Executor**: Performs actions in the environment or calls tools
- **Communication Interface**: Handles interaction with other agents/users

### Agent Types and Roles
- **Coordinator Agents**: Manage workflow and orchestrate other agents
- **Specialist Agents**: Focus on specific tasks or domains
- **Memory Agents**: Handle long-term storage and retrieval
- **Validation Agents**: Check outputs and ensure quality
- **Monitoring Agents**: Track system performance and health

### Communication Patterns
- **Direct Communication**: Point-to-point agent interactions
- **Message Bus**: Centralized communication hub
- **Shared Memory**: Common storage for inter-agent communication
- **Event-Driven**: Asynchronous communication via events
- **Request-Response**: Synchronous communication patterns

## Proven Agent Patterns

### 1. ReAct Pattern (Reason + Act)
**Purpose**: Iterative reasoning with action execution

**Structure**:
```
Thought: "I need to find X information"
Action: search(query="X")
Observation: [search results]
Thought: "Based on results, I need to..."
Action: analyze(data)
Observation: [analysis output]
Final Answer: [conclusion]
```

**When to use**: Complex problem-solving requiring multiple steps, research tasks, data gathering with analysis

**Cost**: 3-5 LLM calls per workflow (each thought + action pair)

### 2. Chain-of-Thought (CoT) Pattern
**Purpose**: Explicit step-by-step reasoning

**Structure**:
```
Problem: [user query]
Step 1: Break down the problem
Step 2: Identify required information
Step 3: Analyze each component
Step 4: Synthesize findings
Conclusion: [answer]
```

**When to use**: Mathematical problems, logical reasoning, complex analysis requiring transparency

**Cost**: 1-2 LLM calls (reasoning + optional verification)

### 3. Tool Use Pattern
**Purpose**: Delegate specific tasks to specialized tools

**Structure**:
```
Agent analyzes task → Selects tool → Executes → Interprets result
```

**Common tools**:
- `web_search()`: Information retrieval
- `calculator()`: Mathematical operations
- `code_interpreter()`: Execute code
- `database_query()`: Data access
- `api_call()`: External service integration

**When to use**: Tasks requiring external data, computation, or specific capabilities

**Cost**: 1 LLM call (tool selection) + tool execution cost

### 4. Hierarchical Planning
**Purpose**: Decompose complex tasks into manageable subtasks

**Structure**:
```
High-level Goal
├── Subtask 1
│   ├── Action 1a
│   └── Action 1b
├── Subtask 2
│   ├── Action 2a
│   └── Action 2b
└── Subtask 3
```

**When to use**: Multi-day projects, complex workflows, tasks requiring coordination

**Cost**: 1 planning call + N execution calls (N = number of subtasks)

### 5. Memory-Augmented Agents
**Purpose**: Maintain context across interactions

**Memory types**:
- **Short-term**: Current conversation (last N messages)
- **Long-term**: User preferences, historical interactions
- **Episodic**: Specific past events ("last time we discussed X")
- **Semantic**: General knowledge and facts

**Implementation**:
- Vector database for semantic search (Pinecone, Weaviate)
- Key-value store for quick lookups (Redis)
- SQL database for structured data (PostgreSQL)

**When to use**: Multi-turn conversations, personalization, learning systems

**Cost**: Storage cost + retrieval cost (vector search ~$0.001/query)

### 6. Reflection Pattern
**Purpose**: Self-evaluation and improvement

**Structure**:
```
Generate output → Evaluate quality → Identify issues → Refine → Repeat
```

**Evaluation criteria**:
- Accuracy: Does it answer the question?
- Completeness: Are all requirements met?
- Clarity: Is it easy to understand?
- Correctness: Are there factual errors?

**When to use**: High-stakes decisions, content creation, code generation

**Cost**: 2-3× base cost (initial + evaluation + refinement)

## Mermaid Diagram Guidelines

### Flowchart Syntax for Agent Workflows
```
graph TD
    A[User Request] --> B{Agent Decision}
    B -->|Condition 1| C[Specialist Agent 1]
    B -->|Condition 2| D[Specialist Agent 2]
    C --> E[Action Execution]
    D --> E
    E --> F[Result Aggregation]
    F --> G[Response to User]
```

### Sequence Diagrams for Agent Interactions
```
sequenceDiagram
    participant U as User
    participant C as Coordinator
    participant S1 as Specialist 1
    participant S2 as Specialist 2
    U->>C: Request
    C->>S1: Task Assignment
    C->>S2: Task Assignment
    S1->>C: Result 1
    S2->>C: Result 2
    C->>U: Final Response
```

### Component Diagrams for System Architecture
```
componentDiagram
    component "User Interface" as UI
## Cost Estimation Formulas

### Token-Based Pricing
**Anthropic Claude Pricing (as of 2024)**:
- **Claude 3.5 Sonnet**:
  - Input: $3 per million tokens ($0.003 per 1K)
  - Output: $15 per million tokens ($0.015 per 1K)
- **Claude 3 Haiku** (fast, cheap):
  - Input: $0.25 per million tokens ($0.00025 per 1K)
  - Output: $1.25 per million tokens ($0.00125 per 1K)

### Workflow Cost Calculation

**Formula**:
```
Cost = (Requests/Month) × (Tokens/Request) × (Price/Token)
```

**Token estimation**:
- Short prompt (simple question): 50-200 tokens
- Medium prompt (with context): 500-1,500 tokens
- Long prompt (with documents): 3,000-8,000 tokens
- Agent system prompt: 1,000-3,000 tokens (added to each call)

**Example: Customer Support**:
```
Volume: 15,000 queries/month
Tokens per query:
  - System prompt: 2,000 tokens (input)
  - User query: 100 tokens (input)
  - Knowledge retrieval: 3,000 tokens (input)
  - Response: 500 tokens (output)
  
Cost = 15,000 × ((2,000+100+3,000) × $0.003 + 500 × $0.015)
     = 15,000 × ($0.0153 + $0.0075)
     = 15,000 × $0.0228
     = $342/month
```

### Multi-Agent Cost Optimization

**Tiered LLM Strategy**:
- Use **Haiku** for: Routing, classification, simple lookups
- Use **Sonnet** for: Complex reasoning, content generation

**Example: Research System**:
```
100 research queries/month:
  - Router (Haiku): 100 × 300 tokens × $0.00025 = $0.08
  - 3 parallel searches (Haiku): 300 × 500 tokens × $0.00025 = $0.38
  - Synthesis (Sonnet): 100 × 4,000 tokens × $0.003 = $1.20
  
Total: $1.66/month (vs $4.80 with all-Sonnet)
Savings: 65%
```

**Caching Strategy**:
- Cache hit rate: 70-80% typical
- Cost reduction: 1 / (1 - hit_rate) = 3-5× cheaper

### Infrastructure Costs

**AWS Fargate** (per task/month, US-East):
- 0.5 vCPU, 1GB RAM: ~$15
- 1 vCPU, 2GB RAM: ~$30
- 2 vCPU, 4GB RAM: ~$60

**Database**:
- RDS PostgreSQL (db.t4g.small): $25/month
- ElastiCache Redis (cache.t4g.small): $20/month
- DocumentDB (MongoDB-compatible, t3.medium): $70/month

**Total typical cost**:
```
LLM: $300-1,500/month (varies by volume)
Compute: $30-120/month (3-4 containers)
Database: $65-150/month
Monitoring: $10-50/month (CloudWatch, Sentry)
----------------------------------------------
Total: $405-1,820/month
```

## Performance Benchmarks

### Latency Targets
| Agent Type | Target Latency | Typical Range |
|------------|---------------|---------------|
| Simple Routing | <100ms | 50-150ms |
| Classification | <200ms | 100-300ms |
| Information Retrieval | <500ms | 300-800ms |
| Content Generation | <2s | 1-4s |
| Complex Analysis | <5s | 3-10s |
| Multi-Agent Workflow | <10s | 5-20s |

### Throughput Expectations
- **Single agent**: 10-50 requests/second (with caching)
- **Multi-agent**: 5-20 requests/second
- **With database**: Limited by DB (1,000-10,000 queries/second)
- **With LLM API**: Limited by rate limits (Anthropic: 5,000 req/min)

### Token Throughput
- **Claude 3.5 Sonnet**: ~100 tokens/second output
- **Claude 3 Haiku**: ~200-400 tokens/second output

### Optimization Strategies
1. **Parallel execution**: Run independent agents concurrently (2-3× faster)
2. **Streaming**: Stream LLM responses for perceived speed
3. **Caching**: Cache frequent queries (70-80% hit rate typical)
4. **Batching**: Process multiple requests together (10-50% cost reduction)
5. **Smart routing**: Use fast models for simple tasks

## Common Anti-Patterns & Solutions

### ❌ Anti-Pattern 1: Too Many Agents
**Problem**: Creating 10+ specialized agents for simple tasks
**Symptoms**: High latency, complex orchestration, debugging nightmares
**Solution**: Start with 2-4 agents, add more only when complexity justifies it

### ❌ Anti-Pattern 2: Synchronous Cascades
**Problem**: Agent1 → wait → Agent2 → wait → Agent3 (serial processing)
**Symptoms**: 3× latency, poor user experience
**Solution**: Parallelize independent operations, use async/await patterns

### ❌ Anti-Pattern 3: No Error Handling
**Problem**: Assuming all LLM calls and API calls succeed
**Symptoms**: System crashes on network issues, API rate limits, malformed responses
**Solution**: Implement retry logic (exponential backoff), fallbacks, circuit breakers

### ❌ Anti-Pattern 4: Infinite Loops
**Problem**: Agent calls itself or creates circular dependencies
**Symptoms**: Runaway costs, system hangs, timeout errors
**Solution**: Max iteration limits (e.g., 5 reasoning steps), cycle detection

### ❌ Anti-Pattern 5: Ignoring Token Limits
**Problem**: Sending massive context to LLM without consideration
**Symptoms**: API errors (context too large), high costs, slow responses
**Solution**: Implement context windowing, summarization, relevance filtering

### ❌ Anti-Pattern 6: No Human-in-the-Loop
**Problem**: Fully automated system for high-stakes decisions
**Symptoms**: Errors propagate without oversight, compliance issues
**Solution**: Add approval gates, confidence thresholds, human review for critical actions

### ❌ Anti-Pattern 7: Over-Engineered Memory
**Problem**: Storing everything forever in vector databases
**Symptoms**: High storage costs, slow retrieval, irrelevant context
**Solution**: TTL policies (expire old data), relevance scoring, tiered storage

### ✅ Best Practices Checklist
- [ ] Start simple: 1-3 agents maximum for MVP
- [ ] Implement comprehensive logging (all agent interactions)
- [ ] Add retry logic with exponential backoff (3-5 attempts)
- [ ] Set timeout limits (5-30 seconds depending on complexity)
- [ ] Monitor token usage and costs in real-time
- [ ] Implement circuit breakers for external APIs
- [ ] Add fallback responses for error scenarios
- [ ] Use tiered LLM strategy (Haiku for simple, Sonnet for complex)
- [ ] Cache frequent queries (70-80% hit rate target)
- [ ] Parallelize independent operations
- [ ] Add human review for high-stakes decisions
- [ ] Test with realistic data volumes
- [ ] Plan for 10× scale from day one

## Real-World Integration Patterns

### Webhook Integration
```python
@app.post("/webhook/support-ticket")
async def handle_ticket(ticket: Ticket):
    # Trigger agent workflow
    result = await agent_system.process(ticket)
    
    # Update external system
    await update_crm(ticket.id, result)
    
    return {"status": "processed"}
```

### Queue Processing
```python
# Worker consumes from queue
async def process_queue():
    async for message in queue.consume():
        result = await agent.handle(message)
        await queue.ack(message)
        await publish_result(result)
```

### Streaming Responses
```python
async def stream_agent_response(query: str):
    async for chunk in agent.stream(query):
        yield f"data: {json.dumps(chunk)}\n\n"
```

---

**Related Documentation**:
- See SKILL.md for usage instructions
- See EXAMPLES.md for complete architecture examples
    AO --> SA2
    AO --> MS
    SA1 --> TI
    SA2 --> TI
```

## MCP (Model Context Protocol) Configuration

### Basic MCP Configuration Structure
```json
{
  "version": "1.0",
  "name": "agentic-architecture",
  "description": "Configuration for multi-agent system",
  "endpoints": {
    "assistant": {
      "path": "/assistant",
      "methods": ["POST"]
    }
  },
  "tools": [
    {
      "name": "agent_tool",
      "description": "Tool for agent communication",
      "input_schema": {
        "type": "object",
        "properties": {
          "agent_id": {"type": "string"},
          "message": {"type": "string"}
        }
      }
    }
  ],
  "resources": {
    "memory": {
      "type": "vector_store",
      "config": {
        "dimension": 1536,
        "distance_function": "cosine"
      }
    }
  }
}
```

### Security and Authentication Settings
- API key management
- Rate limiting configurations
- Role-based access control
- Encryption settings for data in transit and at rest

## Architecture Design Patterns

### Centralized Orchestration Pattern
- Single coordinator agent manages all other agents
- Good for predictable workflows
- Potential bottleneck at the coordinator

### Decentralized Pattern
- Agents communicate directly with each other
- More resilient to failures
- Complex coordination logic

### Hybrid Pattern
- Combines centralized and decentralized approaches
- Coordinator for high-level orchestration
- Direct communication for specific tasks

## Implementation Considerations

### Scalability Factors
- Horizontal vs. vertical scaling strategies
- Load balancing between agent instances
- State management in distributed systems
- Caching strategies for performance

### Monitoring and Observability
- Agent performance metrics
- Error rates and failure tracking
- Response time monitoring
- Resource utilization tracking
- Traceability across agent interactions

### Security Measures
- Input validation and sanitization
- Secure communication between agents
- Access control and authentication
- Data privacy and compliance
- Audit logging for compliance

## Documentation Standards

### Architecture Document Structure
1. Executive Summary
2. System Overview
3. Component Architecture
4. Data Flow Diagrams
5. Security Architecture
6. Scalability Considerations
7. Monitoring and Observability
8. Implementation Roadmap

### Implementation Notes Structure
1. Prerequisites and Dependencies
2. Setup and Configuration
3. Component Implementation
4. Integration Points
5. Testing Strategy
6. Deployment Instructions
7. Operational Guidelines
8. Troubleshooting Guide