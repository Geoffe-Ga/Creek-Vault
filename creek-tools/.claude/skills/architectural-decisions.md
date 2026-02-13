# Architectural Decisions Skill

**Skill Name**: `architectural-decisions`
**Purpose**: Guide explicit, factual analysis of architectural trade-offs before implementation
**When to use**: When choosing between different approaches, patterns, libraries, or technologies

---

## Core Principle

**Never make architectural decisions unilaterally. Always present options with explicit, factual trade-off analysis.**

When faced with an architectural choice:
1. Identify 2-4 viable options
2. Analyze factual trade-offs (not opinions)
3. Present to user for decision
4. Document the chosen approach

---

## Decision Framework

### 1. Identify the Decision Point

State clearly what needs to be decided:
```markdown
**Decision Required**: [Specific choice to be made]
**Context**: [Why this decision matters now]
**Impact**: [What will be affected by this choice]
```

### 2. Present Options (2-4 alternatives)

For each option, provide:
- **Name**: Clear, descriptive label
- **Description**: What this approach entails
- **Pros**: Factual advantages (measurable when possible)
- **Cons**: Factual disadvantages (measurable when possible)
- **Implementation Effort**: Estimated complexity
- **Maintenance Impact**: Long-term considerations

### 3. Comparison Matrix

Provide a table comparing options across key dimensions:
- Performance characteristics
- Development time
- Maintenance burden
- Scalability implications
- Testing complexity
- Learning curve
- Community support

### 4. Recommendation (Optional)

If one option is clearly superior for the context, state:
```markdown
**Recommended**: Option X
**Rationale**: [Factual reasoning based on project constraints]
```

But always let the user make the final decision.

---

## Template: Architectural Decision

```markdown
## Architectural Decision: [Topic]

**Context**: [Why we need to decide this now]
**Impact**: [What parts of the system are affected]

---

### Option 1: [Approach Name]

**Description**: [What this approach involves]

**Pros**:
- ✅ [Factual advantage 1 with metrics/data]
- ✅ [Factual advantage 2 with metrics/data]
- ✅ [Factual advantage 3 with metrics/data]

**Cons**:
- ❌ [Factual disadvantage 1 with metrics/data]
- ❌ [Factual disadvantage 2 with metrics/data]
- ❌ [Factual disadvantage 3 with metrics/data]

**Implementation**: [Complexity: Low/Medium/High]
**Maintenance**: [Impact on long-term maintenance]

---

### Option 2: [Approach Name]

**Description**: [What this approach involves]

**Pros**:
- ✅ [Factual advantage 1]
- ✅ [Factual advantage 2]
- ✅ [Factual advantage 3]

**Cons**:
- ❌ [Factual disadvantage 1]
- ❌ [Factual disadvantage 2]
- ❌ [Factual disadvantage 3]

**Implementation**: [Complexity: Low/Medium/High]
**Maintenance**: [Impact on long-term maintenance]

---

### Option 3: [Approach Name]

(If applicable)

---

### Comparison Matrix

| Criterion | Option 1 | Option 2 | Option 3 |
|-----------|----------|----------|----------|
| Performance | [Metric] | [Metric] | [Metric] |
| Dev Time | [Estimate] | [Estimate] | [Estimate] |
| Complexity | [Rating] | [Rating] | [Rating] |
| Scalability | [Assessment] | [Assessment] | [Assessment] |
| Test Coverage | [Impact] | [Impact] | [Impact] |
| Dependencies | [Count/Risk] | [Count/Risk] | [Count/Risk] |

---

### Recommendation

**Recommended**: Option X
**Rationale**: [Factual reasoning based on stated constraints]

**Alternative**: If [specific constraint], consider Option Y instead.

---

**Question for User**: Which option should we proceed with?
```

---

## Example 1: Database Choice

