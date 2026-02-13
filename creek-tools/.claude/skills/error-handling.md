# Error Handling Skill

## Purpose
Implement robust, informative error handling that fails fast, provides clear diagnostics, and maintains system integrity.

## Principles
1. Fail fast with clear error messages
2. Use typed exceptions/errors over generic ones
3. Include context in error messages (what, where, why)
4. Never silence errors without logging
5. Validate inputs at boundaries
6. Handle errors at the appropriate abstraction level

## Patterns by Language

### Python
**Typed exceptions with context**
```python
from typing import NoReturn

class ConfigurationError(Exception):
    """Raised when configuration is invalid."""

    def __init__(self, field: str, value: str, reason: str) -> None:
        self.field = field
        self.value = value
        self.reason = reason
        super().__init__(f"Invalid {field}={value!r}: {reason}")

def load_config(path: Path) -> dict[str, str]:
    """Load configuration from file.

    Args:
        path: Path to configuration file

    Returns:
        Parsed configuration dictionary

    Raises:
        FileNotFoundError: If configuration file doesn't exist
        ConfigurationError: If configuration is invalid
        ValueError: If configuration file is empty
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {path.absolute()}"
        )

    content = path.read_text()
    if not content.strip():
        raise ValueError(
            f"Configuration file is empty: {path.absolute()}"
        )

    config = parse_config(content)

    # Validate required fields
    required = {"api_key", "base_url"}
    missing = required - config.keys()
    if missing:
        raise ConfigurationError(
            field="required_fields",
            value=str(missing),
            reason="Missing required configuration fields",
        )

    return config
```

**Context managers for resource cleanup**
```python
from contextlib import contextmanager
from typing import Iterator

@contextmanager
def atomic_write(path: Path) -> Iterator[Path]:
    """Write to temporary file, rename on success.

    Args:
        path: Target file path

    Yields:
        Temporary file path to write to

    Raises:
        IOError: If write or rename fails
    """
    temp_path = path.with_suffix(path.suffix + ".tmp")

    try:
        yield temp_path
        # Only rename if no exception occurred
        temp_path.replace(path)
    except Exception as e:
        # Clean up temporary file on error
        if temp_path.exists():
            temp_path.unlink()
        raise IOError(
            f"Failed to write {path.absolute()}: {e}"
        ) from e
```

**Try-except with specific exceptions**
```python
import json
from pathlib import Path

def load_json_file(path: Path) -> dict:
    """Load JSON file with comprehensive error handling.

    Args:
        path: Path to JSON file

    Returns:
        Parsed JSON data as dictionary

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If JSON is malformed or not a dict
    """
    try:
        content = path.read_text()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"JSON file not found: {path.absolute()}"
        )
    except PermissionError as e:
        raise PermissionError(
            f"Cannot read {path.absolute()}: {e}"
        ) from e

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Invalid JSON in {path.absolute()}: {e.msg} "
            f"at line {e.lineno}, column {e.colno}"
        ) from e

    if not isinstance(data, dict):
        raise ValueError(
            f"Expected JSON object in {path.absolute()}, "
            f"got {type(data).__name__}"
        )

    return data
```

### TypeScript
**Custom error classes**
```typescript
class ValidationError extends Error {
  constructor(
    public readonly field: string,
    public readonly value: unknown,
    message: string
  ) {
    super(`Validation failed for ${field}: ${message}`);
    this.name = 'ValidationError';

    // Maintains proper stack trace
    if (Error.captureStackTrace) {
      Error.captureStackTrace(this, ValidationError);
    }
  }
}

function validateUser(data: unknown): User {
  if (typeof data !== 'object' || data === null) {
    throw new ValidationError(
      'user',
      data,
      'Expected object, got ' + typeof data
    );
  }

  const { email, name } = data as Record<string, unknown>;

  if (typeof email !== 'string' || !email.includes('@')) {
    throw new ValidationError('email', email, 'Invalid email format');
  }

  if (typeof name !== 'string' || name.length < 2) {
    throw new ValidationError('name', name, 'Name must be at least 2 characters');
  }

  return { email, name };
}
```

