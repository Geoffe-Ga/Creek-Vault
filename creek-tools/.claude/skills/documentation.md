# Documentation Skill

## Purpose
Create clear, comprehensive documentation that serves as a reliable reference and aids understanding for both humans and AI agents.

## Principles
1. Write for your future self (you will forget)
2. Document the "why", not just the "what"
3. Keep documentation close to code
4. Use examples liberally
5. Update docs when changing code
6. Make documentation searchable and navigable

## Patterns by Language

### Python
**Google-style docstrings**
```python
from typing import Optional
from pathlib import Path

def process_file(
    input_path: Path,
    output_path: Path,
    *,
    encoding: str = "utf-8",
    validate: bool = True,
) -> dict[str, int]:
    """Process input file and write results to output file.

    Reads the input file, performs validation if enabled, processes
    the content, and writes formatted results to the output file.
    Processing includes tokenization, normalization, and statistical
    analysis.

    Args:
        input_path: Path to input file. Must exist and be readable.
        output_path: Path to output file. Parent directory must exist.
        encoding: Character encoding for file I/O. Defaults to UTF-8.
        validate: Whether to validate input before processing.
            When True, raises ValueError for invalid input.
            When False, attempts best-effort processing.

    Returns:
        Dictionary containing processing statistics:
            - 'lines_processed': Number of lines processed
            - 'tokens_found': Number of tokens extracted
            - 'errors_encountered': Number of validation errors

    Raises:
        FileNotFoundError: If input_path does not exist.
        PermissionError: If input_path is not readable or output_path
            is not writable.
        ValueError: If validate=True and input content is invalid.
        IOError: If file I/O operations fail.

    Examples:
        Basic usage:
        >>> result = process_file(
        ...     Path("input.txt"),
        ...     Path("output.txt")
        ... )
        >>> print(f"Processed {result['lines_processed']} lines")

        Skip validation for best-effort processing:
        >>> result = process_file(
        ...     Path("messy_input.txt"),
        ...     Path("output.txt"),
        ...     validate=False
        ... )

    Note:
        This function uses atomic writes. The output file is written
        to a temporary location and renamed on success, ensuring the
        output file is never in a partial state.

        Processing large files (>100MB) may require significant memory.
        Consider using process_file_streaming() for large files.

    See Also:
        process_file_streaming: Memory-efficient alternative for large files
        validate_file_format: Standalone validation function
    """
    if not input_path.exists():
        raise FileNotFoundError(
            f"Input file not found: {input_path.absolute()}"
        )

    # Implementation...
    return {"lines_processed": 0, "tokens_found": 0, "errors_encountered": 0}


class ConfigurationManager:
    """Manage application configuration with validation and reloading.

    ConfigurationManager handles loading, validating, and hot-reloading
    of application configuration from JSON or YAML files. It supports
    environment-specific overrides and provides type-safe access to
    configuration values.

    Attributes:
        config_path: Path to primary configuration file.
        current_config: Currently loaded configuration dictionary.
        last_reload: Timestamp of last successful reload.

    Examples:
        Basic usage:
        >>> manager = ConfigurationManager("config.json")
        >>> api_key = manager.get("api_key")
        >>> timeout = manager.get("timeout", default=30)

        Hot reload on file change:
        >>> manager = ConfigurationManager("config.json", auto_reload=True)
        >>> # Configuration automatically reloads when file changes

        Environment-specific configuration:
        >>> manager = ConfigurationManager(
        ...     "config.base.json",
        ...     overrides="config.production.json"
        ... )

    Note:
        Thread-safe for concurrent reads. Write operations (reload)
        acquire an exclusive lock.
    """

    def __init__(
        self,
        config_path: str | Path,
        *,
        auto_reload: bool = False,
        overrides: Optional[str | Path] = None,
    ) -> None:
        """Initialize configuration manager.

        Args:
            config_path: Path to configuration file (JSON or YAML).
            auto_reload: Enable automatic reload on file change.
                Uses file system watcher. Defaults to False.
            overrides: Optional path to override configuration.
                Values in override file take precedence.

        Raises:
            FileNotFoundError: If config_path doesn't exist.
            ValueError: If configuration file is invalid.
        """
        # Implementation...
        pass
```

