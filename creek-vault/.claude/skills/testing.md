# Testing Skill

## Purpose
Write comprehensive, maintainable tests that verify correctness, prevent regressions, and serve as living documentation.

## Principles
1. Test behavior, not implementation
2. One assertion concept per test
3. Follow AAA pattern (Arrange-Act-Assert)
4. Write tests first (TDD) when possible
5. Keep tests fast and isolated
6. Make tests readable and self-documenting

## Patterns by Language

### Python
**Unit tests with pytest**
```python
import pytest
from pathlib import Path

class TestConfigLoader:
    """Test suite for configuration loading."""

    def test_load_valid_config_returns_parsed_data(self, tmp_path: Path) -> None:
        """Test loading valid config file returns correct data."""
        # Arrange: Create test config file
        config_file = tmp_path / "config.json"
        config_file.write_text('{"api_key": "test123", "timeout": 30}')
        loader = ConfigLoader()

        # Act: Load configuration
        result = loader.load(config_file)

        # Assert: Verify expected structure
        assert result["api_key"] == "test123"
        assert result["timeout"] == 30

    def test_load_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        """Test loading non-existent file raises FileNotFoundError."""
        # Arrange
        missing_file = tmp_path / "nonexistent.json"
        loader = ConfigLoader()

        # Act & Assert: Verify exception raised
        with pytest.raises(FileNotFoundError) as exc_info:
            loader.load(missing_file)

        assert str(missing_file) in str(exc_info.value)

    @pytest.mark.parametrize(
        "content,expected_error",
        [
            ("", "empty"),
            ("{invalid json}", "invalid JSON"),
            ("[]", "must be object"),
        ],
    )
    def test_load_invalid_content_raises_value_error(
        self,
        tmp_path: Path,
        content: str,
        expected_error: str,
    ) -> None:
        """Test loading invalid content raises ValueError with message."""
        # Arrange
        config_file = tmp_path / "config.json"
        config_file.write_text(content)
        loader = ConfigLoader()

        # Act & Assert
        with pytest.raises(ValueError, match=expected_error):
            loader.load(config_file)
```

**Fixtures for test data**
```python
import pytest
from typing import Iterator

@pytest.fixture
def sample_config() -> dict[str, str]:
    """Provide sample configuration for tests."""
    return {
        "api_key": "test_key_123",
        "base_url": "https://api.example.com",
        "timeout": "30",
    }

@pytest.fixture
def config_file(tmp_path: Path, sample_config: dict[str, str]) -> Path:
    """Create temporary config file with sample data."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(sample_config))
    return config_path

@pytest.fixture
def mock_api_client(mocker) -> Iterator[Mock]:
    """Provide mocked API client."""
    client = mocker.Mock(spec=APIClient)
    client.fetch.return_value = {"status": "success"}
    yield client
    # Teardown: verify client was used correctly
    assert client.fetch.called
```

**Testing async code**
```python
import pytest
import asyncio

@pytest.mark.asyncio
async def test_async_fetch_returns_data() -> None:
    """Test async fetch returns expected data."""
    # Arrange
    client = AsyncAPIClient(base_url="https://api.test.com")

    # Act
    result = await client.fetch_user("user123")

    # Assert
    assert result["id"] == "user123"
    assert "email" in result

@pytest.fixture
async def async_client() -> AsyncGenerator[AsyncAPIClient, None]:
    """Provide async API client with cleanup."""
    client = AsyncAPIClient()
    await client.connect()

    yield client

    await client.disconnect()
```

