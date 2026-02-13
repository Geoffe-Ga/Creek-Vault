# Concurrency Patterns by Language

## Python

### asyncio (I/O-bound)
```python
async def fetch_data(url: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.json()

async def process_multiple_urls(urls: list[str]) -> list[dict]:
    tasks = [fetch_data(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if not isinstance(r, Exception)]
```

### ThreadPoolExecutor (CPU-bound with I/O)
```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def process_files_concurrently(file_paths: list[Path]) -> list[dict]:
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(process_file, p): p for p in file_paths}
        results = []
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                logger.error(f"Failed {futures[future]}: {e}")
        return results
```

### ProcessPoolExecutor (true parallelism)
```python
from concurrent.futures import ProcessPoolExecutor

def parallel_processing(datasets: list[bytes]) -> list[int]:
    with ProcessPoolExecutor() as executor:
        return list(executor.map(cpu_intensive_task, datasets))
```

## TypeScript

### Promise.all with error handling
```typescript
async function fetchMultipleResources(urls: string[]): Promise<Array<Resource | null>> {
  return Promise.all(urls.map(async (url) => {
    try {
      const response = await fetch(url);
      return await response.json();
    } catch (error) {
      console.error(`Failed to fetch ${url}:`, error);
      return null;
    }
  }));
}
```

### Worker threads (CPU-bound)
```typescript
import { Worker } from 'worker_threads';

function runWorker(workerData: unknown): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const worker = new Worker('./worker.js', { workerData });
    worker.on('message', resolve);
    worker.on('error', reject);
    worker.on('exit', (code) => {
      if (code !== 0) reject(new Error(`Worker exit code ${code}`));
    });
  });
}
```

## Go

### Goroutines with sync.WaitGroup
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

### Context for cancellation
```go
func fetchWithTimeout(ctx context.Context, url string) (*Response, error) {
    ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
    defer cancel()
    req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
    if err != nil { return nil, err }
    return http.DefaultClient.Do(req)
}
```

## Rust

### Tokio JoinSet
```rust
async fn process_items(items: Vec<Item>) -> Vec<Result<Output, Error>> {
    let mut set = JoinSet::new();
    for item in items {
        set.spawn(async move { process_item(item).await });
    }
    let mut results = Vec::new();
    while let Some(result) = set.join_next().await {
        results.push(result.unwrap());
    }
    results
}
```

### Channels
```rust
use tokio::sync::mpsc;

async fn producer_consumer() {
    let (tx, mut rx) = mpsc::channel(100);
    tokio::spawn(async move {
        for i in 0..10 { tx.send(i).await.unwrap(); }
    });
    while let Some(value) = rx.recv().await {
        println!("Received: {}", value);
    }
}
```

## Resource Management

```python
# Always clean up with context managers
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
        try: await task
        except asyncio.CancelledError: pass
```

## Performance Considerations

1. Don't create too many concurrent tasks (use semaphores)
2. Batch operations when possible
3. Use connection pooling
4. Consider backpressure mechanisms
5. Profile before optimizing