**Module-level documentation**
```python
"""Configuration management for Start Green Stay Green.

This module provides configuration loading, validation, and management
utilities for the SGSG application. It supports multiple configuration
formats (JSON, YAML, TOML) and environment-specific overrides.

Key Components:
    - ConfigurationManager: Primary configuration interface
    - ConfigValidator: Schema validation for configurations
    - EnvironmentConfig: Environment-specific configuration handling

Usage:
    Basic configuration loading:
    >>> from start_green_stay_green.config import ConfigurationManager
    >>> manager = ConfigurationManager("config.json")
    >>> api_key = manager.get("api_key")

    Schema validation:
    >>> from start_green_stay_green.config import ConfigValidator
    >>> validator = ConfigValidator(schema_path="schema.json")
    >>> validator.validate(config_data)

Environment Variables:
    SGSG_CONFIG_PATH: Override default configuration file path
    SGSG_ENV: Environment name (dev, staging, production)
    SGSG_LOG_LEVEL: Logging level (DEBUG, INFO, WARNING, ERROR)

See Also:
    - Configuration schema: docs/config-schema.md
    - Environment setup: docs/environment-setup.md

Examples:
    See examples/config/ directory for complete examples:
    - basic_config.py: Simple configuration loading
    - advanced_config.py: Environment overrides and validation
    - hot_reload.py: Automatic configuration reloading
"""

from pathlib import Path
from typing import Optional

__version__ = "1.0.0"
__all__ = [
    "ConfigurationManager",
    "ConfigValidator",
    "EnvironmentConfig",
]
```

### TypeScript
**TSDoc comments**
```typescript
/**
 * Process user input and validate against schema.
 *
 * Performs comprehensive validation of user input including type checking,
 * format validation, and business rule enforcement. Returns validated data
 * or throws detailed validation errors.
 *
 * @param input - Raw user input data (may be any type)
 * @param schema - JSON schema for validation
 * @param options - Optional validation settings
 * @returns Validated and typed user data
 *
 * @throws {ValidationError} When input fails validation
 * @throws {SchemaError} When schema itself is invalid
 *
 * @example
 * Basic validation:
 * ```typescript
 * const userData = processInput(
 *   { email: 'user@example.com', age: 25 },
 *   userSchema
 * );
 * ```
 *
 * @example
 * With custom options:
 * ```typescript
 * const userData = processInput(
 *   rawInput,
 *   schema,
 *   { strict: true, coerceTypes: false }
 * );
 * ```
 *
 * @see {@link ValidationOptions} for available options
 * @see {@link UserSchema} for user schema definition
 *
 * @since 1.0.0
 * @public
 */
export function processInput(
  input: unknown,
  schema: JSONSchema,
  options?: ValidationOptions
): UserData {
  // Implementation...
}

/**
 * Configuration manager for application settings.
 *
 * Handles loading, validation, and hot-reloading of application
 * configuration from JSON or YAML files. Supports environment-specific
 * overrides and type-safe configuration access.
 *
 * @remarks
 * This class is thread-safe for concurrent reads. Reload operations
 * acquire an exclusive lock to prevent race conditions.
 *
 * The configuration file is watched for changes when `autoReload` is
 * enabled. Changes trigger automatic reload with validation.
 *
 * @example
 * Basic usage:
 * ```typescript
 * const manager = new ConfigManager('config.json');
 * const apiKey = manager.get('apiKey');
 * const timeout = manager.get('timeout', 30); // with default
 * ```
 *
 * @example
 * With auto-reload:
 * ```typescript
 * const manager = new ConfigManager('config.json', {
 *   autoReload: true,
 *   onReload: (config) => console.log('Config reloaded', config)
 * });
 * ```
 *
 * @public
 */
export class ConfigManager {
  /**
   * Path to primary configuration file.
   * @readonly
   */
  public readonly configPath: string;

  /**
   * Currently loaded configuration object.
   * @remarks
   * Accessing this property creates a shallow copy to prevent
   * external modifications.
   */
  public get currentConfig(): Readonly<Config> {
    return { ...this._config };
  }

  /**
   * Create new configuration manager.
   *
   * @param configPath - Path to configuration file
   * @param options - Optional configuration settings
   *
   * @throws {FileNotFoundError} If config file doesn't exist
   * @throws {ValidationError} If config file is invalid
   */
  constructor(
    configPath: string,
    options?: ConfigManagerOptions
  ) {
    // Implementation...
  }

  /**
   * Reload configuration from file.
   *
   * Reads configuration file, validates content, and updates
   * current configuration. Triggers `onReload` callback if configured.
   *
   * @returns Promise resolving to new configuration
   *
   * @throws {FileNotFoundError} If config file is missing
   * @throws {ValidationError} If new config is invalid
   *
   * @remarks
   * This method is async to support file watchers and validation.
   * It acquires an exclusive lock during reload to prevent
   * concurrent modification.
   */
  public async reload(): Promise<Config> {
    // Implementation...
  }
}
```

