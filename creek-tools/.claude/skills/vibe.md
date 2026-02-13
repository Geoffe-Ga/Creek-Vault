# Vibe Skill

## Purpose
Establish consistent code style, naming conventions, and structural patterns across the codebase.

## Principles
1. Consistency over cleverness
2. Readability over brevity
3. Explicitness over implicitness
4. Standard patterns over custom solutions
5. Team conventions over personal preference

## Language-Specific Guidelines

### Python
- Follow PEP 8 and PEP 257
- Use type hints consistently
- Prefer dataclasses/Pydantic for data structures
- Use pathlib over os.path
- async/await for I/O-bound operations

### TypeScript
- Follow TypeScript best practices
- Use strict mode
- Prefer interfaces over types for objects
- Use const assertions where appropriate
- Avoid `any`, prefer `unknown`

### Go
- Follow effective Go guidelines
- Use gofmt/goimports
- Prefer composition over inheritance
- Use context.Context for cancellation
- Keep interfaces small

### Rust
- Follow Rust API guidelines
- Use clippy lints
- Prefer owned types at API boundaries
- Use Result<T, E> for errors
- Leverage type system for correctness

## Naming Conventions

### Functions
- Python: `snake_case`
- TypeScript/JavaScript: `camelCase`
- Go: `CamelCase` (exported), `camelCase` (internal)
- Rust: `snake_case`

### Classes/Types
- Python: `PascalCase`
- TypeScript: `PascalCase`
- Go: `PascalCase`
- Rust: `PascalCase`

### Constants
- Python: `SCREAMING_SNAKE_CASE`
- TypeScript: `SCREAMING_SNAKE_CASE` or `camelCase` for const values
- Go: `CamelCase` (exported), `camelCase` (internal)
- Rust: `SCREAMING_SNAKE_CASE`

## Anti-patterns
- Clever code over clear code
- Abbreviations in names (unless universally understood)
- Deep nesting (> 3 levels)
- Long functions (> 50 lines)
- Mixed abstraction levels
- Magic numbers without constants
- Global state
- God objects

## Examples

### Good Python
```python
from pathlib import Path
from typing import Protocol

class FileReader(Protocol):
    """Protocol for reading file contents."""

    def read_text(self) -> str:
        """Read file as text."""
        ...

def process_config_file(config_path: Path) -> dict[str, str]:
    """Process configuration file and return parsed settings.

    Args:
        config_path: Path to configuration file

    Returns:
        Dictionary of configuration key-value pairs

    Raises:
        FileNotFoundError: If configuration file doesn't exist
        ValueError: If configuration file is invalid
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    content = config_path.read_text()
    return parse_config(content)
```

### Good TypeScript
```typescript
interface UserSettings {
  readonly userId: string;
  readonly preferences: ReadonlyArray<string>;
  readonly theme: 'light' | 'dark';
}

function processUserSettings(settings: UserSettings): void {
  // Process settings with clear, descriptive variable names
  const { userId, preferences, theme } = settings;

  if (preferences.length === 0) {
    throw new Error('User must have at least one preference');
  }

  applyTheme(theme);
  savePreferences(userId, preferences);
}
```

## Code Organization
1. Group related functionality
2. Separate concerns clearly
3. Use consistent file/directory structure
4. Keep modules focused and cohesive
5. Minimize coupling between modules

## Documentation
- All public APIs must have docstrings/JSDoc
- Complex logic requires inline comments
- README for each major module
- Architecture decision records (ADRs) for significant choices
