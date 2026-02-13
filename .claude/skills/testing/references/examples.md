# Testing Examples

## Example 1: Comprehensive Python Test Suite

```python
class TestConfigurationManager:
    @pytest.fixture
    def manager(self) -> ConfigurationManager:
        return ConfigurationManager()

    @pytest.fixture
    def valid_config_file(self, tmp_path: Path) -> Path:
        config = tmp_path / "config.json"
        config.write_text('{"api_key": "abc123", "timeout": 30}')
        return config

    def test_load_valid_config_returns_config_object(self, manager, valid_config_file):
        result = manager.load(valid_config_file)
        assert isinstance(result, Config)
        assert result.api_key == "abc123"
        assert result.timeout == 30

    def test_load_missing_file_raises_file_not_found(self, manager, tmp_path):
        with pytest.raises(FileNotFoundError) as exc:
            manager.load(tmp_path / "nonexistent.json")
        assert "not found" in str(exc.value).lower()

    @pytest.mark.parametrize("content,error_pattern", [
        pytest.param("", "empty", id="empty_file"),
        pytest.param("{invalid}", "invalid JSON", id="malformed_json"),
        pytest.param("[]", "must be object", id="array_not_object"),
        pytest.param('{"timeout": 30}', "missing required.*api_key", id="missing_field"),
    ])
    def test_load_invalid_content_raises_validation_error(self, manager, tmp_path, content, error_pattern):
        config_file = tmp_path / "invalid.json"
        config_file.write_text(content)
        with pytest.raises(ValidationError, match=error_pattern):
            manager.load(config_file)

    def test_reload_updates_configuration(self, manager, valid_config_file):
        manager.load(valid_config_file)
        valid_config_file.write_text('{"api_key": "xyz789", "timeout": 60}')
        manager.reload()
        assert manager.current_config.api_key == "xyz789"
        assert manager.current_config.timeout == 60
```

## Example 2: Integration Test

```python
@pytest.mark.integration
class TestProjectGenerator:
    @pytest.fixture
    def output_dir(self) -> Iterator[Path]:
        temp_dir = Path(tempfile.mkdtemp())
        yield temp_dir
        shutil.rmtree(temp_dir)

    def test_generate_creates_complete_project(self, output_dir):
        generator = ProjectGenerator(project_name="test-project", language="python")
        result = generator.generate(output_dir)
        assert result.success
        assert (output_dir / "src").is_dir()
        assert (output_dir / "tests").is_dir()
        assert (output_dir / "README.md").is_file()
        readme = (output_dir / "README.md").read_text()
        assert "test-project" in readme
```

## Example 3: Property-Based Test (Python)

```python
from hypothesis import given, strategies as st

@given(project_name=st.text(
    min_size=1, max_size=50,
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
))
def test_validate_accepts_valid_names(project_name: str) -> None:
    result = validate_project_name(project_name)
    assert result.is_valid
    assert not result.errors
```