**Result type pattern**
```typescript
type Result<T, E = Error> =
  | { ok: true; value: T }
  | { ok: false; error: E };

function parseConfig(content: string): Result<Config> {
  try {
    const data = JSON.parse(content);

    if (!isValidConfig(data)) {
      return {
        ok: false,
        error: new Error('Invalid configuration structure'),
      };
    }

    return { ok: true, value: data };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error
        ? error
        : new Error('Unknown error parsing config'),
    };
  }
}

// Usage
const result = parseConfig(fileContents);
if (result.ok) {
  console.log('Config loaded:', result.value);
} else {
  console.error('Failed to load config:', result.error.message);
}
```

**Promise error handling**
```typescript
async function fetchUserData(userId: string): Promise<UserData> {
  try {
    const response = await fetch(`/api/users/${userId}`);

    if (!response.ok) {
      throw new Error(
        `HTTP ${response.status}: ${response.statusText}`
      );
    }

    const data = await response.json();
    return validateUserData(data);
  } catch (error) {
    if (error instanceof ValidationError) {
      throw error;  // Re-throw validation errors
    }

    // Wrap network errors with context
    throw new Error(
      `Failed to fetch user ${userId}: ${
        error instanceof Error ? error.message : 'Unknown error'
      }`
    );
  }
}
```

### Go
**Error wrapping with context**
```go
import (
    "fmt"
    "os"
)

func LoadConfig(path string) (*Config, error) {
    file, err := os.Open(path)
    if err != nil {
        return nil, fmt.Errorf("failed to open config file %q: %w", path, err)
    }
    defer file.Close()

    config, err := parseConfig(file)
    if err != nil {
        return nil, fmt.Errorf("failed to parse config from %q: %w", path, err)
    }

    if err := validateConfig(config); err != nil {
        return nil, fmt.Errorf("invalid config in %q: %w", path, err)
    }

    return config, nil
}
```

**Custom error types**
```go
type ValidationError struct {
    Field  string
    Value  interface{}
    Reason string
}

func (e *ValidationError) Error() string {
    return fmt.Sprintf("validation failed for %s=%v: %s",
        e.Field, e.Value, e.Reason)
}

func ValidateUser(user *User) error {
    if user.Email == "" {
        return &ValidationError{
            Field:  "email",
            Value:  user.Email,
            Reason: "email is required",
        }
    }

    if !strings.Contains(user.Email, "@") {
        return &ValidationError{
            Field:  "email",
            Value:  user.Email,
            Reason: "invalid email format",
        }
    }

    return nil
}
```

**Error checking patterns**
```go
// Check errors immediately
func ProcessFile(path string) error {
    data, err := os.ReadFile(path)
    if err != nil {
        return fmt.Errorf("failed to read %q: %w", path, err)
    }

    result, err := transform(data)
    if err != nil {
        return fmt.Errorf("failed to transform data from %q: %w", path, err)
    }

    if err := validate(result); err != nil {
        return fmt.Errorf("validation failed for %q: %w", path, err)
    }

    if err := saveResult(result); err != nil {
        return fmt.Errorf("failed to save result: %w", err)
    }

    return nil
}
```

### Rust
**Result type with custom errors**
```rust
use std::fmt;
use std::io;

#[derive(Debug)]
pub enum ConfigError {
    NotFound(String),
    ParseError(String),
    ValidationError { field: String, reason: String },
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            ConfigError::NotFound(path) => {
                write!(f, "Configuration file not found: {}", path)
            }
            ConfigError::ParseError(msg) => {
                write!(f, "Failed to parse configuration: {}", msg)
            }
            ConfigError::ValidationError { field, reason } => {
                write!(f, "Validation failed for {}: {}", field, reason)
            }
        }
    }
}

impl std::error::Error for ConfigError {}

impl From<io::Error> for ConfigError {
    fn from(err: io::Error) -> Self {
        ConfigError::ParseError(err.to_string())
    }
}

pub fn load_config(path: &str) -> Result<Config, ConfigError> {
    let content = std::fs::read_to_string(path)
        .map_err(|_| ConfigError::NotFound(path.to_string()))?;

    let config: Config = serde_json::from_str(&content)
        .map_err(|e| ConfigError::ParseError(e.to_string()))?;

    validate_config(&config)?;

    Ok(config)
}

fn validate_config(config: &Config) -> Result<(), ConfigError> {
    if config.api_key.is_empty() {
        return Err(ConfigError::ValidationError {
            field: "api_key".to_string(),
            reason: "API key is required".to_string(),
        });
    }

    Ok(())
}
```

