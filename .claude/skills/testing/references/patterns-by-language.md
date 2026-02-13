# Testing Patterns by Language

## Python (pytest)

### Fixtures
```python
@pytest.fixture
def sample_config() -> dict[str, str]:
    return {"api_key": "test_key_123", "base_url": "https://api.example.com", "timeout": "30"}

@pytest.fixture
def config_file(tmp_path: Path, sample_config: dict) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(sample_config))
    return config_path

@pytest.fixture
def mock_api_client(mocker) -> Iterator[Mock]:
    client = mocker.Mock(spec=APIClient)
    client.fetch.return_value = {"status": "success"}
    yield client
    assert client.fetch.called
```

### Async Testing
```python
@pytest.mark.asyncio
async def test_async_fetch_returns_data() -> None:
    client = AsyncAPIClient(base_url="https://api.test.com")
    result = await client.fetch_user("user123")
    assert result["id"] == "user123"

@pytest.fixture
async def async_client() -> AsyncGenerator[AsyncAPIClient, None]:
    client = AsyncAPIClient()
    await client.connect()
    yield client
    await client.disconnect()
```

### Mocking
```python
def test_service_calls_api_correctly(mocker) -> None:
    mock_requests = mocker.patch("requests.get")
    mock_response = Mock()
    mock_response.json.return_value = {"data": "test"}
    mock_response.status_code = 200
    mock_requests.return_value = mock_response

    service = ExternalService()
    result = service.fetch_data("endpoint")

    mock_requests.assert_called_once_with(
        "https://api.example.com/endpoint", timeout=10,
        headers={"Authorization": "Bearer token"},
    )
    assert result == {"data": "test"}
```

## TypeScript (Jest)

### Unit Tests
```typescript
describe('ConfigLoader', () => {
  let loader: ConfigLoader;
  beforeEach(() => { loader = new ConfigLoader(); });

  it('should load valid configuration', async () => {
    jest.spyOn(fs.promises, 'readFile').mockResolvedValue(JSON.stringify({ apiKey: 'test' }));
    const result = await loader.load('/tmp/config.json');
    expect(result).toEqual({ apiKey: 'test' });
  });

  it.each([
    ['', 'empty'],
    ['{invalid', 'invalid JSON'],
  ])('should reject invalid content: %s', async (content, errorMsg) => {
    jest.spyOn(fs.promises, 'readFile').mockResolvedValue(content);
    await expect(loader.load('/tmp/config.json')).rejects.toThrow(errorMsg);
  });
});
```

### React Component Testing
```typescript
import { render, screen, fireEvent } from '@testing-library/react';

describe('UserProfile', () => {
  it('should display user information', () => {
    render(<UserProfile user={{ name: 'John', email: 'john@test.com' }} />);
    expect(screen.getByText('John')).toBeInTheDocument();
  });

  it('should call onEdit when clicked', () => {
    const mockOnEdit = jest.fn();
    render(<UserProfile user={{ name: 'John' }} onEdit={mockOnEdit} />);
    fireEvent.click(screen.getByRole('button', { name: /edit/i }));
    expect(mockOnEdit).toHaveBeenCalled();
  });
});
```

## Go

### Table-Driven Tests
```go
func TestValidateConfig(t *testing.T) {
    tests := []struct {
        name    string
        config  Config
        wantErr bool
    }{
        {"valid", Config{APIKey: "key", Timeout: 30}, false},
        {"missing key", Config{Timeout: 30}, true},
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

### Mock Interfaces
```go
type mockAPIClient struct {
    fetchFunc func(string) (interface{}, error)
}

func (m *mockAPIClient) Fetch(url string) (interface{}, error) {
    if m.fetchFunc != nil { return m.fetchFunc(url) }
    return nil, nil
}
```

## Rust

### Unit Tests
```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_valid_config() {
        let json = r#"{"api_key": "test123", "timeout": 30}"#;
        let result = parse_config(json);
        assert!(result.is_ok());
        let config = result.unwrap();
        assert_eq!(config.api_key, "test123");
    }

    #[test]
    fn test_parse_invalid_json_returns_error() {
        let result = parse_config("{invalid}");
        assert!(result.is_err());
    }
}
```

### Property-Based Testing (proptest)
```rust
proptest! {
    #[test]
    fn test_roundtrip(api_key in "[a-zA-Z0-9]{10,50}", timeout in 1u32..3600u32) {
        let original = Config { api_key, timeout };
        let json = serde_json::to_string(&original).unwrap();
        let parsed: Config = serde_json::from_str(&json).unwrap();
        prop_assert_eq!(original.api_key, parsed.api_key);
    }
}
```
