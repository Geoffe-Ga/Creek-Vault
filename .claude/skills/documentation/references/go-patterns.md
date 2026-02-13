# Go Documentation Patterns

## Package Documentation

```go
// Package config provides configuration management for applications.
//
// This package supports loading configuration from JSON, YAML, and TOML files,
// with environment-specific overrides and validation.
//
// Basic Usage
//
//	manager, err := config.NewManager("config.json")
//	if err != nil {
//	    log.Fatal(err)
//	}
//	apiKey := manager.Get("api_key")
//
// Environment Overrides
//
//	export APP_API_KEY="override-key"
//
// See examples/ directory for complete usage examples.
package config
```

## Function Documentation

```go
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
//     ValidationError if content is invalid
//
// Example:
//
//	config, err := LoadConfig("config.json")
//	if err != nil {
//	    log.Fatal(err)
//	}
//	fmt.Println("API Key:", config.APIKey)
func LoadConfig(path string) (*Config, error) {
    // Implementation...
}
```

## Struct Documentation

```go
// Manager manages application configuration with validation and hot-reloading.
//
// Thread Safety:
// Manager is safe for concurrent reads. Reload operations acquire an
// exclusive lock.
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
}
```
