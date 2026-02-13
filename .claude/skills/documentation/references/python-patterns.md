# Python Documentation Patterns

## Google-Style Docstrings

```python
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

    Examples:
        Basic usage:
        >>> result = process_file(Path("input.txt"), Path("output.txt"))
        >>> print(f"Processed {result['lines_processed']} lines")

        Skip validation:
        >>> result = process_file(
        ...     Path("messy.txt"), Path("out.txt"), validate=False
        ... )

    Note:
        Uses atomic writes. Output file is never in a partial state.

    See Also:
        process_file_streaming: Memory-efficient alternative for large files
    """
```

## Class Docstrings

```python
class ConfigurationManager:
    """Manage application configuration with validation and reloading.

    Handles loading, validating, and hot-reloading of application
    configuration from JSON or YAML files.

    Attributes:
        config_path: Path to primary configuration file.
        current_config: Currently loaded configuration dictionary.

    Examples:
        >>> manager = ConfigurationManager("config.json")
        >>> api_key = manager.get("api_key")
        >>> timeout = manager.get("timeout", default=30)

    Note:
        Thread-safe for concurrent reads. Write operations (reload)
        acquire an exclusive lock.
    """
```

## Module-Level Docstrings

```python
"""Configuration management for the application.

This module provides configuration loading, validation, and management
utilities. Supports JSON, YAML, TOML formats and environment overrides.

Key Components:
    - ConfigurationManager: Primary configuration interface
    - ConfigValidator: Schema validation for configurations

Usage:
    >>> from myapp.config import ConfigurationManager
    >>> manager = ConfigurationManager("config.json")
    >>> api_key = manager.get("api_key")

Environment Variables:
    APP_CONFIG_PATH: Override default configuration file path
    APP_ENV: Environment name (dev, staging, production)
"""
```
