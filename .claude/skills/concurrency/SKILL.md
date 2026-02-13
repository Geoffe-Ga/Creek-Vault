---
name: concurrency
description: >-
  Safe and efficient concurrent code patterns across languages.
  Use when implementing async operations, parallel processing,
  thread pools, or managing shared state. Covers Python asyncio,
  TypeScript Promises, Go goroutines, and Rust Tokio.
  Do NOT use for general performance optimization without concurrency.
metadata:
  author: Geoff
  version: 1.0.0
---

# Concurrency

Prefer immutability, minimize shared state, use structured concurrency, handle cancellation explicitly.

## Instructions

### Step 1: Choose the Right Concurrency Model

- **I/O-bound** (network, file, database): Use async/await
- **CPU-bound with I/O**: Use thread pools
- **True parallelism (CPU-intensive)**: Use process pools or language-native parallelism

### Step 2: Apply Language-Specific Patterns

See `references/patterns-by-language.md` for detailed patterns.

Key rules:
- Always handle errors in concurrent tasks (never fire-and-forget)
- Use context managers / structured concurrency for cleanup
- Limit concurrency with semaphores (don't spawn unlimited tasks)
- Use channels/queues for communication, not shared state

### Step 3: Avoid Anti-Patterns

- **Fire and forget**: Always await or handle task results
- **Unhandled rejections**: Catch errors in every concurrent branch
- **Race conditions**: Use locks for shared state, or eliminate shared state
- **Blocking in async**: Never call blocking I/O in async code

## Examples

### Example 1: Python asyncio - Concurrent HTTP Fetches

```python
async def process_multiple_urls(urls: list[str]) -> list[dict]:
    tasks = [fetch_data(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    valid_results = []
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Failed to fetch: {result}")
        else:
            valid_results.append(result)
    return valid_results
```

### Example 2: Go - Goroutines with WaitGroup

```go
func processItems(items []Item) []Result {
    results := make([]Result, len(items))
    var wg sync.WaitGroup
    for i, item := range items {
        wg.Add(1)
        go func(index int, it Item) {
            defer wg.Done()
            results[index] = processItem(it)
        }(i, item)
    }
    wg.Wait()
    return results
}
```

## Troubleshooting

### Error: Tasks silently failing
- Check if you're using `asyncio.gather(*tasks, return_exceptions=True)` and actually inspecting the results
- In Go, ensure goroutine panics are recovered
- In TypeScript, add `.catch()` to every Promise or use try/catch with await

### Error: Deadlock or hang
- Check for lock ordering issues (always acquire locks in the same order)
- Look for await inside a lock that another task needs
- Use timeouts on all blocking operations