**The ? operator for error propagation**
```rust
use std::fs::File;
use std::io::{self, Read};

fn read_file_contents(path: &str) -> io::Result<String> {
    let mut file = File::open(path)?;  // Propagates error
    let mut contents = String::new();
    file.read_to_string(&mut contents)?;  // Propagates error
    Ok(contents)
}

// With context using .map_err()
fn load_config(path: &str) -> Result<Config, String> {
    let contents = read_file_contents(path)
        .map_err(|e| format!("Failed to read config from {}: {}", path, e))?;

    parse_config(&contents)
        .map_err(|e| format!("Failed to parse config from {}: {}", path, e))
}
```

## Anti-patterns

### Silent Failures
**Bad:**
```python
try:
    process_data(item)
except Exception:
    pass  # Error swallowed!
```

**Good:**
```python
try:
    process_data(item)
except ProcessingError as e:
    logger.error(f"Failed to process {item}: {e}")
    # Re-raise or handle appropriately
    raise
```

### Generic Exception Catching
**Bad:**
```python
try:
    result = risky_operation()
except Exception as e:  # Too broad!
    return None
```

**Good:**
```python
try:
    result = risky_operation()
except (ValueError, KeyError) as e:  # Specific exceptions
    logger.error(f"Invalid input: {e}")
    raise
except IOError as e:  # Different handling
    logger.warning(f"I/O error (retrying): {e}")
    return retry_operation()
```

### Bare Except
**Bad:**
```python
try:
    operation()
except:  # Catches KeyboardInterrupt, SystemExit, etc.!
    handle_error()
```

**Good:**
```python
try:
    operation()
except Exception as e:  # Doesn't catch system exits
    handle_error(e)
```

### Error Messages Without Context
**Bad:**
```python
raise ValueError("Invalid input")  # What input? Where? Why?
```

**Good:**
```python
raise ValueError(
    f"Invalid email format for user '{username}': "
    f"expected user@domain.com, got '{email}'"
)
```

### Returning Error Codes Instead of Exceptions
**Bad:**
```python
def process_data(data: str) -> int:
    """Return 0 on success, -1 on error."""  # Ambiguous!
    if not data:
        return -1
    # ... process ...
    return 0
```

**Good:**
```python
def process_data(data: str) -> Result:
    """Process data and return result.

    Raises:
        ValueError: If data is empty or invalid
    """
    if not data:
        raise ValueError("Data cannot be empty")
    # ... process ...
    return result
```

## Examples

### Example 1: API Client Error Handling
```python
import requests
from typing import Optional

class APIError(Exception):
    """Base exception for API errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(message)

class APIClient:
    """HTTP API client with comprehensive error handling."""

    def fetch_user(self, user_id: str) -> dict:
        """Fetch user data from API.

        Args:
            user_id: User identifier

        Returns:
            User data dictionary

        Raises:
            APIError: If API request fails
            ValueError: If user_id is invalid
        """
        if not user_id or not user_id.strip():
            raise ValueError(
                f"Invalid user_id: {user_id!r} (must be non-empty string)"
            )

        url = f"{self.base_url}/users/{user_id}"

        try:
            response = requests.get(url, timeout=10)
        except requests.Timeout as e:
            raise APIError(
                f"Request to {url} timed out after 10s",
                status_code=None,
            ) from e
        except requests.ConnectionError as e:
            raise APIError(
                f"Failed to connect to {url}: {e}",
                status_code=None,
            ) from e

        if response.status_code == 404:
            raise APIError(
                f"User not found: {user_id}",
                status_code=404,
                response_body=response.text,
            )

        if not response.ok:
            raise APIError(
                f"API error for {url}: HTTP {response.status_code}",
                status_code=response.status_code,
                response_body=response.text,
            )

        try:
            data = response.json()
        except ValueError as e:
            raise APIError(
                f"Invalid JSON response from {url}: {e}",
                status_code=response.status_code,
                response_body=response.text,
            ) from e

        return data
```