```markdown
## Architectural Decision: Database for User Data

**Context**: Need to persist user data (users, sessions, preferences)
**Impact**: Data layer, API response times, operational complexity

---

### Option 1: PostgreSQL (Relational)

**Description**: Traditional relational database with ACID guarantees

**Pros**:
- ✅ **Strong consistency guarantees (ACID transactions)**: Every write is immediately visible to all readers. Critical when user data must be accurate across concurrent requests (e.g., preventing duplicate signups, ensuring session validity). Eliminates entire class of race condition bugs.

- ✅ **Rich query capabilities (JOINs, aggregations, full-text search)**: Can fetch related data in single query (e.g., user + preferences + sessions). Reduces API roundtrips. Full-text search built-in eliminates need for separate search service for basic use cases.

- ✅ **Mature ecosystem (ORMs: SQLAlchemy, Django ORM)**: Extensive documentation, battle-tested patterns, large community. Reduces development time - common problems already solved. Type-safe queries catch bugs at development time.

- ✅ **Data integrity via foreign keys and constraints**: Database enforces referential integrity (can't delete user with active sessions). Prevents orphaned data. Validation happens at DB layer, not just application - protects against bugs and direct DB modifications.

- ✅ **Well-understood operational patterns**: Standard backup/restore, replication, monitoring tools. Easy to find DevOps expertise. Established best practices for performance tuning.

**Cons**:
- ❌ **Vertical scaling limitations (single-server bottleneck)**: Write throughput capped at ~15K-20K transactions/sec on single server. Requires more expensive hardware to scale up. Read replicas help with reads, but writes still bottleneck on primary. Matters when write volume exceeds single server capacity.

- ❌ **Schema migrations required for changes**: Adding/changing columns requires migration scripts. Downtime or careful coordination for schema changes. Slows iteration speed compared to schemaless approaches. Rollbacks more complex when schema changed.

- ❌ **More complex local development setup**: Requires PostgreSQL installation, separate process. Docker helps but adds tooling. Slower test startup (DB initialization). Matters for development velocity.

- ❌ **Higher resource usage for simple key-value operations**: Overhead of transaction log, indexes, query planner for simple gets. ~100-200 bytes per row minimum vs ~50 bytes in key-value stores. Matters at very high scale or resource-constrained environments.

**Implementation**: Low (SQLAlchemy already in common use)
**Maintenance**: Low (well-documented, stable)

**When This Matters**:
- Data integrity is critical (user accounts, financial data)
- Complex queries needed (analytics, reporting)
- Write volume < 10K requests/sec
- Team has SQL expertise

---

### Option 2: MongoDB (Document)

**Description**: Document database with flexible schema

**Pros**:
- ✅ **Flexible schema (easy to add fields without migrations)**: Can add new fields to documents without altering existing data. Enables rapid iteration - deploy new features without coordinated schema migrations. Different documents can have different structures. Reduces deployment coordination overhead.

- ✅ **Horizontal scaling built-in (sharding)**: Write throughput scales linearly with servers (~30K writes/sec per shard). Can handle 100K+ writes/sec by adding shards. Matters when single-server capacity exceeded and vertical scaling too expensive.

- ✅ **Good performance for document retrieval**: Optimized for fetching entire documents. Single query gets all user data vs multiple JOINs. ~2-3x faster than relational for document-centric access patterns. Matters for read-heavy APIs fetching complete objects.

- ✅ **JSON-native (matches API data structures)**: Documents stored as JSON, no ORM mapping overhead. API request → DB → API response with minimal transformation. Simplifies code - fewer translation layers.

**Cons**:
- ❌ **Eventual consistency by default (can cause data anomalies)**: Writes may not be immediately visible to all readers. Can see stale data briefly (milliseconds to seconds). User might not see their own write immediately. Requires application-level handling of inconsistencies. Matters when strict consistency required (auth, payments).

- ❌ **No foreign key constraints (data integrity in application layer)**: Database won't prevent orphaned data (sessions without users). All referential integrity checks must be in code. Bugs or direct DB access can corrupt data. Higher testing burden to ensure integrity.

- ❌ **Less efficient for complex queries (no JOINs)**: Fetching related data requires multiple queries or data duplication. N+1 query problems common. Denormalization necessary (duplicates data across documents). Updates must propagate to duplicates.

- ❌ **Larger disk footprint (data duplication)**: Denormalized data stored redundantly. ~2-3x storage vs normalized schema. Higher storage costs at scale. More data to backup/transfer.

- ❌ **Additional operational complexity**: Sharding adds complexity (shard key choice critical). Different monitoring tools/expertise needed. Smaller community than PostgreSQL for troubleshooting.

**Implementation**: Medium (need new ODM, different patterns)
**Maintenance**: Medium (requires MongoDB expertise)

**When This Matters**:
- Write volume > 20K requests/sec (need horizontal scaling)
- Rapid schema iteration critical
- Document-centric access patterns
- Eventual consistency acceptable

---

### Option 3: SQLite (Embedded)

**Description**: File-based SQL database

**Pros**:
- ✅ Zero configuration (single file)
- ✅ Perfect for development/testing
- ✅ ACID compliant
- ✅ Fast for read-heavy workloads
- ✅ Minimal operational overhead

**Cons**:
- ❌ Single writer limitation (write concurrency bottleneck)
- ❌ Not suitable for distributed systems
- ❌ Limited to single server
- ❌ Migration path to production DB required

**Implementation**: Low (stdlib support)
**Maintenance**: Low for development, High for production migration

---

### Comparison Matrix

| Criterion | PostgreSQL | MongoDB | SQLite | **Why This Matters** |
|-----------|------------|---------|--------|----------------------|
| **Write Throughput** | ~15K/sec | ~30K/sec | ~5K/sec | At 1K req/day (~0.01/sec), all options handle load. At 1M req/day (~12/sec), still well within capacity. Only matters at 100M+ req/day where PostgreSQL might bottleneck. |
| **Concurrent Writes** | High (MVCC) | High (document locking) | Low (single writer) | Multiple users writing simultaneously. SQLite serializes writes - concurrent updates wait in queue. Critical if >10 concurrent write users expected. |
| **Setup Time** | 10 min (Docker) | 15 min (Docker) | 0 min (file) | Affects development velocity. SQLite: instant testing. Others: one-time setup cost. |
| **Data Integrity** | Excellent (FK, constraints) | App-layer only | Excellent (FK, constraints) | PostgreSQL/SQLite prevent orphaned data at DB level. MongoDB requires application code to maintain integrity - more tests needed, bugs possible. |
| **Scalability Path** | Vertical + read replicas | Horizontal (sharding) | None | Future growth strategy. |
| **Operational Cost** | Medium (managed service ~$50/mo) | High (managed service ~$100/mo) | Low (embedded) | Hosting costs. MongoDB cluster more expensive due to replica sets. |
| **Learning Curve** | Low (standard SQL) | Medium (query API) | Low (standard SQL) | Team ramp-up time. SQL skills widely available. MongoDB requires learning new query syntax. Affects hiring and knowledge transfer. |

---

### Recommendation

**Recommended**: PostgreSQL

**Rationale** (context-specific analysis):
- **Data integrity**: User/session data requires strict consistency. PostgreSQL foreign keys prevent orphaned sessions automatically. MongoDB would require application code to maintain this - additional testing burden.
- **Development speed**: SQLAlchemy ORM already familiar - faster than learning MongoDB ODM.
- **Ecosystem**: Well-established patterns, extensive documentation, large community.

**Why not MongoDB**:
- Horizontal scaling capability unused at typical scale
- Eventual consistency adds complexity without benefit
- Extra work maintaining referential integrity in code
- Learning curve for ODM slows development

**Why not SQLite**:
- Single-writer limitation fine for prototyping (not production)
- Good for prototyping, but would need migration for production deployment
- If production plans exist, start with PostgreSQL to avoid migration

**Decision point**: Use PostgreSQL unless rapid prototyping is the only goal (then SQLite, accept migration later).

**Alternative**: SQLite for rapid prototyping if production deployment not planned.

---

**Question for User**: Which database should we use?
```