**README sections**
```markdown
# Component Name

## Overview
Brief description of what this component does and why it exists.

## Installation
```bash
npm install @sgsg/component-name
```

## Quick Start
```typescript
import { Component } from '@sgsg/component-name';

const component = new Component({
  apiKey: 'your-api-key',
  timeout: 30
});

const result = await component.process(data);
console.log(result);
```

## API Reference

### `Component`
Main component class for processing data.

#### Constructor
```typescript
new Component(options: ComponentOptions)
```

**Parameters:**
- `options.apiKey` (string, required): API authentication key
- `options.timeout` (number, optional): Request timeout in seconds. Default: 30

**Throws:**
- `ValidationError`: If apiKey is missing or invalid

#### Methods

##### `process(data: InputData): Promise<Result>`
Process input data and return results.

**Parameters:**
- `data`: Input data to process

**Returns:**
Promise resolving to processing results

**Example:**
```typescript
const result = await component.process({ text: 'Hello, world!' });
```

## Configuration

Configuration can be provided via:
1. Constructor options
2. Environment variables
3. Configuration file

### Environment Variables
- `COMPONENT_API_KEY`: API authentication key
- `COMPONENT_TIMEOUT`: Request timeout in seconds

### Configuration File
```json
{
  "apiKey": "your-api-key",
  "timeout": 30,
  "retries": 3
}
```

## Error Handling

The component throws typed errors for different failure scenarios:

- `ValidationError`: Invalid input or configuration
- `TimeoutError`: Request exceeded timeout
- `APIError`: API request failed

```typescript
try {
  await component.process(data);
} catch (error) {
  if (error instanceof ValidationError) {
    console.error('Invalid data:', error.message);
  } else if (error instanceof TimeoutError) {
    console.error('Request timed out');
  }
}
```

## Examples

See `examples/` directory for complete examples:
- `basic-usage.ts`: Simple processing
- `advanced-config.ts`: Advanced configuration
- `error-handling.ts`: Comprehensive error handling

## Development

```bash
# Install dependencies
npm install

# Run tests
npm test

# Build
npm run build

# Lint
npm run lint
```

## License
MIT
```