**Mocking external dependencies**
```python
from unittest.mock import Mock, patch, call

def test_service_calls_external_api_correctly(mocker) -> None:
    """Test service makes correct API calls."""
    # Arrange: Mock external dependency
    mock_requests = mocker.patch("requests.get")
    mock_response = Mock()
    mock_response.json.return_value = {"data": "test"}
    mock_response.status_code = 200
    mock_requests.return_value = mock_response

    service = ExternalService()

    # Act
    result = service.fetch_data("endpoint")

    # Assert: Verify correct API call
    mock_requests.assert_called_once_with(
        "https://api.example.com/endpoint",
        timeout=10,
        headers={"Authorization": "Bearer token"},
    )
    assert result == {"data": "test"}

@patch.dict("os.environ", {"API_KEY": "test_key"})
def test_service_uses_environment_variable() -> None:
    """Test service reads API key from environment."""
    # Arrange
    service = APIService()

    # Act
    api_key = service.get_api_key()

    # Assert
    assert api_key == "test_key"
```

### TypeScript
**Unit tests with Jest**
```typescript
import { describe, it, expect, beforeEach, jest } from '@jest/globals';
import { ConfigLoader } from '../config-loader';

describe('ConfigLoader', () => {
  let loader: ConfigLoader;

  beforeEach(() => {
    loader = new ConfigLoader();
  });

  it('should load valid configuration file', async () => {
    // Arrange
    const configPath = '/tmp/test-config.json';
    const mockConfig = { apiKey: 'test123', timeout: 30 };

    jest.spyOn(fs.promises, 'readFile')
      .mockResolvedValue(JSON.stringify(mockConfig));

    // Act
    const result = await loader.load(configPath);

    // Assert
    expect(result).toEqual(mockConfig);
    expect(fs.promises.readFile).toHaveBeenCalledWith(
      configPath,
      'utf-8'
    );
  });

  it('should throw error for missing file', async () => {
    // Arrange
    const missingPath = '/tmp/nonexistent.json';
    jest.spyOn(fs.promises, 'readFile')
      .mockRejectedValue(new Error('ENOENT'));

    // Act & Assert
    await expect(loader.load(missingPath))
      .rejects
      .toThrow('Configuration file not found');
  });

  it.each([
    ['', 'empty'],
    ['{invalid', 'invalid JSON'],
    ['[]', 'must be object'],
  ])('should reject invalid content: %s', async (content, errorMsg) => {
    // Arrange
    jest.spyOn(fs.promises, 'readFile')
      .mockResolvedValue(content);

    // Act & Assert
    await expect(loader.load('/tmp/config.json'))
      .rejects
      .toThrow(errorMsg);
  });
});
```

**Mocking with Jest**
```typescript
import { jest } from '@jest/globals';

describe('UserService', () => {
  let service: UserService;
  let mockApiClient: jest.Mocked<APIClient>;

  beforeEach(() => {
    // Create typed mock
    mockApiClient = {
      get: jest.fn(),
      post: jest.fn(),
      delete: jest.fn(),
    } as jest.Mocked<APIClient>;

    service = new UserService(mockApiClient);
  });

  it('should fetch user by id', async () => {
    // Arrange
    const mockUser = { id: '123', name: 'Test User' };
    mockApiClient.get.mockResolvedValue(mockUser);

    // Act
    const user = await service.getUser('123');

    // Assert
    expect(user).toEqual(mockUser);
    expect(mockApiClient.get).toHaveBeenCalledWith('/users/123');
  });

  it('should handle API errors gracefully', async () => {
    // Arrange
    mockApiClient.get.mockRejectedValue(
      new Error('Network error')
    );

    // Act & Assert
    await expect(service.getUser('123'))
      .rejects
      .toThrow('Failed to fetch user 123');
  });
});
```

**Testing React components**
```typescript
import { render, screen, fireEvent } from '@testing-library/react';
import { UserProfile } from './UserProfile';

describe('UserProfile', () => {
  it('should display user information', () => {
    // Arrange
    const user = { name: 'John Doe', email: 'john@example.com' };

    // Act
    render(<UserProfile user={user} />);

    // Assert
    expect(screen.getByText('John Doe')).toBeInTheDocument();
    expect(screen.getByText('john@example.com')).toBeInTheDocument();
  });

  it('should call onEdit when edit button clicked', () => {
    // Arrange
    const mockOnEdit = jest.fn();
    const user = { name: 'John Doe', email: 'john@example.com' };

    // Act
    render(<UserProfile user={user} onEdit={mockOnEdit} />);
    fireEvent.click(screen.getByRole('button', { name: /edit/i }));

    // Assert
    expect(mockOnEdit).toHaveBeenCalledWith(user);
  });
});
```