---

## Example 2: Authentication Strategy

```markdown
## Architectural Decision: User Authentication

**Context**: Need to authenticate users for API endpoints
**Impact**: Security model, session management, API design

---

### Option 1: JWT (Stateless)

**Description**: JSON Web Tokens, server doesn't store session state

**Pros**:
- ✅ **Stateless (no server-side session storage)**: Server validates token cryptographically without database lookup. Reduces infrastructure cost - no Redis/session DB needed. Simplifies deployment - any server can validate any token. Matters when minimizing operational complexity or infrastructure cost.

- ✅ **Scales horizontally (no shared session state)**: Can add servers without session store coordination. Each server independently validates tokens. Perfect for load balancers across multiple servers. Matters when horizontal scaling required (>10K concurrent users).

- ✅ **Contains user claims (reduces DB lookups)**: Token includes user ID, roles, permissions. No database hit to get basic user info. ~50ms saved per request (no DB round-trip). Matters at high request volumes where DB becomes bottleneck.

- ✅ **Works across domains (CORS-friendly)**: Token sent in Authorization header, bypasses cookie domain restrictions. Enables microservices on different domains. Mobile apps can use same auth mechanism. Matters for multi-domain architectures or mobile apps.

**Cons**:
- ❌ **Cannot invalidate tokens before expiry (logout challenges)**: Once issued, token valid until expiration (typically 15-60 minutes). Logout requires maintaining revocation list (defeats stateless benefit) or accepting delay. Compromised token remains valid. Critical security concern - stolen token usable until expiry.

- ❌ **Token size larger than session ID (~200 bytes vs 32 bytes)**: JWT payload + signature ~200 bytes, session ID ~32 bytes. Extra 168 bytes per request. At 1M requests/day = 168MB/day bandwidth. Negligible for small scale, measurable at large scale (costs).

- ❌ **Sent with every request (bandwidth overhead)**: 200 bytes in every HTTP header vs 32-byte cookie. Adds latency on slow connections. Matters for mobile users or high-request-rate APIs.

- ❌ **Requires client-side token storage (XSS risk if in localStorage)**: LocalStorage accessible to JavaScript - vulnerable to XSS attacks. If attacker injects script, can steal token. HttpOnly cookies not accessible to JavaScript (safer). Critical security consideration for user-facing apps.

**Implementation**: Medium (PyJWT library, token refresh logic)
**Maintenance**: Medium (key rotation, token refresh)

**When This Matters**:
- Horizontal scaling required (>10K concurrent users)
- Microservices across multiple domains
- Mobile app authentication needed
- Can tolerate logout delay (not real-time)

---

### Option 2: Session Cookies (Stateful)

**Description**: Server-side sessions with cookie-based session ID

**Pros**:
- ✅ **Small cookie size (32 bytes session ID)**: Minimal bandwidth overhead (32 vs 200 bytes for JWT). Faster transmission on slow connections. Matters for high-request-volume APIs or mobile users.

- ✅ **Easy to invalidate (delete server-side session)**: Logout immediately effective - delete session from store. Compromised session can be revoked instantly. Admin can forcibly logout users. Critical for security - immediate response to threats.

- ✅ **Secure defaults (httpOnly, secure, sameSite cookies)**: HttpOnly prevents JavaScript access (XSS protection). Secure flag ensures HTTPS-only transmission. SameSite prevents CSRF attacks. Browser-enforced security - less code to audit.

- ✅ **Simple logout (clear session)**: Single operation: delete session from store. No token revocation list needed. Clean, predictable behavior. Easier to implement and test correctly.

**Cons**:
- ❌ **Requires session storage (Redis/DB)**: Additional infrastructure component needed. Redis: ~$20/month for managed service or self-hosted complexity. Database table: query overhead on every request. Matters for infrastructure cost and operational complexity.

- ❌ **Sticky sessions or shared session store for scaling**: Load balancer must route user to same server (sticky sessions) OR all servers share session store (Redis). Sticky sessions: server failure loses sessions. Shared store: single point of failure, network latency. Matters when horizontal scaling needed.

- ❌ **CSRF protection needed (extra complexity)**: Cookies sent automatically by browser - vulnerable to CSRF. Requires anti-CSRF tokens in forms. Additional middleware, token generation, validation. 50-100 lines of code. Testing overhead.

- ❌ **Not ideal for mobile apps (cookie handling)**: Mobile HTTP clients don't automatically handle cookies like browsers. Manual cookie storage/transmission code needed. More complex mobile implementation vs Authorization header. Matters if mobile app planned.

**Implementation**: Low (Flask-Session, straightforward)
**Maintenance**: Low (well-established pattern)

**When This Matters**:
- Single-server or modest scale (<10K concurrent users)
- Immediate logout critical (security-sensitive)
- Web-only application (no mobile app)
- Infrastructure cost secondary to simplicity

---

### Option 3: API Keys (Static)

**Description**: Long-lived API keys for programmatic access

**Pros**:
- ✅ Simple to implement (just key validation)
- ✅ Good for service-to-service auth
- ✅ Easy to test (curl with header)

**Cons**:
- ❌ No expiration (security risk if leaked)
- ❌ No user context (single permission level)
- ❌ Harder to rotate (all clients need update)
- ❌ Not suitable for user-facing auth

**Implementation**: Low (simple hash verification)
**Maintenance**: Low for service auth, High for user auth

---

### Comparison Matrix

| Criterion | JWT | Session Cookie | API Key | **Why This Matters** |
|-----------|-----|----------------|---------|----------------------|
| **Security** | Medium (XSS risk) | High (httpOnly) | Low (no expiry) | Cookies are httpOnly (browser prevents JavaScript access). JWT in localStorage vulnerable to XSS. Matters for user-facing apps where XSS possible. |
| **Scalability** | Excellent (stateless) | Good (needs Redis) | Excellent | JWT: any server validates. Sessions: need shared Redis. Matters at >10K concurrent users. |
| **Logout** | Hard (wait expiry) | Easy (delete session) | Hard (revocation) | Session: instant logout. JWT: valid until expiry (15-60 min). Critical for compromised account response time. |
| **User Experience** | Good | Excellent | N/A | Sessions: immediate logout, clear session expiry. JWT: delayed logout, token refresh complexity. Affects user perception of security. |
| **Mobile Support** | Excellent (header) | Limited (cookie handling) | Good | Mobile apps prefer Authorization headers. Cookie handling requires manual implementation. Matters if mobile app planned. |
| **Bandwidth** | ~200 bytes/req | ~32 bytes/req | ~64 bytes/req | At 1M req/day: JWT=200MB, Cookie=32MB, saves 168MB/day. Production: bandwidth costs at scale. |
| **Complexity** | Medium (refresh flow) | Low (Flask-Session) | Low | JWT: token refresh, revocation handling. Sessions: Redis setup. Simplicity preferred. |
| **Infrastructure** | None | Redis (~$20/mo) | None | Session needs storage. JWT stateless. Recurring cost consideration. |

---

### Recommendation

**Recommended**: Session Cookies (Option 2)

**Rationale** (context-specific analysis):
- **Security**: User-facing web app = XSS risk exists. HttpOnly cookies eliminate this attack vector. JWT in localStorage vulnerable.
- **Development speed**: Flask-Session = 10 lines of code. JWT = 50+ lines (refresh logic, token validation, error handling).
- **User experience**: Immediate logout expected behavior. Session provides this. JWT's delayed logout (15-60 min) requires explaining to users.

**Why not JWT**:
- Stateless benefit unused (single server, no load balancer)
- XSS vulnerability requires localStorage alternative (adds complexity)
- Token refresh logic adds 50+ LOC without benefit at this scale
- Logout delay poor UX, requires revocation list (defeats stateless benefit)

**Why not API Key**:
- No expiration = security risk for user auth
- No fine-grained permissions (all-or-nothing access)
- Better suited for service-to-service, not user authentication

**Decision point**: Use session cookies unless horizontal scaling or microservices planned (then JWT).

**Alternative**: JWT if mobile app required or microservices architecture planned.

---

**Question for User**: Which authentication approach should we implement?
```

