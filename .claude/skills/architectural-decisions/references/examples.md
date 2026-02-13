# Architectural Decision Examples

## Example 1: Database Choice

```markdown
## Architectural Decision: Database for User Data

**Context**: Need to persist user data (users, sessions, preferences)
**Impact**: Data layer, API response times, operational complexity

### Option 1: PostgreSQL (Relational)

**Pros**:
- Strong consistency guarantees (ACID transactions)
- Rich query capabilities (JOINs, aggregations, full-text search)
- Mature ecosystem (SQLAlchemy, Django ORM)
- Data integrity via foreign keys and constraints
- Well-understood operational patterns

**Cons**:
- Vertical scaling limitations (~15K-20K writes/sec single server)
- Schema migrations required for changes
- More complex local development setup
- Higher resource usage for simple key-value operations

**Implementation**: Low (SQLAlchemy already common)
**Maintenance**: Low (well-documented, stable)

### Option 2: MongoDB (Document)

**Pros**:
- Flexible schema (easy to add fields without migrations)
- Horizontal scaling built-in (~30K writes/sec per shard)
- Good performance for document retrieval (~2-3x faster for document-centric patterns)
- JSON-native (matches API data structures)

**Cons**:
- Eventual consistency by default
- No foreign key constraints (integrity in application layer)
- Less efficient for complex queries (no JOINs)
- Larger disk footprint (data duplication from denormalization)

**Implementation**: Medium (need new ODM, different patterns)
**Maintenance**: Medium (requires MongoDB expertise)

### Option 3: SQLite (Embedded)

**Pros**:
- Zero configuration (single file)
- ACID compliant
- Fast for read-heavy workloads

**Cons**:
- Single writer limitation
- Not suitable for distributed systems
- Migration path to production DB required

**Implementation**: Low (stdlib support)

### Comparison Matrix

| Criterion | PostgreSQL | MongoDB | SQLite |
|-----------|------------|---------|--------|
| Write Throughput | ~15K/sec | ~30K/sec per shard | ~5K/sec |
| Data Integrity | Excellent (FK, constraints) | App-layer only | Excellent |
| Scalability | Vertical + read replicas | Horizontal (sharding) | None |
| Operational Cost | ~$50/mo managed | ~$100/mo managed | Free |
| Learning Curve | Low (SQL) | Medium (query API) | Low (SQL) |

### Recommendation

**Recommended**: PostgreSQL
- Data integrity critical for user/session data
- Rich queries needed for analytics
- Write volume well within single-server capacity
- Team has SQL expertise

**Alternative**: SQLite for rapid prototyping if production deployment not planned.
```

---

## Example 2: Authentication Strategy

```markdown
## Architectural Decision: User Authentication

**Context**: Need to authenticate users for API endpoints
**Impact**: Security model, session management, API design

### Option 1: JWT (Stateless)

**Pros**:
- Stateless (no server-side session storage)
- Scales horizontally (no shared session state)
- Contains user claims (reduces DB lookups, ~50ms saved/request)
- Works across domains (CORS-friendly)

**Cons**:
- Cannot invalidate tokens before expiry (logout delay)
- Token size larger (~200 bytes vs 32 bytes session ID)
- XSS risk if stored in localStorage
- Requires client-side token management

**Implementation**: Medium (PyJWT, token refresh logic)

### Option 2: Session Cookies (Stateful)

**Pros**:
- Small cookie size (32 bytes)
- Easy to invalidate (delete server-side session)
- Secure defaults (httpOnly, secure, sameSite)
- Simple implementation

**Cons**:
- Requires session storage (Redis ~$20/mo)
- Sticky sessions or shared store for scaling
- CSRF protection needed
- Not ideal for mobile apps

**Implementation**: Low (Flask-Session)

### Option 3: API Keys (Static)

**Pros**:
- Simple to implement
- Good for service-to-service auth

**Cons**:
- No expiration (security risk if leaked)
- No user context
- Not suitable for user-facing auth

### Comparison Matrix

| Criterion | JWT | Session Cookie | API Key |
|-----------|-----|----------------|---------|
| Security | Medium (XSS risk) | High (httpOnly) | Low (no expiry) |
| Scalability | Excellent (stateless) | Good (needs Redis) | Excellent |
| Logout | Hard (wait expiry) | Easy (delete session) | Hard |
| Mobile Support | Excellent | Limited | Good |
| Bandwidth | ~200 bytes/req | ~32 bytes/req | ~64 bytes/req |

### Recommendation

**Recommended**: Session Cookies
- User-facing web app: httpOnly cookies eliminate XSS
- Flask-Session: 10 LOC vs JWT's 50+ LOC
- Immediate logout is expected behavior
- Single server: stateless benefit unused

**Alternative**: JWT if mobile app required or microservices planned.
```

---

## Example 3: Error Response Format

```markdown
## Architectural Decision: Error Response Format

**Context**: Need consistent error responses from API
**Impact**: API contract, client error handling, debugging

### Option 1: Problem Details (RFC 7807)

**Pros**: Industry standard, machine-readable, extensible
**Cons**: ~100 bytes overhead, requires implementation
**Implementation**: Medium (50 LOC)

Example:
```json
{
  "type": "https://api.example.com/errors/validation",
  "title": "Validation Failed",
  "status": 400,
  "detail": "Email field is required"
}
```

### Option 2: Simple Error Object

**Pros**: Minimal (~30 bytes), trivial to implement
**Cons**: No standard, not extensible
**Implementation**: Low (5 LOC)

### Option 3: FastAPI Default

**Pros**: Zero implementation, consistent with ecosystem
**Cons**: Not customizable, format varies by error type
**Implementation**: Zero

### Comparison Matrix

| Criterion | Problem Details | Simple Object | FastAPI Default |
|-----------|----------------|---------------|-----------------|
| Payload Size | ~100 bytes | ~30 bytes | ~20 bytes |
| Implementation | 50 LOC | 5 LOC | 0 LOC |
| Extensibility | Excellent | Poor | Limited |

### Recommendation

**Recommended**: FastAPI Default
- Zero implementation cost
- Sufficient for current needs
- Can enhance later if building public API
```
