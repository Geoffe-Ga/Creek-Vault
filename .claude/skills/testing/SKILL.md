---
name: testing
description: >-
  Write comprehensive, maintainable tests following TDD and AAA pattern.
  Use when writing unit tests, integration tests, setting up fixtures,
  mocking dependencies, or improving test coverage. Covers Python (pytest),
  TypeScript (Jest), Go (testing), and Rust (built-in + proptest).
  Do NOT use for mutation testing specifics (use mutation-testing skill).
metadata:
  author: Geoff
  version: 1.0.0
---

# Testing

Test behavior, not implementation. One assertion concept per test. Follow AAA (Arrange-Act-Assert).

## Instructions

### Step 1: Follow the Principles

1. Test behavior, not implementation
2. One assertion concept per test
3. Follow AAA pattern (Arrange-Act-Assert)
4. Write tests first (TDD) when possible
5. Keep tests fast and isolated
6. Make tests readable and self-documenting

### Step 2: Apply Language-Specific Patterns

See `references/patterns-by-language.md` for detailed patterns:
- **Python**: pytest classes, fixtures, parametrize, async testing, mocking
- **TypeScript**: Jest describe/it, spyOn, React Testing Library
- **Go**: Table-driven tests, test helpers, mock interfaces
- **Rust**: #[cfg(test)] modules, proptest, mockall

### Step 3: Follow Coverage Best Practices

- Aim for 90%+ line coverage, 80%+ branch coverage
- Test edge cases: empty inputs, boundary values, null/None, concurrent access
- Test error paths, not just happy paths
- Don't test third-party code - mock external libraries
- Use coverage reports to find gaps: `pytest --cov=package --cov-report=html`

## Examples

See `references/examples.md` for complete test suite examples.

### Example 1: Python Unit Test with Parametrize

```python
class TestConfigLoader:
    def test_load_valid_config_returns_parsed_data(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text('{"api_key": "test123", "timeout": 30}')
        loader = ConfigLoader()
        result = loader.load(config_file)
        assert result["api_key"] == "test123"
        assert result["timeout"] == 30

    @pytest.mark.parametrize("content,expected_error", [
        ("", "empty"),
        ("{invalid}", "invalid JSON"),
        ("[]", "must be object"),
    ])
    def test_load_invalid_content_raises_value_error(self, tmp_path, content, expected_error):
        config_file = tmp_path / "config.json"
        config_file.write_text(content)
        with pytest.raises(ValueError, match=expected_error):
            ConfigLoader().load(config_file)
```

### Example 2: Go Table-Driven Test

```go
func TestValidateConfig(t *testing.T) {
    tests := []struct {
        name    string
        config  Config
        wantErr bool
        errMsg  string
    }{
        {"valid config", Config{APIKey: "test", Timeout: 30}, false, ""},
        {"missing key", Config{APIKey: "", Timeout: 30}, true, "api_key is required"},
        {"bad timeout", Config{APIKey: "test", Timeout: 0}, true, "timeout must be positive"},
    }
    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            err := ValidateConfig(tt.config)
            if (err != nil) != tt.wantErr {
                t.Errorf("error = %v, wantErr %v", err, tt.wantErr)
            }
        })
    }
}
```

## Troubleshooting

### Error: Tests are slow
- Mock external dependencies (HTTP, database, filesystem)
- Use `tmp_path` fixture instead of real filesystem
- Run unit tests separately from integration tests
- Profile with `pytest --durations=10`

### Error: Tests are flaky (sometimes pass, sometimes fail)
- Look for shared state between tests
- Check for test order dependencies (use pytest-randomly)
- Mock time-dependent code
- Avoid sleep() in tests - use polling with timeout