### Go
**Package documentation**
```go
// Package config provides configuration management for SGSG applications.
//
// This package supports loading configuration from JSON, YAML, and TOML files,
// with environment-specific overrides and validation. It provides type-safe
// access to configuration values and supports hot-reloading.
//
// Basic Usage
//
// Load and access configuration:
//
//	manager, err := config.NewManager("config.json")
//	if err != nil {
//	    log.Fatal(err)
//	}
//	apiKey := manager.Get("api_key")
//	timeout := manager.GetInt("timeout", 30) // with default
//
// Environment Overrides
//
// Configuration values can be overridden using environment variables.
// Environment variables use SGSG_ prefix:
//
//	export SGSG_API_KEY="override-key"
//	export SGSG_TIMEOUT="60"
//
// Hot Reloading
//
// Enable automatic reload when configuration file changes:
//
//	manager, err := config.NewManager("config.json",
//	    config.WithAutoReload(true),
//	    config.WithReloadCallback(func(cfg *Config) {
//	        log.Println("Configuration reloaded")
//	    }),
//	)
//
// Validation
//
// Validate configuration against JSON schema:
//
//	validator := config.NewValidator("schema.json")
//	if err := validator.Validate(configData); err != nil {
//	    log.Fatal("Invalid configuration:", err)
//	}
//
// See examples/ directory for complete usage examples.
package config

// LoadConfig loads configuration from the specified file path.
//
// Supports JSON, YAML, and TOML formats based on file extension.
// Automatically applies environment variable overrides.
//
// Parameters:
//   - path: Path to configuration file. Must exist and be readable.
//
// Returns:
//   - *Config: Loaded configuration object
//   - error: FileNotFoundError if file doesn't exist,
//     ValidationError if content is invalid,
//     or other error for I/O failures
//
// Example:
//
//	config, err := LoadConfig("config.json")
//	if err != nil {
//	    log.Fatal(err)
//	}
//	fmt.Println("API Key:", config.APIKey)
//
// Environment Variables:
//
// Configuration values can be overridden using environment variables
// with SGSG_ prefix:
//   - SGSG_API_KEY: Override api_key field
//   - SGSG_TIMEOUT: Override timeout field
//
// See also: Manager.Reload() for reloading configuration
func LoadConfig(path string) (*Config, error) {
    // Implementation...
    return nil, nil
}

// Manager manages application configuration with validation and hot-reloading.
//
// Manager handles loading, validating, and reloading configuration from
// files. It supports multiple formats and environment-specific overrides.
//
// Fields:
//   - ConfigPath: Path to primary configuration file
//   - CurrentConfig: Currently loaded configuration (read-only)
//
// Thread Safety:
//
// Manager is safe for concurrent reads. Reload operations acquire an
// exclusive lock to prevent race conditions.
//
// Example:
//
//	manager, err := NewManager("config.json")
//	if err != nil {
//	    log.Fatal(err)
//	}
//	apiKey := manager.Get("api_key")
type Manager struct {
    ConfigPath    string
    CurrentConfig *Config
    // private fields...
}
```

**Function documentation**
```go
// ProcessFile processes the input file and writes results to output file.
//
// Reads the input file, performs validation if enabled, processes the content,
// and writes formatted results to the output file. Processing includes
// tokenization, normalization, and statistical analysis.
//
// Parameters:
//   - inputPath: Path to input file. Must exist and be readable.
//   - outputPath: Path to output file. Parent directory must exist.
//   - encoding: Character encoding for file I/O. Use "utf-8" for UTF-8.
//   - validate: Whether to validate input before processing.
//     When true, returns error for invalid input.
//     When false, attempts best-effort processing.
//
// Returns:
//   - map[string]int: Processing statistics including:
//     - "lines_processed": Number of lines processed
//     - "tokens_found": Number of tokens extracted
//     - "errors_encountered": Number of validation errors
//   - error: FileNotFoundError if input doesn't exist,
//     PermissionError for access issues,
//     ValidationError if validate=true and input is invalid,
//     or other error for processing failures
//
// Example:
//
//	stats, err := ProcessFile("input.txt", "output.txt", "utf-8", true)
//	if err != nil {
//	    log.Fatal(err)
//	}
//	fmt.Printf("Processed %d lines\n", stats["lines_processed"])
//
// Notes:
//
// This function uses atomic writes. The output file is written to a
// temporary location and renamed on success, ensuring the output file
// is never in a partial state.
//
// Processing large files (>100MB) may require significant memory.
// Consider using ProcessFileStreaming for large files.
func ProcessFile(
    inputPath string,
    outputPath string,
    encoding string,
    validate bool,
) (map[string]int, error) {
    // Implementation...
    return nil, nil
}
```