---

## Example 3: Error Handling Pattern

```markdown
## Architectural Decision: Error Response Format

**Context**: Need consistent error responses from API
**Impact**: API contract, client error handling, debugging

---

### Option 1: Problem Details (RFC 7807)

**Description**: Standardized error format with type, title, detail

**Pros**:
- ✅ Industry standard (RFC 7807)
- ✅ Machine-readable error types
- ✅ Extensible (custom fields)
- ✅ Clear contract (documented standard)

**Cons**:
- ❌ More verbose (~100 bytes overhead)
- ❌ Requires library or custom implementation
- ❌ Overkill for simple APIs

**Implementation**: Medium (use problem library or build formatter)
**Maintenance**: Low (standard is stable)

**Example**:
```json
{
  "type": "https://api.example.com/errors/validation",
  "title": "Validation Failed",
  "status": 400,
  "detail": "Email field is required",
  "instance": "/api/users/create"
}
```

---

### Option 2: Simple Error Object

**Description**: Minimal {error, message} format

**Pros**:
- ✅ Minimal payload (~30 bytes)
- ✅ Easy to implement (5 lines of code)
- ✅ Easy to parse (no spec to learn)
- ✅ Sufficient for simple APIs

**Cons**:
- ❌ No standard (clients must guess format)
- ❌ Not extensible (hard to add fields later)
- ❌ Limited debugging info (no error codes)

**Implementation**: Low (trivial)
**Maintenance**: Low

**Example**:
```json
{
  "error": "Validation error",
  "message": "Email field is required"
}
```

---

### Option 3: FastAPI Default

**Description**: Use FastAPI's built-in error responses

**Pros**:
- ✅ Zero implementation (free)
- ✅ Consistent with FastAPI ecosystem
- ✅ Automatic validation errors
- ✅ Good defaults (includes detail)

**Cons**:
- ❌ Not customizable without override
- ❌ Format varies by error type
- ❌ Less control over contract

**Implementation**: Zero (already built-in)
**Maintenance**: Zero (framework-managed)

**Example**:
```json
{
  "detail": "Email field is required"
}
```

---

### Comparison Matrix

| Criterion | Problem Details | Simple Object | FastAPI Default |
|-----------|----------------|---------------|-----------------|
| **Payload Size** | ~100 bytes | ~30 bytes | ~20 bytes |
| **Implementation** | 50 LOC | 5 LOC | 0 LOC |
| **Standardization** | RFC 7807 | Custom | FastAPI convention |
| **Extensibility** | Excellent | Poor | Limited |
| **Client Parsing** | Well-defined | Varies | Framework-specific |
| **Debugging Info** | Rich | Minimal | Moderate |

---

### Recommendation

**Recommended**: FastAPI Default (Option 3)
**Rationale**:
- Zero implementation cost
- FastAPI convention well-documented
- Sufficient detail for debugging
- Can enhance later if needed

**Alternative**: Problem Details (Option 1) if building a public API with many clients.

---

**Question for User**: Which error format should we use?
```

