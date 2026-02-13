---
name: tracer-code
description: >-
  Tracer code development methodology for building working systems
  incrementally. Use when starting complex projects, working under
  time constraints, doing interview coding sessions, or when you need
  a demoable application at every stage. Wire the skeleton first,
  then replace stubs with real logic one at a time.
  Do NOT use for small bug fixes or single-function tasks.
metadata:
  author: Geoff
  version: 1.0.0
---

# Tracer Code Development

Wire the entire system end-to-end with stubs, then iteratively replace them with real implementations - always maintaining a working, demoable application.

## Instructions

### Phase 1: Wire the Skeleton (10-15% of time budget)

1. **Define the API surface** - All endpoints with request/response models
2. **Stub every endpoint** - Return mock/hardcoded data matching response models
3. **Connect all layers** - Router -> Service -> Model, even if one-liners
4. **Verify it runs** - Hit every endpoint, confirm valid responses
5. **Write smoke tests** - One test per endpoint proving it returns 200

```python
# Stubbed endpoint example
@router.post("/billing/calculate", response_model=BillingResponse)
async def calculate_billing(request: BillingRequest) -> BillingResponse:
    # TODO: implement real calculation
    return BillingResponse(cost=0.0, currency="USD")
```

**Gate check**: All tests pass. You now have a demoable skeleton.

### Phase 2: Prioritize and Iterate (75-80% of time budget)

Replace stubs with real implementations one at a time, in priority order:

1. Rank features by demo impact
2. For each feature: write failing test -> implement -> verify -> commit
3. Never break the skeleton - if stuck, keep the stub and move on
4. Reassess priority after each feature

**Priority heuristic**:
- **P0**: Core business logic (the thing they asked you to build)
- **P1**: Input validation and meaningful error responses
- **P2**: Edge cases and secondary features
- **P3**: Nice-to-haves (logging, metrics, advanced error handling)

### Phase 3: Polish (5-10% of time budget)

- Add edge case tests for implemented features
- Improve error messages
- Clean up remaining TODOs in implemented code
- Do NOT start new features - polish what works

### Decision Framework

At any point, ask: "If the clock ran out right now, would I have something to demo?"
- **Yes** -> Keep going, pick next highest-impact feature
- **No** -> Stop. Get back to green. Stub it out and move on.

## Examples

### Example 1: Building a Billing API

**Phase 1** (skeleton):
```python
class BillingService:
    def calculate(self, impressions: int, cpm: float) -> float:
        return 0.0  # TODO: implement
```

**Phase 2** (real logic):
```python
class BillingService:
    def calculate(self, impressions: int, cpm: float) -> float:
        return impressions * (cpm / 1000)
```

### Example 2: Interview Time Management

1. **Minutes 0-10**: Wire skeleton (all endpoints return stubs)
2. **Demo to interviewer**: "All endpoints respond with correct shapes"
3. **Minutes 10-50**: Implement P0 features with TDD
4. **Minutes 50-60**: Polish, add edge case tests

## Troubleshooting

### Error: Feature is harder than expected and breaking the skeleton
- Revert the change immediately
- Keep the stub for that feature
- Move to the next priority item
- Come back to it if time remains

### Error: Spending too long on one feature
- Check the clock. If you've spent >25% of remaining time on one feature, stub it
- A working skeleton with 3 real features beats a broken app with 1 perfect feature