### Example 2: File Processing with Cleanup
```python
from pathlib import Path
from typing import Callable
import tempfile
import shutil

class ProcessingError(Exception):
    """Error during file processing."""
    pass

def process_file_safely(
    input_path: Path,
    output_path: Path,
    processor: Callable[[bytes], bytes],
) -> None:
    """Process file with atomic write and error recovery.

    Args:
        input_path: Source file path
        output_path: Destination file path
        processor: Function to process file bytes

    Raises:
        FileNotFoundError: If input file doesn't exist
        ProcessingError: If processing fails
        IOError: If file operations fail
    """
    if not input_path.exists():
        raise FileNotFoundError(
            f"Input file not found: {input_path.absolute()}"
        )

    # Create temporary directory for processing
    temp_dir = Path(tempfile.mkdtemp(prefix="process_"))
    temp_output = temp_dir / "output"

    try:
        # Read input file
        try:
            input_data = input_path.read_bytes()
        except PermissionError as e:
            raise IOError(
                f"Cannot read {input_path}: permission denied"
            ) from e

        # Process data
        try:
            output_data = processor(input_data)
        except Exception as e:
            raise ProcessingError(
                f"Failed to process {input_path}: {e}"
            ) from e

        # Write to temporary file
        try:
            temp_output.write_bytes(output_data)
        except IOError as e:
            raise IOError(
                f"Failed to write temporary file: {e}"
            ) from e

        # Atomic move to final location
        try:
            shutil.move(str(temp_output), str(output_path))
        except Exception as e:
            raise IOError(
                f"Failed to move to {output_path}: {e}"
            ) from e

    finally:
        # Always clean up temporary directory
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
```

### Example 3: Validation with Multiple Error Collection
```typescript
interface ValidationResult {
  valid: boolean;
  errors: string[];
}

function validateRegistration(data: unknown): ValidationResult {
  const errors: string[] = [];

  if (typeof data !== 'object' || data === null) {
    return {
      valid: false,
      errors: ['Expected object, got ' + typeof data],
    };
  }

  const { email, password, username } = data as Record<string, unknown>;

  // Collect all validation errors instead of failing fast
  if (typeof email !== 'string' || !email.includes('@')) {
    errors.push('Invalid email format');
  }

  if (typeof password !== 'string' || password.length < 8) {
    errors.push('Password must be at least 8 characters');
  }

  if (typeof username !== 'string' || username.length < 3) {
    errors.push('Username must be at least 3 characters');
  }

  if (typeof username === 'string' && !/^[a-zA-Z0-9_]+$/.test(username)) {
    errors.push('Username can only contain letters, numbers, and underscores');
  }

  return {
    valid: errors.length === 0,
    errors,
  };
}

// Usage
const result = validateRegistration(userData);
if (!result.valid) {
  console.error('Validation failed:');
  result.errors.forEach(error => console.error('  -', error));
  throw new Error('Invalid registration data');
}
```

## Error Logging Best Practices

1. **Include structured context**
```python
logger.error(
    "Failed to process user data",
    extra={
        "user_id": user_id,
        "operation": "update_profile",
        "error_type": type(e).__name__,
    }
)
```

2. **Use appropriate log levels**
- ERROR: Operation failed, needs attention
- WARNING: Degraded operation, might be a problem
- INFO: Normal operation events
- DEBUG: Detailed diagnostic information

3. **Don't log and re-raise without context**
```python
# Bad
try:
    operation()
except Exception as e:
    logger.error(str(e))  # Logged
    raise  # Same error re-raised (will be logged again)

# Good
try:
    operation()
except Exception as e:
    logger.error(f"Operation failed: {e}", exc_info=True)
    raise OperationError("Failed to complete operation") from e
```

4. **Include stack traces for debugging**
```python
logger.error("Critical error occurred", exc_info=True)
```
