# TypeScript Documentation Patterns

## TSDoc Comments

```typescript
/**
 * Process user input and validate against schema.
 *
 * Performs comprehensive validation including type checking,
 * format validation, and business rule enforcement.
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
 * @see {@link ValidationOptions} for available options
 * @since 1.0.0
 */
export function processInput(
  input: unknown,
  schema: JSONSchema,
  options?: ValidationOptions
): UserData {
  // Implementation...
}
```

## Class Documentation

```typescript
/**
 * Configuration manager for application settings.
 *
 * Handles loading, validation, and hot-reloading from JSON/YAML files.
 * Supports environment-specific overrides and type-safe access.
 *
 * @example
 * ```typescript
 * const manager = new ConfigManager('config.json');
 * const apiKey = manager.get('apiKey');
 * ```
 *
 * @example
 * With auto-reload:
 * ```typescript
 * const manager = new ConfigManager('config.json', {
 *   autoReload: true,
 *   onReload: (config) => console.log('Config reloaded')
 * });
 * ```
 */
export class ConfigManager {
  /** Path to primary configuration file. */
  public readonly configPath: string;

  /**
   * Create new configuration manager.
   *
   * @param configPath - Path to configuration file
   * @param options - Optional configuration settings
   * @throws {FileNotFoundError} If config file doesn't exist
   */
  constructor(configPath: string, options?: ConfigManagerOptions) {
    // Implementation...
  }
}
```

## README Structure

```markdown
# Component Name

## Overview
Brief description.

## Installation
npm install @scope/component

## Quick Start
[Code example]

## API Reference
### `ClassName`
#### Constructor
#### Methods

## Configuration
### Environment Variables
### Configuration File

## Error Handling
[Typed errors and how to handle them]

## Examples
See examples/ directory.
```