### Go
**Table-driven tests**
```go
package config

import (
    "testing"
)

func TestValidateConfig(t *testing.T) {
    tests := []struct {
        name    string
        config  Config
        wantErr bool
        errMsg  string
    }{
        {
            name: "valid config",
            config: Config{
                APIKey:  "test_key_123",
                Timeout: 30,
            },
            wantErr: false,
        },
        {
            name: "missing API key",
            config: Config{
                APIKey:  "",
                Timeout: 30,
            },
            wantErr: true,
            errMsg:  "api_key is required",
        },
        {
            name: "invalid timeout",
            config: Config{
                APIKey:  "test_key",
                Timeout: 0,
            },
            wantErr: true,
            errMsg:  "timeout must be positive",
        },
    }

    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            // Act
            err := ValidateConfig(tt.config)

            // Assert
            if (err != nil) != tt.wantErr {
                t.Errorf("ValidateConfig() error = %v, wantErr %v",
                    err, tt.wantErr)
                return
            }

            if tt.wantErr && err.Error() != tt.errMsg {
                t.Errorf("ValidateConfig() error = %v, want %v",
                    err.Error(), tt.errMsg)
            }
        })
    }
}
```

**Test helpers and fixtures**
```go
func TestLoadConfig(t *testing.T) {
    // Arrange: Create temporary config file
    tmpDir := t.TempDir()
    configPath := filepath.Join(tmpDir, "config.json")

    configData := `{"api_key": "test123", "timeout": 30}`
    if err := os.WriteFile(configPath, []byte(configData), 0644); err != nil {
        t.Fatalf("Failed to create test config: %v", err)
    }

    // Act
    config, err := LoadConfig(configPath)

    // Assert
    if err != nil {
        t.Fatalf("LoadConfig() unexpected error: %v", err)
    }

    if config.APIKey != "test123" {
        t.Errorf("APIKey = %v, want %v", config.APIKey, "test123")
    }

    if config.Timeout != 30 {
        t.Errorf("Timeout = %v, want %v", config.Timeout, 30)
    }
}

// Helper function for creating test configs
func createTestConfig(t *testing.T, data string) string {
    t.Helper()
    tmpDir := t.TempDir()
    configPath := filepath.Join(tmpDir, "config.json")

    if err := os.WriteFile(configPath, []byte(data), 0644); err != nil {
        t.Fatalf("Failed to create test config: %v", err)
    }

    return configPath
}
```

**Mocking interfaces**
```go
// Mock implementation for testing
type mockAPIClient struct {
    fetchFunc func(string) (interface{}, error)
}

func (m *mockAPIClient) Fetch(url string) (interface{}, error) {
    if m.fetchFunc != nil {
        return m.fetchFunc(url)
    }
    return nil, nil
}

func TestService_FetchData(t *testing.T) {
    // Arrange
    expectedData := map[string]string{"key": "value"}
    mockClient := &mockAPIClient{
        fetchFunc: func(url string) (interface{}, error) {
            if url != "expected_url" {
                t.Errorf("Fetch() called with url = %v, want %v",
                    url, "expected_url")
            }
            return expectedData, nil
        },
    }

    service := NewService(mockClient)

    // Act
    result, err := service.FetchData("expected_url")

    // Assert
    if err != nil {
        t.Fatalf("FetchData() unexpected error: %v", err)
    }

    if !reflect.DeepEqual(result, expectedData) {
        t.Errorf("FetchData() = %v, want %v", result, expectedData)
    }
}
```