### Rust
**Doc comments with examples**
```rust
/// Process input file and write results to output file.
///
/// Reads the input file, performs validation if enabled, processes the content,
/// and writes formatted results to the output file. Processing includes
/// tokenization, normalization, and statistical analysis.
///
/// # Arguments
///
/// * `input_path` - Path to input file. Must exist and be readable.
/// * `output_path` - Path to output file. Parent directory must exist.
/// * `encoding` - Character encoding for file I/O (e.g., "utf-8").
/// * `validate` - Whether to validate input before processing.
///   When `true`, returns error for invalid input.
///   When `false`, attempts best-effort processing.
///
/// # Returns
///
/// Returns `HashMap` containing processing statistics:
/// - `lines_processed`: Number of lines processed
/// - `tokens_found`: Number of tokens extracted
/// - `errors_encountered`: Number of validation errors
///
/// # Errors
///
/// Returns error if:
/// - Input file doesn't exist (`io::ErrorKind::NotFound`)
/// - Permission denied for reading/writing (`io::ErrorKind::PermissionDenied`)
/// - Input validation fails (when `validate=true`)
/// - File I/O operations fail
///
/// # Examples
///
/// Basic usage:
/// ```
/// use std::path::Path;
/// use my_crate::process_file;
///
/// let stats = process_file(
///     Path::new("input.txt"),
///     Path::new("output.txt"),
///     "utf-8",
///     true
/// ).expect("Failed to process file");
///
/// println!("Processed {} lines", stats.get("lines_processed").unwrap());
/// ```
///
/// Skip validation for best-effort processing:
/// ```
/// let stats = process_file(
///     Path::new("messy_input.txt"),
///     Path::new("output.txt"),
///     "utf-8",
///     false
/// )?;
/// ```
///
/// # Notes
///
/// This function uses atomic writes. The output file is written to a
/// temporary location and renamed on success, ensuring the output file
/// is never in a partial state.
///
/// Processing large files (>100MB) may require significant memory.
/// Consider using `process_file_streaming` for large files.
///
/// # See Also
///
/// * [`process_file_streaming`] - Memory-efficient alternative for large files
/// * [`validate_file_format`] - Standalone validation function
pub fn process_file(
    input_path: &Path,
    output_path: &Path,
    encoding: &str,
    validate: bool,
) -> Result<HashMap<String, usize>, Error> {
    // Implementation...
    Ok(HashMap::new())
}

/// Configuration manager for application settings.
///
/// Handles loading, validation, and hot-reloading of application
/// configuration from JSON or YAML files. Supports environment-specific
/// overrides and type-safe configuration access.
///
/// # Thread Safety
///
/// `ConfigManager` is safe for concurrent reads using `Arc` and `RwLock`.
/// Reload operations acquire an exclusive write lock.
///
/// # Examples
///
/// Basic usage:
/// ```
/// use my_crate::ConfigManager;
///
/// let manager = ConfigManager::new("config.json")?;
/// let api_key = manager.get("api_key")?;
/// let timeout = manager.get_or("timeout", 30);
/// ```
///
/// With auto-reload:
/// ```
/// let manager = ConfigManager::builder()
///     .config_path("config.json")
///     .auto_reload(true)
///     .on_reload(|config| {
///         println!("Config reloaded: {:?}", config);
///     })
///     .build()?;
/// ```
pub struct ConfigManager {
    /// Path to primary configuration file
    pub config_path: PathBuf,

    /// Currently loaded configuration (read-only access via getter)
    current_config: Arc<RwLock<Config>>,
}
```

## Anti-patterns

### Under-documenting Public APIs
**Bad:**
```python
def process(data):
    """Process data."""
    return transform(data)
