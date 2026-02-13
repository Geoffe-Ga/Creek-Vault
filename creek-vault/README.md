# creek-vault

Creek Vault - A quality-controlled Python project generated with
Start Green Stay Green.

## Description

This project was generated with maximum quality standards from day one, including:

- ✅ Comprehensive testing infrastructure (pytest with 90%+ coverage requirement)
- ✅ Code quality tools (ruff, black, isort, mypy)
- ✅ Security scanning (bandit, safety)
- ✅ Complexity analysis (radon, xenon)
- ✅ Pre-commit hooks (32 quality checks)
- ✅ CI/CD pipeline (GitHub Actions)
- ✅ AI-assisted development (Claude Code skills and subagents)

## Installation

```bash
# Clone the repository
git clone <repository-url>
cd creek-vault

# Install dependencies
pip install -r requirements-dev.txt

# Install pre-commit hooks
pre-commit install
```

## Usage

Run the Hello World application:

```bash
python -m creek_vault.main
```

Expected output:
```
Hello from creek-vault!
```

## Development

### Running Quality Checks

```bash
# Run all quality checks (recommended before commit)
pre-commit run --all-files

# Or run individual checks:
./scripts/test.sh          # Run tests with coverage
./scripts/lint.sh          # Run linting
./scripts/format.sh --fix  # Auto-format code
./scripts/typecheck.sh     # Run type checking
./scripts/check-all.sh     # Run all checks
```

### Quality Tools

This project includes:

- **pytest**: Testing framework with 90%+ coverage requirement
- **ruff**: Fast Python linter (replaces flake8, isort, and more)
- **black**: Code formatter
- **isort**: Import sorting
- **mypy**: Static type checker
- **bandit**: Security linter
- **safety**: Dependency vulnerability scanner
- **radon/xenon**: Code complexity analysis (≤10 cyclomatic complexity)
- **pre-commit**: Git hooks framework (32 quality checks)

### Project Structure

```
creek-vault/
├── creek_vault/     # Main package
│   ├── __init__.py
│   └── main.py
├── tests/                # Test suite
│   ├── __init__.py
│   └── test_main.py
├── scripts/              # Quality control scripts
│   ├── check-all.sh
│   ├── test.sh
│   ├── lint.sh
│   ├── format.sh
│   ├── typecheck.sh
│   ├── coverage.sh
│   ├── security.sh
│   └── complexity.sh
├── .github/workflows/    # CI/CD pipelines
├── .claude/              # AI subagents and skills
├── requirements.txt      # Runtime dependencies
├── requirements-dev.txt  # Development dependencies
├── pyproject.toml        # Tool configurations
└── .pre-commit-config.yaml  # Pre-commit hooks
```

### Testing

```bash
# Run tests
./scripts/test.sh

# Run tests with coverage report
./scripts/coverage.sh

# Run tests with HTML coverage report
./scripts/coverage.sh --html
# View htmlcov/index.html in browser
```

### Code Quality

This project maintains MAXIMUM QUALITY standards:

- **Test Coverage**: ≥90% required
- **Cyclomatic Complexity**: ≤10 per function
- **All Linters**: Must pass with zero violations
- **Type Coverage**: 100% type hints

## License

MIT License

## Attribution

Generated with [Start Green Stay Green](https://github.com/Geoffe-Ga/start_green_stay_green)
- Maximum quality Python projects from day one.
