# Concurrency Skill

## Purpose
Safe and efficient concurrent code patterns across different programming languages.

## Principles
1. Prefer immutability
2. Minimize shared state
3. Use structured concurrency
4. Explicit cancellation handling
5. Proper resource cleanup
6. Avoid blocking operations in async code

## Patterns by Language

### Python
**asyncio - Preferred for I/O-bound operations**
```python
import asyncio
from contextlib import asynccontextmanager

async def fetch_data(url: str) -> dict:
    """Fetch data from URL asynchronously."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.json()

async def process_multiple_urls(urls: list[str]) -> list[dict]:
    """Process multiple URLs concurrently with proper error handling."""
    tasks = [fetch_data(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter out exceptions and log errors
    valid_results = []
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Failed to fetch: {result}")
        else:
            valid_results.append(result)

    return valid_results
```

**ThreadPoolExecutor - For CPU-bound with I/O**
```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def process_file(file_path: Path) -> dict:
    """CPU-bound processing of a single file."""
    content = file_path.read_text()
    return analyze_content(content)

def process_files_concurrently(file_paths: list[Path]) -> list[dict]:
    """Process multiple files using thread pool."""
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(process_file, path): path
                  for path in file_paths}

        results = []
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to process {futures[future]}: {e}")

        return results
```

**ProcessPoolExecutor - For true parallelism**
```python
from concurrent.futures import ProcessPoolExecutor

def cpu_intensive_task(data: bytes) -> int:
    """CPU-intensive computation."""
    return complex_calculation(data)

def parallel_processing(datasets: list[bytes]) -> list[int]:
    """Process datasets in parallel using multiple processes."""
    with ProcessPoolExecutor() as executor:
        results = list(executor.map(cpu_intensive_task, datasets))
    return results
```

### TypeScript
**Promises with proper error handling**
```typescript
async function fetchMultipleResources(
  urls: string[]
): Promise<Array<Resource | null>> {
  const promises = urls.map(async (url) => {
    try {
      const response = await fetch(url);
      return await response.json();
    } catch (error) {
      console.error(`Failed to fetch ${url}:`, error);
      return null;
    }
  });

  return await Promise.all(promises);
}
```

**Worker threads for CPU-bound operations**
```typescript
import { Worker } from 'worker_threads';

function runWorker(workerData: unknown): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const worker = new Worker('./worker.js', { workerData });

    worker.on('message', resolve);
    worker.on('error', reject);
    worker.on('exit', (code) => {
      if (code !== 0) {
        reject(new Error(`Worker stopped with exit code ${code}`));
      }
    });
  });
}
```

### Go
**Goroutines with proper synchronization**
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

**Context for cancellation**
```go
func fetchWithTimeout(ctx context.Context, url string) (*Response, error) {
    ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
    defer cancel()

    req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
    if err != nil {
        return nil, err
    }

    return http.DefaultClient.Do(req)
}
```

### Rust
**async/await with Tokio**
```rust
use tokio::task::JoinSet;

async fn process_items(items: Vec<Item>) -> Vec<Result<Output, Error>> {
    let mut set = JoinSet::new();

    for item in items {
        set.spawn(async move {
            process_item(item).await
        });
    }

    let mut results = Vec::new();
    while let Some(result) = set.join_next().await {
        results.push(result.unwrap());
    }

    results
}
```

**Channels for communication**
```rust
use tokio::sync::mpsc;

async fn producer_consumer() {
    let (tx, mut rx) = mpsc::channel(100);

    // Producer task
    tokio::spawn(async move {
        for i in 0..10 {
            tx.send(i).await.unwrap();
        }
    });

    // Consumer task
    while let Some(value) = rx.recv().await {
        println!("Received: {}", value);
    }
}
```

## Anti-patterns

### Fire and Forget
**Bad:**
```python
# Task started but never awaited
asyncio.create_task(some_operation())  # Potential exception lost
```

**Good:**
```python
# Proper task management
task = asyncio.create_task(some_operation())
try:
    await task
except Exception as e:
    logger.error(f"Task failed: {e}")
```

### Unhandled Rejections
**Bad:**
```typescript
// Promise rejection not handled
Promise.all(urls.map(fetch));  // Exceptions lost
```

**Good:**
```typescript
// Proper error handling
try {
  await Promise.all(urls.map(async (url) => {
    try {
      return await fetch(url);
    } catch (error) {
      console.error(`Failed: ${url}`, error);
      return null;
    }
  }));
} catch (error) {
  console.error('Critical error:', error);
}
```

### Race Conditions
**Bad:**
```python
# Shared state without synchronization
counter = 0

async def increment():
    global counter
    counter += 1  # Race condition!
```

**Good:**
```python
# Use asyncio.Lock for shared state
import asyncio

counter = 0
counter_lock = asyncio.Lock()

async def increment():
    global counter
    async with counter_lock:
        counter += 1
```

## Resource Management

### Always clean up resources
```python
# Use context managers
async with aiohttp.ClientSession() as session:
    async with session.get(url) as response:
        return await response.json()

# Or ensure cleanup in finally
task = None
try:
    task = asyncio.create_task(operation())
    await task
finally:
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
```

## Testing Concurrent Code
- Use deterministic scheduling when possible
- Test timeout scenarios
- Test cancellation handling
- Use race condition detectors (ThreadSanitizer, etc.)
- Mock time for testing timeouts

## Performance Considerations
1. Don't create too many concurrent tasks (use semaphores for limiting)
2. Batch operations when possible
3. Use connection pooling
4. Consider backpressure mechanisms
5. Profile before optimizing