### Rust
**Unit tests with #[cfg(test)]**
```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_valid_config() {
        // Arrange
        let json = r#"{"api_key": "test123", "timeout": 30}"#;

        // Act
        let result = parse_config(json);

        // Assert
        assert!(result.is_ok());
        let config = result.unwrap();
        assert_eq!(config.api_key, "test123");
        assert_eq!(config.timeout, 30);
    }

    #[test]
    fn test_parse_invalid_json_returns_error() {
        // Arrange
        let invalid_json = "{invalid}";

        // Act
        let result = parse_config(invalid_json);

        // Assert
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("invalid JSON"));
    }

    #[test]
    #[should_panic(expected = "api_key cannot be empty")]
    fn test_empty_api_key_panics() {
        // Arrange
        let config = Config {
            api_key: String::new(),
            timeout: 30,
        };

        // Act: Should panic
        validate_config(&config);
    }
}
```

**Property-based testing with proptest**
```rust
use proptest::prelude::*;

proptest! {
    #[test]
    fn test_parse_and_serialize_roundtrip(
        api_key in "[a-zA-Z0-9]{10,50}",
        timeout in 1u32..3600u32,
    ) {
        // Arrange
        let original = Config { api_key, timeout };

        // Act: Serialize and deserialize
        let json = serde_json::to_string(&original).unwrap();
        let parsed: Config = serde_json::from_str(&json).unwrap();

        // Assert: Should be identical
        prop_assert_eq!(original.api_key, parsed.api_key);
        prop_assert_eq!(original.timeout, parsed.timeout);
    }

    #[test]
    fn test_timeout_validation(timeout in 0u32..10000u32) {
        let config = Config {
            api_key: "test".to_string(),
            timeout,
        };

        let result = validate_config(&config);

        if timeout == 0 {
            prop_assert!(result.is_err());
        } else {
            prop_assert!(result.is_ok());
        }
    }
}
```

**Mocking with mockall**
```rust
use mockall::{automock, predicate::*};

#[automock]
pub trait APIClient {
    fn fetch(&self, url: &str) -> Result<String, Error>;
}

#[test]
fn test_service_calls_api_client() {
    // Arrange
    let mut mock_client = MockAPIClient::new();
    mock_client
        .expect_fetch()
        .with(eq("https://api.example.com/data"))
        .times(1)
        .returning(|_| Ok("response".to_string()));

    let service = Service::new(mock_client);

    // Act
    let result = service.get_data();

    // Assert
    assert!(result.is_ok());
    assert_eq!(result.unwrap(), "response");
}
```

## Anti-patterns

### Testing Implementation Details
**Bad:**
```python
def test_internal_cache_structure():
    """Testing internal cache implementation."""
    service = DataService()
    service.fetch("key")
    assert service._cache == {"key": "value"}  # Fragile!
```

**Good:**
```python
def test_fetch_returns_cached_data_on_second_call():
    """Test caching behavior through public API."""
    service = DataService()

    first_result = service.fetch("key")
    second_result = service.fetch("key")

    assert first_result == second_result
    # Verify cache hit by checking no second API call made
    assert mock_api.call_count == 1
```

### Multiple Assertions on Different Concepts
**Bad:**
```python
def test_user_service():
    """Test everything at once."""
    service = UserService()

    user = service.create("john@example.com")
    assert user.email == "john@example.com"  # Creation
    assert service.exists("john@example.com")  # Existence check
    assert service.delete(user.id)  # Deletion
    assert not service.exists("john@example.com")  # Verify deleted
```

**Good:**
```python
def test_create_user_sets_email_correctly():
    """Test user creation sets email."""
    service = UserService()
    user = service.create("john@example.com")
    assert user.email == "john@example.com"

def test_exists_returns_true_for_created_user():
    """Test exists() returns true for created user."""
    service = UserService()
    service.create("john@example.com")
    assert service.exists("john@example.com")

def test_delete_removes_user():
    """Test delete() removes user from storage."""
    service = UserService()
    user = service.create("john@example.com")
    service.delete(user.id)
    assert not service.exists("john@example.com")
```

### Slow Tests with Real I/O
**Bad:**
```python
def test_fetch_user_data():
    """Test makes real HTTP request."""
    response = requests.get("https://api.example.com/users/123")
    assert response.status_code == 200  # Slow and fragile!
```

