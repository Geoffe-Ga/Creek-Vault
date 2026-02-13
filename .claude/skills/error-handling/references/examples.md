# Error Handling Examples

## Example 1: API Client Error Handling (Python)

```python
class APIError(Exception):
    def __init__(self, message: str, status_code: int | None = None, response_body: str | None = None) -> None:
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(message)

class APIClient:
    def fetch_user(self, user_id: str) -> dict:
        if not user_id or not user_id.strip():
            raise ValueError(f"Invalid user_id: {user_id!r} (must be non-empty)")

        url = f"{self.base_url}/users/{user_id}"

        try:
            response = requests.get(url, timeout=10)
        except requests.Timeout as e:
            raise APIError(f"Request to {url} timed out after 10s") from e
        except requests.ConnectionError as e:
            raise APIError(f"Failed to connect to {url}: {e}") from e

        if response.status_code == 404:
            raise APIError(f"User not found: {user_id}", status_code=404, response_body=response.text)

        if not response.ok:
            raise APIError(f"API error for {url}: HTTP {response.status_code}", status_code=response.status_code)

        try:
            return response.json()
        except ValueError as e:
            raise APIError(f"Invalid JSON from {url}: {e}", status_code=response.status_code) from e
```

## Example 2: File Processing with Cleanup (Python)

```python
def process_file_safely(input_path: Path, output_path: Path, processor: Callable[[bytes], bytes]) -> None:
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path.absolute()}")

    temp_dir = Path(tempfile.mkdtemp(prefix="process_"))
    temp_output = temp_dir / "output"

    try:
        try:
            input_data = input_path.read_bytes()
        except PermissionError as e:
            raise IOError(f"Cannot read {input_path}: permission denied") from e

        try:
            output_data = processor(input_data)
        except Exception as e:
            raise ProcessingError(f"Failed to process {input_path}: {e}") from e

        temp_output.write_bytes(output_data)
        shutil.move(str(temp_output), str(output_path))
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
```

## Example 3: Validation with Error Collection (TypeScript)

```typescript
interface ValidationResult {
  valid: boolean;
  errors: string[];
}

function validateRegistration(data: unknown): ValidationResult {
  const errors: string[] = [];

  if (typeof data !== 'object' || data === null) {
    return { valid: false, errors: ['Expected object, got ' + typeof data] };
  }

  const { email, password, username } = data as Record<string, unknown>;

  if (typeof email !== 'string' || !email.includes('@')) {
    errors.push('Invalid email format');
  }
  if (typeof password !== 'string' || password.length < 8) {
    errors.push('Password must be at least 8 characters');
  }
  if (typeof username !== 'string' || username.length < 3) {
    errors.push('Username must be at least 3 characters');
  }

  return { valid: errors.length === 0, errors };
}
```

## Error Logging Best Practices

```python
# Include structured context
logger.error("Failed to process user data", extra={
    "user_id": user_id,
    "operation": "update_profile",
    "error_type": type(e).__name__,
})

# Use appropriate levels
# ERROR: Operation failed, needs attention
# WARNING: Degraded operation
# INFO: Normal operation events
# DEBUG: Detailed diagnostics

# Include stack traces for debugging
logger.error("Critical error occurred", exc_info=True)
```
