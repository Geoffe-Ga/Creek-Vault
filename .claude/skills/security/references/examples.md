# Security Examples

## Example 1: Comprehensive Input Validator (Python)

```python
import re
from pathlib import Path
from typing import Optional

class InputValidator:
    """Validate user inputs for security."""

    PROJECT_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')
    EMAIL_PATTERN = re.compile(r'^[^@]+@[^@]+\.[^@]+$')
    RESERVED_NAMES = {'con', 'prn', 'aux', 'nul', 'com1', 'com2', 'lpt1', 'lpt2'}

    @staticmethod
    def validate_project_name(name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("Project name cannot be empty")
        if len(name) > 100:
            raise ValueError(f"Project name too long: {len(name)} chars (max 100)")
        if not InputValidator.PROJECT_NAME_PATTERN.match(name):
            raise ValueError(f"Invalid project name: {name!r}")
        if name[0] in ('-', '_'):
            raise ValueError(f"Project name cannot start with {name[0]!r}")
        if name.lower() in InputValidator.RESERVED_NAMES:
            raise ValueError(f"Project name {name!r} is reserved")
        return name

    @staticmethod
    def validate_email(email: str) -> str:
        email = email.strip().lower()
        if not email:
            raise ValueError("Email cannot be empty")
        if len(email) > 254:
            raise ValueError("Email address too long")
        if not InputValidator.EMAIL_PATTERN.match(email):
            raise ValueError(f"Invalid email format: {email!r}")
        if any(c in email for c in ['\n', '\r', '\0', ';']):
            raise ValueError("Email contains invalid characters")
        return email

    @staticmethod
    def validate_url(url: str, allowed_schemes: Optional[list[str]] = None) -> str:
        if allowed_schemes is None:
            allowed_schemes = ['https', 'git', 'ssh']
        url = url.strip()
        if not url:
            raise ValueError("URL cannot be empty")
        if not any(url.startswith(f"{scheme}://") for scheme in allowed_schemes):
            raise ValueError(f"Invalid URL scheme. Allowed: {', '.join(allowed_schemes)}")
        # Prevent SSRF
        dangerous_hosts = ['localhost', '127.0.0.1', '0.0.0.0', '169.254', '10.', '172.16.', '192.168.']
        if any(host in url.lower() for host in dangerous_hosts):
            raise ValueError("URLs pointing to localhost or private IPs are not allowed")
        return url
```

## Example 2: Secure File Manager (Python)

```python
class SecureFileManager:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir.resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _validate_path(self, file_path: str) -> Path:
        target = (self.base_dir / file_path).resolve()
        try:
            target.relative_to(self.base_dir)
        except ValueError:
            raise ValueError(f"Path traversal detected: {file_path!r}") from None
        return target

    def write_file(self, file_path: str, content: str) -> None:
        target = self._validate_path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write using temporary file
        temp_fd, temp_path = tempfile.mkstemp(dir=target.parent)
        try:
            with open(temp_fd, 'w', encoding='utf-8') as f:
                f.write(content)
            Path(temp_path).replace(target)
        except Exception:
            Path(temp_path).unlink(missing_ok=True)
            raise

    def read_file(self, file_path: str) -> str:
        target = self._validate_path(file_path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        return target.read_text(encoding='utf-8')
```

## Example 3: Secure API Client (Python)

```python
class SecureAPIClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'MyApp/1.0',
            'X-API-Key': api_key,
        })

    def request(self, method: str, path: str, data: dict | None = None, timeout: int = 30) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        response = self.session.request(
            method=method,
            url=url,
            json=data if method in ('POST', 'PUT', 'PATCH') else None,
            timeout=timeout,
            verify=True,           # Verify SSL certificates
            allow_redirects=False,  # Don't follow redirects (prevent SSRF)
        )
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', '')
        if 'application/json' not in content_type:
            raise ValueError(f"Unexpected content type: {content_type}")
        return response.json()
```
