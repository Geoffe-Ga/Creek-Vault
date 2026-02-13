# Rust Documentation Patterns

## Function Doc Comments

```rust
/// Process input file and write results to output file.
///
/// Reads the input file, performs validation if enabled, processes the content,
/// and writes formatted results to the output file.
///
/// # Arguments
///
/// * `input_path` - Path to input file. Must exist and be readable.
/// * `output_path` - Path to output file. Parent directory must exist.
/// * `validate` - Whether to validate input before processing.
///
/// # Returns
///
/// Returns `HashMap` containing processing statistics:
/// - `lines_processed`: Number of lines processed
/// - `tokens_found`: Number of tokens extracted
///
/// # Errors
///
/// Returns error if:
/// - Input file doesn't exist (`io::ErrorKind::NotFound`)
/// - Permission denied (`io::ErrorKind::PermissionDenied`)
/// - Validation fails (when `validate=true`)
///
/// # Examples
///
/// ```
/// use std::path::Path;
///
/// let stats = process_file(
///     Path::new("input.txt"),
///     Path::new("output.txt"),
///     true
/// ).expect("Failed to process file");
///
/// println!("Processed {} lines", stats.get("lines_processed").unwrap());
/// ```
///
/// # See Also
///
/// * [`process_file_streaming`] - Memory-efficient alternative
pub fn process_file(
    input_path: &Path,
    output_path: &Path,
    validate: bool,
) -> Result<HashMap<String, usize>, Error> {
    // Implementation...
}
```

## Struct Doc Comments

```rust
/// Configuration manager for application settings.
///
/// Handles loading, validation, and hot-reloading from JSON/YAML files.
///
/// # Thread Safety
///
/// `ConfigManager` is safe for concurrent reads using `Arc` and `RwLock`.
///
/// # Examples
///
/// ```
/// let manager = ConfigManager::new("config.json")?;
/// let api_key = manager.get("api_key")?;
/// ```
pub struct ConfigManager {
    /// Path to primary configuration file
    pub config_path: PathBuf,

    /// Currently loaded configuration (read-only access via getter)
    current_config: Arc<RwLock<Config>>,
}
```
