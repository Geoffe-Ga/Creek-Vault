# Security Patterns by Language

## Python

### Input Validation
```python
import re

def validate_project_name(name: str) -> None:
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError(f"Invalid project name: {name!r}. Only letters, numbers, hyphens, underscores.")
    if len(name) > 100:
        raise ValueError(f"Project name too long: {len(name)} chars (max 100)")
    if name.startswith(('-', '_')):
        raise ValueError(f"Project name cannot start with {name[0]!r}")
    reserved = {'con', 'prn', 'aux', 'nul', 'com1', 'lpt1'}
    if name.lower() in reserved:
        raise ValueError(f"Project name {name!r} is reserved")
```

### Path Traversal Prevention
```python
def safe_path_join(base_dir: Path, user_path: str) -> Path:
    base_dir = base_dir.resolve()
    target = (base_dir / user_path).resolve()
    try:
        target.relative_to(base_dir)
    except ValueError:
        raise ValueError(f"Path traversal detected: {user_path!r}") from None
    return target
```

### Subprocess Safety
```python
def run_git_command(args: list[str], cwd: Path) -> str:
    dangerous_chars = {';', '|', '&', '$', '`', '\n', '\r'}
    for arg in args:
        if any(char in arg for char in dangerous_chars):
            raise ValueError(f"Argument contains dangerous characters: {arg!r}")
    # NEVER use shell=True
    result = subprocess.run(['git'] + args, cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout
```

### SQL Injection Prevention
```python
# WRONG - SQL injection
query = f"SELECT * FROM users WHERE email = '{email}'"
cursor.execute(query)

# CORRECT - parameterized query
query = "SELECT * FROM users WHERE email = ?"
cursor.execute(query, (email,))
```

### API Key Management
```python
import keyring

def get_api_key(key_name: str) -> str:
    api_key = keyring.get_password("my_app", key_name)
    if not api_key:
        raise ValueError(f"API key '{key_name}' not found in OS keyring.")
    return api_key

# WRONG: API_KEY = "sk-ant-1234567890"
# WRONG: api_key = os.environ.get("API_KEY")
# CORRECT: api_key = get_api_key("claude_api_key")
```

## TypeScript

### Input Validation
```typescript
export class InputValidator {
  static validateEmail(email: string): string {
    email = email.trim().toLowerCase();
    if (!validator.isEmail(email)) throw new Error(`Invalid email: ${email}`);
    if (email.length > 254) throw new Error('Email too long');
    return email;
  }

  static sanitizeHtml(html: string): string {
    return validator.escape(html);
  }
}
```

### XSS Prevention
```typescript
function renderUserContent(content: string): string {
  return content
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;');
}
// React/JSX escapes by default - {content} is safe
// dangerouslySetInnerHTML should be avoided
```

### CSRF Protection
```typescript
import crypto from 'crypto';

function generateCsrfToken(): string {
  return crypto.randomBytes(32).toString('hex');
}

function validateCsrfToken(token: string, sessionToken: string): boolean {
  return crypto.timingSafeEqual(Buffer.from(token), Buffer.from(sessionToken));
}
```

## Go

### Input Validation
```go
var projectNameRegex = regexp.MustCompile(`^[a-zA-Z0-9_-]+$`)

func ValidateProjectName(name string) error {
    name = strings.TrimSpace(name)
    if name == "" { return errors.New("project name cannot be empty") }
    if len(name) > 100 { return errors.New("project name too long (max 100)") }
    if !projectNameRegex.MatchString(name) { return errors.New("invalid characters in project name") }
    return nil
}
```

### Path Traversal Prevention
```go
func SafePathJoin(baseDir, userPath string) (string, error) {
    baseDir = filepath.Clean(baseDir)
    target := filepath.Join(baseDir, filepath.Clean(userPath))
    if !strings.HasPrefix(target, baseDir+string(filepath.Separator)) {
        return "", errors.New("path traversal detected")
    }
    return target, nil
}
```

### Command Injection Prevention
```go
// exec.Command does NOT use shell - safe by default
cmd := exec.CommandContext(ctx, "git", args...)
cmd.Dir = dir
output, err := cmd.CombinedOutput()
```

## Rust

### Input Validation
```rust
lazy_static! {
    static ref PROJECT_NAME_REGEX: Regex = Regex::new(r"^[a-zA-Z0-9_-]+$").unwrap();
}

pub fn validate_project_name(name: &str) -> Result<String, Box<dyn Error>> {
    let name = name.trim();
    if name.is_empty() { return Err("Project name cannot be empty".into()); }
    if name.len() > 100 { return Err("Project name too long".into()); }
    if !PROJECT_NAME_REGEX.is_match(name) { return Err("Invalid characters".into()); }
    Ok(name.to_string())
}
```

### Path Traversal Prevention
```rust
pub fn safe_path_join(base_dir: &Path, user_path: &str) -> Result<PathBuf, io::Error> {
    let base_dir = base_dir.canonicalize()?;
    let target = base_dir.join(user_path).canonicalize()?;
    if !target.starts_with(&base_dir) {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Path traversal detected"));
    }
    Ok(target)
}
```