```

**Good:**
```python
def process(data: dict[str, Any]) -> ProcessedData:
    """Process raw input data and return structured output.

    Applies transformation pipeline including validation,
    normalization, and enrichment.

    Args:
        data: Raw input data dictionary containing at least
            'type' and 'content' keys.

    Returns:
        ProcessedData object with normalized fields.

    Raises:
        ValueError: If data is missing required fields.
        ProcessingError: If transformation pipeline fails.

    Examples:
        >>> result = process({"type": "text", "content": "Hello"})
        >>> print(result.normalized_content)
        'hello'
    """
    return transform(data)
```

### Documenting Implementation Instead of Interface
**Bad:**
```python
def fetch_user(user_id: str) -> User:
    """
    First we check the cache using a dict lookup.
    If not found, we make an HTTP GET request to /api/users/{id}.
    Then we parse the JSON response using json.loads().
    Finally we create a User object and cache it.
    """
```

**Good:**
```python
def fetch_user(user_id: str) -> User:
    """Retrieve user by ID from API.

    Fetches user data from the API, using cache when available
    to reduce API calls.

    Args:
        user_id: Unique user identifier

    Returns:
        User object with profile data

    Raises:
        UserNotFoundError: If user doesn't exist
        APIError: If API request fails

    Note:
        Results are cached for 5 minutes to reduce API load.
    """
```

### Stale Documentation
**Bad:**
```python
def process_data(
    data: str,
    validate: bool = True,
    transform: bool = True,  # New parameter added
) -> dict:
    """Process input data.

    Args:
        data: Input string
        validate: Whether to validate input
        # Forgot to document new parameter!

    Returns:
        Processed data dictionary
    """
```

**Good:**
```python
def process_data(
    data: str,
    validate: bool = True,
    transform: bool = True,
) -> dict:
    """Process input data with validation and transformation.

    Args:
        data: Input string to process
        validate: Whether to validate input before processing
        transform: Whether to apply transformation pipeline.
            When False, returns data with validation only.
            Added in version 2.0.

    Returns:
        Processed data dictionary with 'status' and 'result' keys
    """
```

### Missing Examples
**Bad:**
```python
def calculate_statistics(
    values: list[float],
    include_median: bool = False,
    include_mode: bool = False,
) -> Statistics:
    """Calculate statistical measures for values.

    Args:
        values: List of numerical values
        include_median: Calculate median
        include_mode: Calculate mode

    Returns:
        Statistics object
    """
```

**Good:**
```python
def calculate_statistics(
    values: list[float],
    include_median: bool = False,
    include_mode: bool = False,
) -> Statistics:
    """Calculate statistical measures for values.

    Args:
        values: List of numerical values
        include_median: Calculate median (slower for large datasets)
        include_mode: Calculate mode (requires hashable values)

    Returns:
        Statistics object with mean, std_dev, and optional median/mode

    Examples:
        Basic usage:
        >>> stats = calculate_statistics([1, 2, 3, 4, 5])
        >>> print(stats.mean)
        3.0

        Include all measures:
        >>> stats = calculate_statistics(
        ...     [1, 2, 2, 3, 4],
        ...     include_median=True,
        ...     include_mode=True
        ... )
        >>> print(f"Median: {stats.median}, Mode: {stats.mode}")
        Median: 2, Mode: 2
    """