---

## When to Use This Skill

Use `architectural-decisions` skill when:

- ✅ Choosing between libraries (ORM, HTTP client, validation)
- ✅ Selecting data structures (list vs dict, sync vs async)
- ✅ Picking architectural patterns (MVC, repository, service layer)
- ✅ Deciding on infrastructure (DB, cache, queue)
- ✅ Designing API contracts (REST, GraphQL, gRPC)
- ✅ Choosing testing strategies (unit vs integration)

Don't use for:

- ❌ Variable naming (too granular)
- ❌ Code formatting (use linter)
- ❌ Trivial choices with no trade-offs
- ❌ Decisions already made (follow existing patterns)

---

## Integration with Stay Green

Architectural decisions should be documented in plan files:

1. **Before Implementation**: Present options in `plan/YYYY-MM-DD_FEATURE_NAME.md`
2. **Get User Decision**: Use AskUserQuestion with options
3. **Document Choice**: Record decision and rationale
4. **Proceed with TDD**: Gate 1 (tests first)
5. **Verify Quality**: Gate 2 (pre-commit)

---

## Anti-Patterns

❌ **Don't**:
- Make architectural choices without user input
- Present opinions as facts ("X is better")
- Skip trade-off analysis ("just use X")
- Hide cons of recommended option
- Present 10 options (analysis paralysis)

✅ **Do**:
- State facts with evidence (benchmarks, metrics)
- Present 2-4 realistic options
- Show honest pros/cons for each
- Provide recommendation with rationale
- Let user make final decision
- Document the chosen approach

---

## Factual vs Subjective Analysis

**Factual** (use these):
- "PostgreSQL supports 15K writes/sec on standard hardware"
- "JWT tokens are ~200 bytes, session IDs are ~32 bytes"
- "Option A requires 3 additional dependencies"
- "Implementation estimated at 50 LOC"

**Subjective** (avoid these):
- "PostgreSQL is better"
- "JWT is more modern"
- "Option A is cleaner"
- "This approach feels right"

---

**Remember**: Architecture decisions have long-term impact. Take time to analyze trade-offs explicitly.
