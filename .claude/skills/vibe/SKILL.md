---
name: vibe
description: >-
  Code style, naming conventions, and structural patterns for consistent
  codebases. Use when writing new code, reviewing style choices, or
  establishing project conventions. Covers Python, TypeScript, Go, and
  Rust idioms. Do NOT use for documentation content (use documentation
  skill) or architectural decisions.
metadata:
  author: Geoff
  version: 1.0.0
---

# Vibe

Consistent code style: consistency over cleverness, readability over brevity, explicitness over implicitness.

## Instructions

### Step 1: Follow Language Idioms

**Python**: PEP 8/257, type hints, dataclasses/Pydantic, pathlib, async/await for I/O.

**TypeScript**: Strict mode, interfaces over types for objects, const assertions, `unknown` over `any`.

**Go**: Effective Go, gofmt/goimports, composition over inheritance, small interfaces, context.Context.

**Rust**: API guidelines, clippy, owned types at boundaries, Result<T,E>, leverage type system.

### Step 2: Apply Naming Conventions

| Element | Python | TypeScript | Go | Rust |
|---------|--------|------------|-----|------|
| Functions | `snake_case` | `camelCase` | `CamelCase`/`camelCase` | `snake_case` |
| Classes/Types | `PascalCase` | `PascalCase` | `PascalCase` | `PascalCase` |
| Constants | `SCREAMING_SNAKE` | `SCREAMING_SNAKE` | `CamelCase` | `SCREAMING_SNAKE` |

### Step 3: Avoid Anti-Patterns

- No clever code over clear code
- No abbreviations (unless universally understood)
- No deep nesting (> 3 levels)
- No long functions (> 50 lines)
- No mixed abstraction levels
- No magic numbers without constants
- No global state or god objects

## Examples

### Example 1: Good Python Style

```python
from pathlib import Path
from typing import Protocol

class FileReader(Protocol):
    """Protocol for reading file contents."""
    def read_text(self) -> str: ...

def process_config_file(config_path: Path) -> dict[str, str]:
    """Process configuration file and return parsed settings.

    Args:
        config_path: Path to configuration file

    Returns:
        Dictionary of configuration key-value pairs

    Raises:
        FileNotFoundError: If configuration file doesn't exist
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    return parse_config(config_path.read_text())
```

### Example 2: Good TypeScript Style

```typescript
interface UserSettings {
  readonly userId: string;
  readonly preferences: ReadonlyArray<string>;
  readonly theme: 'light' | 'dark';
}

function processUserSettings(settings: UserSettings): void {
  const { userId, preferences, theme } = settings;

  if (preferences.length === 0) {
    throw new Error('User must have at least one preference');
  }

  applyTheme(theme);
  savePreferences(userId, preferences);
}
```

## Troubleshooting

### Error: Inconsistent style across the codebase
- Run the language's formatter (Black, Prettier, gofmt, rustfmt)
- Check for an existing style configuration in the project
- Follow existing patterns in the codebase over personal preference

### Error: Function is getting too long
- Extract logical blocks into well-named helper functions
- Each function should do one thing at one abstraction level
- If you need comments to separate sections, those sections should be functions
