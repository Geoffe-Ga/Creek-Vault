# Error Handling Patterns by Language

## Python

### Typed Exceptions with Context
```python
class ConfigurationError(Exception):
    def __init__(self, field: str, value: str, reason: str) -> None:
        self.field = field
        self.value = value
        self.reason = reason
        super().__init__(f"Invalid {field}={value!r}: {reason}")

def load_config(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path.absolute()}")
    content = path.read_text()
    if not content.strip():
        raise ValueError(f"Config file is empty: {path.absolute()}")
    config = parse_config(content)
    required = {"api_key", "base_url"}
    missing = required - config.keys()
    if missing:
        raise ConfigurationError("required_fields", str(missing), "Missing required fields")
    return config
```

### Context Managers for Resource Cleanup
```python
@contextmanager
def atomic_write(path: Path) -> Iterator[Path]:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        yield temp_path
        temp_path.replace(path)
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        raise IOError(f"Failed to write {path}: {e}") from e
```

### Specific Exception Catching
```python
try:
    content = path.read_text()
except FileNotFoundError:
    raise FileNotFoundError(f"JSON file not found: {path.absolute()}")
except PermissionError as e:
    raise PermissionError(f"Cannot read {path.absolute()}: {e}") from e

try:
    data = json.loads(content)
except json.JSONDecodeError as e:
    raise ValueError(f"Invalid JSON in {path}: {e.msg} at line {e.lineno}") from e
```

## TypeScript

### Custom Error Classes
```typescript
class ValidationError extends Error {
  constructor(
    public readonly field: string,
    public readonly value: unknown,
    message: string
  ) {
    super(`Validation failed for ${field}: ${message}`);
    this.name = 'ValidationError';
    if (Error.captureStackTrace) {
      Error.captureStackTrace(this, ValidationError);
    }
  }
}
```

### Result Type Pattern
```typescript
type Result<T, E = Error> =
  | { ok: true; value: T }
  | { ok: false; error: E };

function parseConfig(content: string): Result<Config> {
  try {
    const data = JSON.parse(content);
    if (!isValidConfig(data)) {
      return { ok: false, error: new Error('Invalid configuration') };
    }
    return { ok: true, value: data };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error : new Error('Unknown error') };
  }
}
```

### Promise Error Handling
```typescript
async function fetchUserData(userId: string): Promise<UserData> {
  try {
    const response = await fetch(`/api/users/${userId}`);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
    return validateUserData(await response.json());
  } catch (error) {
    if (error instanceof ValidationError) throw error;
    throw new Error(`Failed to fetch user ${userId}: ${error instanceof Error ? error.message : 'Unknown'}`);
  }
}
```

## Go

### Error Wrapping with Context
```go
func LoadConfig(path string) (*Config, error) {
    file, err := os.Open(path)
    if err != nil {
        return nil, fmt.Errorf("failed to open config %q: %w", path, err)
    }
    defer file.Close()

    config, err := parseConfig(file)
    if err != nil {
        return nil, fmt.Errorf("failed to parse config from %q: %w", path, err)
    }
    return config, nil
}
```

### Custom Error Types
```go
type ValidationError struct {
    Field  string
    Value  interface{}
    Reason string
}

func (e *ValidationError) Error() string {
    return fmt.Sprintf("validation failed for %s=%v: %s", e.Field, e.Value, e.Reason)
}
```

## Rust

### Custom Error Enums
```rust
#[derive(Debug)]
pub enum ConfigError {
    NotFound(String),
    ParseError(String),
    ValidationError { field: String, reason: String },
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            ConfigError::NotFound(path) => write!(f, "Config not found: {}", path),
            ConfigError::ParseError(msg) => write!(f, "Parse failed: {}", msg),
            ConfigError::ValidationError { field, reason } => write!(f, "Validation failed for {}: {}", field, reason),
        }
    }
}

impl std::error::Error for ConfigError {}
```

### The ? Operator with Context
```rust
fn load_config(path: &str) -> Result<Config, String> {
    let contents = read_file_contents(path)
        .map_err(|e| format!("Failed to read config from {}: {}", path, e))?;
    parse_config(&contents)
        .map_err(|e| format!("Failed to parse config from {}: {}", path, e))
}
```