```

## Examples

### Example 1: Comprehensive Module Documentation
```python
"""User management module for SGSG application.

This module provides user authentication, authorization, and profile
management functionality. It integrates with external identity providers
and maintains user sessions.

Key Components:
    UserManager: Primary interface for user operations
    AuthProvider: Abstract base for authentication providers
    OAuthProvider: OAuth 2.0 authentication implementation
    SessionManager: User session lifecycle management

Architecture:
    The module follows a plugin architecture for authentication providers.
    Custom providers can be registered by subclassing AuthProvider and
    implementing the required methods.

    User sessions are stored in Redis with configurable TTL. Session
    data is encrypted using AES-256.

Usage:
    Basic user authentication:
    >>> from sgsg.users import UserManager
    >>> manager = UserManager()
    >>> user = manager.authenticate(email="user@example.com", password="...")
    >>> session = manager.create_session(user)

    OAuth authentication:
    >>> from sgsg.users import OAuthProvider
    >>> provider = OAuthProvider(
    ...     client_id="...",
    ...     client_secret="...",
    ...     redirect_uri="https://app.com/callback"
    ... )
    >>> manager = UserManager(auth_provider=provider)
    >>> url = manager.get_oauth_url()  # Redirect user to this URL

Configuration:
    Environment variables:
        SGSG_SESSION_TTL: Session timeout in seconds (default: 3600)
        SGSG_REDIS_URL: Redis connection URL for session storage
        SGSG_JWT_SECRET: Secret key for JWT token signing

    Configuration file (config.json):
    ```json
    {
        "users": {
            "session_ttl": 3600,
            "require_email_verification": true,
            "password_min_length": 12
        }
    }
    ```

Security Considerations:
    - All passwords are hashed using Argon2id
    - Sessions use cryptographically secure random IDs
    - Session data is encrypted at rest
    - JWT tokens are signed with HS256
    - Rate limiting applied to authentication endpoints

Examples:
    See examples/users/ directory:
    - basic_auth.py: Username/password authentication
    - oauth_flow.py: Complete OAuth 2.0 flow
    - session_management.py: Session lifecycle
    - custom_provider.py: Implementing custom auth provider

API Reference:
    For detailed API documentation, see:
    - docs/api/users.md
    - Online docs: https://sgsg.readthedocs.io/users

Migration Guide:
    Upgrading from v1.x: See docs/migration/v2.md

See Also:
    - sgsg.auth: Low-level authentication primitives
    - sgsg.crypto: Cryptographic utilities
    - sgsg.sessions: Session storage backends

Version History:
    - 2.0.0: Added OAuth provider support
    - 1.5.0: Added email verification
    - 1.0.0: Initial release
"""

from pathlib import Path
from typing import Optional, Protocol

__version__ = "2.0.0"
__all__ = [
    "UserManager",
    "AuthProvider",
    "OAuthProvider",
    "SessionManager",
    "User",
    "Session",
]
```

### Example 2: Architecture Decision Record
```markdown
# ADR 001: Use OS Keyring for API Key Storage

## Status
Accepted

## Context
The application requires secure storage of API keys for external services.
API keys must persist across application restarts but should not be stored
in plain text.

We considered several approaches:
1. Environment variables in `.env` files
2. Encrypted configuration files
3. OS keyring integration
4. External secret management service (e.g., HashiCorp Vault)

### Requirements
- Secure storage (no plain text)
- Persistent across restarts
- Works on macOS, Linux, Windows
- No additional infrastructure required
- Simple developer experience

## Decision
We will use OS keyring via the `keyring` Python library for API key storage.

API keys will be stored in:
- **macOS**: Keychain
- **Linux**: Secret Service (GNOME Keyring, KWallet)
- **Windows**: Windows Credential Locker

## Consequences

### Positive
- Keys never touch disk in plain text
- OS-level encryption and access control
- No additional infrastructure required
- Cross-platform support
- Integrates with existing OS security features

### Negative
- Requires one-time manual setup on each machine
- Headless/CI environments need alternative approach
- Different behavior per OS (keyring differences)

### Mitigations
- Provide setup script for initial key storage
- Support environment variables as fallback for CI
- Document setup process for each OS
- Include keyring troubleshooting guide

## Implementation
```python
import keyring

# Store API key (one-time setup)
keyring.set_password("sgsg", "claude_api_key", api_key)

# Retrieve API key
api_key = keyring.get_password("sgsg", "claude_api_key")
if not api_key:
    raise ValueError("API key not found in keyring")
```

## Alternatives Considered

### Environment Variables
**Pros**: Simple, universal support
**Cons**: Easily committed to git, visible in process list, not encrypted

### Encrypted Config Files
**Pros**: Portable, self-contained
**Cons**: Key management problem (where to store encryption key?), complex