**Good:**
```python
def test_fetch_user_data_calls_correct_endpoint(mocker):
    """Test API endpoint called correctly."""
    mock_get = mocker.patch("requests.get")
    mock_get.return_value.status_code = 200

    service = UserService()
    service.fetch_user("123")

    mock_get.assert_called_once_with("https://api.example.com/users/123")
```

### Unclear Test Names
**Bad:**
```python
def test_1():
    """Test the thing."""
    assert process(data) == expected
```

**Good:**
```python
def test_process_valid_json_returns_parsed_dict():
    """Test process() parses valid JSON to dictionary."""
    json_data = '{"key": "value"}'
    result = process(json_data)
    assert result == {"key": "value"}
```

## Examples

### Example 1: Comprehensive Test Suite
```python
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

class TestConfigurationManager:
    """Test suite for ConfigurationManager."""

    @pytest.fixture
    def manager(self) -> ConfigurationManager:
        """Provide fresh ConfigurationManager instance."""
        return ConfigurationManager()

    @pytest.fixture
    def valid_config_file(self, tmp_path: Path) -> Path:
        """Provide valid configuration file."""
        config = tmp_path / "config.json"
        config.write_text('{"api_key": "abc123", "timeout": 30}')
        return config

    def test_load_valid_config_returns_config_object(
        self,
        manager: ConfigurationManager,
        valid_config_file: Path,
    ) -> None:
        """Test loading valid config returns Config object."""
        # Act
        result = manager.load(valid_config_file)

        # Assert
        assert isinstance(result, Config)
        assert result.api_key == "abc123"
        assert result.timeout == 30

    def test_load_missing_file_raises_file_not_found(
        self,
        manager: ConfigurationManager,
        tmp_path: Path,
    ) -> None:
        """Test loading missing file raises FileNotFoundError."""
        # Arrange
        missing = tmp_path / "nonexistent.json"

        # Act & Assert
        with pytest.raises(FileNotFoundError) as exc:
            manager.load(missing)
        assert "not found" in str(exc.value).lower()

    @pytest.mark.parametrize(
        "content,error_pattern",
        [
            pytest.param(
                "",
                "empty",
                id="empty_file",
            ),
            pytest.param(
                "{invalid json}",
                "invalid JSON",
                id="malformed_json",
            ),
            pytest.param(
                "[]",
                "must be object",
                id="array_instead_of_object",
            ),
            pytest.param(
                '{"timeout": 30}',
                "missing required field.*api_key",
                id="missing_required_field",
            ),
        ],
    )
    def test_load_invalid_content_raises_validation_error(
        self,
        manager: ConfigurationManager,
        tmp_path: Path,
        content: str,
        error_pattern: str,
    ) -> None:
        """Test loading invalid content raises ValidationError."""
        # Arrange
        config_file = tmp_path / "invalid.json"
        config_file.write_text(content)

        # Act & Assert
        with pytest.raises(ValidationError, match=error_pattern):
            manager.load(config_file)

    def test_reload_updates_configuration(
        self,
        manager: ConfigurationManager,
        valid_config_file: Path,
    ) -> None:
        """Test reload() updates configuration from file."""
        # Arrange: Load initial config
        initial = manager.load(valid_config_file)

        # Modify config file
        valid_config_file.write_text('{"api_key": "xyz789", "timeout": 60}')

        # Act: Reload configuration
        manager.reload()

        # Assert: Configuration updated
        updated = manager.current_config
        assert updated.api_key == "xyz789"
        assert updated.timeout == 60
        assert updated.api_key != initial.api_key

    @patch("start_green_stay_green.config.logger")
    def test_load_logs_configuration_loaded(
        self,
        mock_logger: Mock,
        manager: ConfigurationManager,
        valid_config_file: Path,
    ) -> None:
        """Test load() logs successful configuration load."""
        # Act
        manager.load(valid_config_file)

        # Assert: Info logged
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        assert "loaded" in call_args.lower()
        assert str(valid_config_file) in call_args
```

### Example 2: Integration Test
```python
import pytest
from pathlib import Path
import tempfile
import shutil

@pytest.mark.integration
class TestProjectGenerator:
    """Integration tests for project generation."""

    @pytest.fixture
    def output_dir(self) -> Iterator[Path]:
        """Provide temporary output directory."""
        temp_dir = Path(tempfile.mkdtemp())
        yield temp_dir
        shutil.rmtree(temp_dir)

    def test_generate_creates_complete_project_structure(
        self,
        output_dir: Path,
    ) -> None:
        """Test generate() creates all expected files and directories."""
        # Arrange
        generator = ProjectGenerator(
            project_name="test-project",
            language="python",
        )

        # Act
        result = generator.generate(output_dir)

        # Assert: Verify structure
        assert result.success

        # Check directories
        assert (output_dir / "src").is_dir()
        assert (output_dir / "tests").is_dir()
        assert (output_dir / ".github" / "workflows").is_dir()

        # Check files
        assert (output_dir / "README.md").is_file()
        assert (output_dir / "pyproject.toml").is_file()
        assert (output_dir / ".gitignore").is_file()

        # Verify file contents
        readme = (output_dir / "README.md").read_text()
        assert "test-project" in readme

    def test_generated_project_passes_quality_checks(
        self,
        output_dir: Path,
    ) -> None:
        """Test generated project passes all quality checks."""
        # Arrange & Act: Generate project
        generator = ProjectGenerator(
            project_name="quality-test",
            language="python",
        )
        generator.generate(output_dir)

        # Assert: Run quality checks
        # (This would actually run scripts/check-all.sh)
        result = subprocess.run(
            ["./scripts/check-all.sh"],
            cwd=output_dir,
            capture_output=True,
        )

        assert result.returncode == 0, (
            f"Quality checks failed:\n"
            f"stdout: {result.stdout.decode()}\n"
            f"stderr: {result.stderr.decode()}"
        )
```

### Example 3: Testing with Property-Based Tests
```python
from hypothesis import given, strategies as st

@given(
    project_name=st.text(
        min_size=1,
        max_size=50,
        alphabet=st.characters(
            whitelist_categories=("Lu", "Ll", "Nd"),
            blacklist_characters="-_/\\",
        ),
    )
)
def test_validate_project_name_accepts_valid_names(
    project_name: str,
) -> None:
    """Test validator accepts all valid project names."""
    # Act
    result = validate_project_name(project_name)

    # Assert
    assert result.is_valid
    assert not result.errors

@given(
    email=st.emails(),
    username=st.text(min_size=3, max_size=20),
)
def test_user_creation_is_idempotent(
    email: str,
    username: str,
) -> None:
    """Test creating user twice with same data produces same result."""
    # Arrange
    service = UserService()

    # Act: Create user twice
    user1 = service.create(email=email, username=username)
    user2 = service.create(email=email, username=username)

    # Assert: Same user returned
    assert user1.id == user2.id
    assert user1.email == user2.email
```

## Coverage Best Practices

1. **Aim for 90%+ line coverage, 80%+ branch coverage**
2. **Test edge cases explicitly**
   - Empty inputs
   - Boundary values (0, -1, MAX_INT)
   - Null/None values
   - Concurrent access

3. **Use coverage reports to find gaps**
```bash
pytest --cov=start_green_stay_green --cov-report=html
open htmlcov/index.html
```

4. **Don't test third-party code**
   - Mock external libraries
   - Focus on your code's logic

5. **Test error paths, not just happy paths**
```python
def test_process_handles_all_error_types():
    """Test all error conditions are handled."""
    # Test ValueError
    with pytest.raises(ValueError):
        process(invalid_input)

    # Test IOError
    with pytest.raises(IOError):
        process(unreadable_file)

    # Test timeout
    with pytest.raises(TimeoutError):
        process(slow_operation)
```