### External Secret Service
**Pros**: Enterprise-grade, centralized management
**Cons**: Infrastructure overhead, complexity, cost

## References
- `keyring` library: https://pypi.org/project/keyring/
- Security best practices: docs/security.md
- Setup guide: docs/setup/api-keys.md

## Revision History
- 2026-01-10: Initial decision
- 2026-01-12: Added CI fallback strategy
```

### Example 3: Inline Code Documentation
```python
from typing import Optional, Callable
from pathlib import Path
import json

class ConfigurationManager:
    """Manage application configuration."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self._config: dict = {}
        self._callbacks: list[Callable[[dict], None]] = []

        # Load initial configuration
        # This happens in __init__ to fail fast if config is invalid
        self._load()

    def _load(self) -> None:
        """Load configuration from file.

        Private method - use reload() for public API.
        Separated to allow reloading without recreating object.
        """
        # Validate file exists before attempting to read
        # Explicit check provides better error messages
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {self.config_path}"
            )

        # Read and parse JSON
        # Using read_text() instead of open() for cleaner code
        content = self.config_path.read_text()

        try:
            self._config = json.loads(content)
        except json.JSONDecodeError as e:
            # Wrap JSON error with file context for debugging
            raise ValueError(
                f"Invalid JSON in {self.config_path}: {e.msg} "
                f"at line {e.lineno}, column {e.colno}"
            ) from e

        # Validate required fields
        # Fail fast if configuration is incomplete
        self._validate_config()

    def _validate_config(self) -> None:
        """Validate configuration has required fields.

        Raises:
            ValueError: If required fields are missing
        """
        required_fields = {"api_key", "base_url", "timeout"}
        missing = required_fields - self._config.keys()

        if missing:
            raise ValueError(
                f"Missing required configuration fields: {missing}"
            )

        # Type validation
        # Ensure timeout is numeric to prevent runtime errors
        if not isinstance(self._config.get("timeout"), (int, float)):
            raise ValueError(
                f"Invalid timeout value: {self._config.get('timeout')} "
                f"(must be number)"
            )

    def reload(self) -> None:
        """Reload configuration from file and notify callbacks.

        This is the public API for reloading. It triggers callbacks
        after successful reload, allowing dependent components to
        update their state.
        """
        # Store old config for rollback on error
        old_config = self._config.copy()

        try:
            self._load()
        except Exception:
            # Rollback to old config if reload fails
            # This prevents partial/invalid config from being used
            self._config = old_config
            raise

        # Notify all registered callbacks
        # Callbacks are called after successful reload
        # If callback fails, we log but don't fail the reload
        for callback in self._callbacks:
            try:
                callback(self._config)
            except Exception as e:
                # Log callback errors but don't fail reload
                # One bad callback shouldn't break configuration
                logger.error(
                    f"Configuration reload callback failed: {e}",
                    exc_info=True
                )

    def on_reload(self, callback: Callable[[dict], None]) -> None:
        """Register callback to be called when configuration reloads.

        Args:
            callback: Function accepting config dict, called after reload
        """
        self._callbacks.append(callback)

    def get(self, key: str, default: Optional[str] = None) -> str:
        """Get configuration value by key.

        Args:
            key: Configuration key (supports dot notation for nested)
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        # Support dot notation for nested keys
        # Example: "database.connection.timeout" -> ["database", "connection", "timeout"]
        keys = key.split(".")

        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                # Nested access failed (parent is not dict)
                return default

            if value is None:
                return default

        return value
```

## Documentation Checklist

Before considering documentation complete:

- [ ] All public functions have docstrings
- [ ] All public classes have class docstrings
- [ ] Module has module-level docstring
- [ ] All parameters documented
- [ ] Return values documented
- [ ] All exceptions documented
- [ ] At least one example provided
- [ ] Complex logic has inline comments
- [ ] README exists and is current
- [ ] API changes have migration notes
- [ ] Configuration options documented
- [ ] Environment variables listed
- [ ] Error messages are clear and actionable
