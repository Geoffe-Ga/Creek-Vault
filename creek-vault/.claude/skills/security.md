# Security Skill

## Purpose
Implement secure coding practices that protect against common vulnerabilities and maintain system integrity.

## Principles
1. Defense in depth - multiple layers of security
2. Least privilege - minimal access required
3. Fail securely - errors don't expose sensitive data
4. Input validation at all boundaries
5. Never trust user input
6. Secure by default, not by configuration

## Patterns by Language

### Python
**Input validation and sanitization**
```python
import re
from pathlib import Path
from typing import Optional

def validate_project_name(name: str) -> None:
    """Validate project name is safe for file system.

    Args:
        name: Project name to validate

    Raises:
        ValueError: If name contains unsafe characters
    """
    # Only allow alphanumeric, hyphens, underscores
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError(
            f"Invalid project name: {name!r}. "
            f"Only letters, numbers, hyphens, and underscores allowed."
        )

    if len(name) > 100:
        raise ValueError(
            f"Project name too long: {len(name)} chars (max 100)"
        )

    if name.startswith(('-', '_')):
        raise ValueError(
            f"Project name cannot start with {name[0]!r}"
        )

    # Prevent reserved names
    reserved = {'con', 'prn', 'aux', 'nul', 'com1', 'lpt1'}
    if name.lower() in reserved:
        raise ValueError(
            f"Project name {name!r} is reserved and cannot be used"
        )


def validate_email(email: str) -> str:
    """Validate and normalize email address.

    Args:
        email: Email address to validate

    Returns:
        Normalized email address (lowercase)

    Raises:
        ValueError: If email format is invalid
    """
    # Basic format validation
    email = email.strip().lower()

    if not email:
        raise ValueError("Email cannot be empty")

    if len(email) > 254:  # RFC 5321
        raise ValueError("Email address too long")

    # Simple regex - don't try to fully validate email with regex
    # Real validation requires sending verification email
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
        raise ValueError(f"Invalid email format: {email!r}")

    # Prevent email injection
    if any(char in email for char in ['\n', '\r', '\0']):
        raise ValueError("Email contains invalid characters")

    return email
```

**Path traversal prevention**
```python
from pathlib import Path

def safe_path_join(base_dir: Path, user_path: str) -> Path:
    """Safely join user-provided path to base directory.

    Prevents path traversal attacks by ensuring resolved path
    is within base directory.

    Args:
        base_dir: Base directory (all files must be within this)
        user_path: User-provided path (relative to base_dir)

    Returns:
        Resolved absolute path within base_dir

    Raises:
        ValueError: If path traversal detected
    """
    # Resolve to absolute path
    base_dir = base_dir.resolve()
    target = (base_dir / user_path).resolve()

    # Ensure target is within base directory
    try:
        target.relative_to(base_dir)
    except ValueError:
        raise ValueError(
            f"Path traversal detected: {user_path!r} "
            f"points outside {base_dir}"
        ) from None

    return target


def create_file_safely(
    base_dir: Path,
    file_path: str,
    content: str,
) -> None:
    """Create file with path traversal protection.

    Args:
        base_dir: Base directory for file creation
        file_path: User-provided file path (relative)
        content: File content to write

    Raises:
        ValueError: If path traversal detected
    """
    # Validate and resolve path
    target = safe_path_join(base_dir, file_path)

    # Create parent directories
    target.parent.mkdir(parents=True, exist_ok=True)

    # Write file
    target.write_text(content, encoding="utf-8")
```

**Subprocess safety - avoid shell injection**
```python
import subprocess
import shlex
from typing import List

def run_git_command(args: List[str], cwd: Path) -> str:
    """Run git command safely without shell injection.

    Args:
        args: Git command arguments (excluding 'git')
        cwd: Working directory for command

    Returns:
        Command output

    Raises:
        subprocess.CalledProcessError: If command fails
        ValueError: If arguments are invalid
    """
    # Validate arguments don't contain shell metacharacters
    dangerous_chars = {';', '|', '&', '$', '`', '\n', '\r'}
    for arg in args:
        if any(char in arg for char in dangerous_chars):
            raise ValueError(
                f"Argument contains dangerous characters: {arg!r}"
            )

    # Build command - NEVER use shell=True
    cmd = ['git'] + args

    # Run safely without shell
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        # IMPORTANT: shell=False (default) prevents injection
    )

    return result.stdout


# WRONG - Shell injection vulnerability
def unsafe_git_clone(repo_url: str) -> None:
    """VULNERABLE - Do not use!"""
    # User can inject commands: repo_url = "repo.git; rm -rf /"
    subprocess.run(
        f"git clone {repo_url}",  # UNSAFE!
        shell=True,  # DANGEROUS!
    )


# CORRECT - No shell injection possible
def safe_git_clone(repo_url: str, target_dir: Path) -> None:
    """Clone git repository safely."""
    # Validate URL format
    if not repo_url.startswith(('https://', 'git://', 'ssh://')):
        raise ValueError(f"Invalid repository URL: {repo_url}")

    # Run without shell - command injection impossible
    subprocess.run(
        ['git', 'clone', repo_url, str(target_dir)],
        check=True,
    )
```

**API key management with OS keyring**
```python
import keyring
from typing import Optional

# Service name for keyring
SERVICE_NAME = "start_green_stay_green"


def store_api_key(key_name: str, api_key: str) -> None:
    """Store API key in OS keyring.

    Args:
        key_name: Name for this API key (e.g., "claude_api_key")
        api_key: API key to store

    Raises:
        ValueError: If key_name or api_key is empty
    """
    if not key_name or not api_key:
        raise ValueError("key_name and api_key cannot be empty")

    # Validate key format (basic check)
    if len(api_key) < 10:
        raise ValueError("API key appears invalid (too short)")

    # Store in OS keyring
    keyring.set_password(SERVICE_NAME, key_name, api_key)


def get_api_key(key_name: str) -> str:
    """Retrieve API key from OS keyring.

    Args:
        key_name: Name of API key to retrieve

    Returns:
        API key value

    Raises:
        ValueError: If API key not found
    """
    api_key = keyring.get_password(SERVICE_NAME, key_name)

    if not api_key:
        raise ValueError(
            f"API key '{key_name}' not found in OS keyring. "
            f"Run setup script to configure API keys."
        )

    return api_key


def delete_api_key(key_name: str) -> None:
    """Delete API key from OS keyring.

    Args:
        key_name: Name of API key to delete
    """
    try:
        keyring.delete_password(SERVICE_NAME, key_name)
    except keyring.errors.PasswordDeleteError:
        # Key doesn't exist - not an error
        pass


# WRONG - Hardcoded API key
API_KEY = "sk-ant-1234567890"  # NEVER DO THIS!


# WRONG - API key in environment variable (can be leaked)
import os
api_key = os.environ.get("CLAUDE_API_KEY")  # Avoid for long-term storage


# CORRECT - Use OS keyring
api_key = get_api_key("claude_api_key")
```

**SQL injection prevention**
```python
import sqlite3
from typing import List, Tuple

def get_user_by_email_unsafe(email: str) -> dict:
    """VULNERABLE - SQL injection possible!"""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    # SQL INJECTION VULNERABILITY!
    # email = "' OR '1'='1" would return all users
    query = f"SELECT * FROM users WHERE email = '{email}'"
    cursor.execute(query)  # UNSAFE!

    return cursor.fetchone()


def get_user_by_email_safe(email: str) -> dict:
    """Safe version using parameterized queries."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    # Parameterized query - SQL injection impossible
    query = "SELECT * FROM users WHERE email = ?"
    cursor.execute(query, (email,))  # SAFE!

    row = cursor.fetchone()
    if row:
        return {
            "id": row[0],
            "email": row[1],
            "name": row[2],
        }
    return None


def search_users_safe(search_term: str) -> List[dict]:
    """Safe LIKE query with parameterization."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    # LIKE with parameters - still safe
    # User can't escape the string context
    query = "SELECT * FROM users WHERE name LIKE ?"
    cursor.execute(query, (f"%{search_term}%",))

    return [
        {"id": row[0], "email": row[1], "name": row[2]}
        for row in cursor.fetchall()
    ]
```

### TypeScript
**Input validation**
```typescript
import validator from 'validator';

/**
 * Validate and sanitize user input.
 */
export class InputValidator {
  /**
   * Validate email address.
   */
  static validateEmail(email: string): string {
    email = email.trim().toLowerCase();

    if (!validator.isEmail(email)) {
      throw new Error(`Invalid email format: ${email}`);
    }

    if (email.length > 254) {
      throw new Error('Email address too long');
    }

    return email;
  }

  /**
   * Validate username (alphanumeric, hyphens, underscores).
   */
  static validateUsername(username: string): string {
    username = username.trim();

    if (!/^[a-zA-Z0-9_-]+$/.test(username)) {
      throw new Error(
        'Username can only contain letters, numbers, hyphens, and underscores'
      );
    }

    if (username.length < 3 || username.length > 50) {
      throw new Error('Username must be 3-50 characters');
    }

    return username;
  }

  /**
   * Sanitize HTML to prevent XSS.
   */
  static sanitizeHtml(html: string): string {
    // Use library like DOMPurify in browser or sanitize-html in Node
    return validator.escape(html);
  }
}
```

**XSS prevention**
```typescript
/**
 * Safely render user content to prevent XSS.
 */
export function renderUserContent(content: string): string {
  // Escape HTML entities
  return content
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;')
    .replace(/\//g, '&#x2F;');
}

/**
 * WRONG - XSS vulnerability
 */
function renderUnsafe(content: string): string {
  // Directly injecting user content into HTML!
  return `<div>${content}</div>`;  // UNSAFE!
}

/**
 * CORRECT - Escaped content
 */
function renderSafe(content: string): string {
  const escaped = renderUserContent(content);
  return `<div>${escaped}</div>`;  // SAFE!
}

/**
 * For React/JSX - framework handles escaping automatically
 */
function UserComment({ comment }: { comment: string }) {
  // React escapes by default - this is safe
  return <div>{comment}</div>;

  // dangerouslySetInnerHTML should be avoided
  // Only use with sanitized content from trusted library
}
```

**CSRF protection**
```typescript
import crypto from 'crypto';

/**
 * Generate CSRF token.
 */
export function generateCsrfToken(): string {
  return crypto.randomBytes(32).toString('hex');
}

/**
 * Validate CSRF token.
 */
export function validateCsrfToken(
  token: string,
  sessionToken: string
): boolean {
  // Constant-time comparison to prevent timing attacks
  return crypto.timingSafeEqual(
    Buffer.from(token),
    Buffer.from(sessionToken)
  );
}

/**
 * Express middleware for CSRF protection.
 */
export function csrfProtection(
  req: Request,
  res: Response,
  next: NextFunction
): void {
  // Generate token for GET requests
  if (req.method === 'GET') {
    req.session.csrfToken = generateCsrfToken();
    return next();
  }

  // Validate token for state-changing requests
  const token = req.body._csrf || req.headers['x-csrf-token'];
  const sessionToken = req.session.csrfToken;

  if (!token || !sessionToken || !validateCsrfToken(token, sessionToken)) {
    res.status(403).json({ error: 'Invalid CSRF token' });
    return;
  }

  next();
}
```

### Go
**Input validation**
```go
package security

import (
    "errors"
    "net/mail"
    "regexp"
    "strings"
)

var (
    // Safe characters for project names
    projectNameRegex = regexp.MustCompile(`^[a-zA-Z0-9_-]+$`)

    // Reserved names (Windows)
    reservedNames = map[string]bool{
        "con": true, "prn": true, "aux": true, "nul": true,
    }
)

// ValidateProjectName validates project name is safe for file system.
func ValidateProjectName(name string) error {
    name = strings.TrimSpace(name)

    if name == "" {
        return errors.New("project name cannot be empty")
    }

    if len(name) > 100 {
        return errors.New("project name too long (max 100 chars)")
    }

    if !projectNameRegex.MatchString(name) {
        return errors.New("project name can only contain letters, numbers, hyphens, underscores")
    }

    if strings.HasPrefix(name, "-") || strings.HasPrefix(name, "_") {
        return errors.New("project name cannot start with - or _")
    }

    if reservedNames[strings.ToLower(name)] {
        return errors.New("project name is reserved")
    }

    return nil
}

// ValidateEmail validates and normalizes email address.
func ValidateEmail(email string) (string, error) {
    email = strings.TrimSpace(strings.ToLower(email))

    if email == "" {
        return "", errors.New("email cannot be empty")
    }

    if len(email) > 254 {
        return "", errors.New("email too long")
    }

    // Use net/mail for validation
    addr, err := mail.ParseAddress(email)
    if err != nil {
        return "", fmt.Errorf("invalid email format: %w", err)
    }

    return addr.Address, nil
}
```

**Path traversal prevention**
```go
package security

import (
    "errors"
    "path/filepath"
    "strings"
)

// SafePathJoin joins user path to base directory safely.
func SafePathJoin(baseDir, userPath string) (string, error) {
    // Clean paths
    baseDir = filepath.Clean(baseDir)
    userPath = filepath.Clean(userPath)

    // Join and resolve
    target := filepath.Join(baseDir, userPath)

    // Ensure target is within base directory
    if !strings.HasPrefix(target, baseDir + string(filepath.Separator)) {
        return "", errors.New("path traversal detected")
    }

    return target, nil
}

// CreateFileSafely creates file with path traversal protection.
func CreateFileSafely(baseDir, filePath, content string) error {
    target, err := SafePathJoin(baseDir, filePath)
    if err != nil {
        return fmt.Errorf("invalid path: %w", err)
    }

    // Create parent directories
    dir := filepath.Dir(target)
    if err := os.MkdirAll(dir, 0755); err != nil {
        return fmt.Errorf("failed to create directory: %w", err)
    }

    // Write file
    if err := os.WriteFile(target, []byte(content), 0644); err != nil {
        return fmt.Errorf("failed to write file: %w", err)
    }

    return nil
}
```

**Command injection prevention**
```go
package security

import (
    "context"
    "errors"
    "os/exec"
    "strings"
    "time"
)

// RunGitCommand runs git command safely.
func RunGitCommand(ctx context.Context, args []string, dir string) (string, error) {
    // Validate arguments
    dangerousChars := []string{";", "|", "&", "$", "`", "\n", "\r"}
    for _, arg := range args {
        for _, char := range dangerousChars {
            if strings.Contains(arg, char) {
                return "", errors.New("argument contains dangerous characters")
            }
        }
    }

    // Build command - exec.Command does NOT use shell
    cmd := exec.CommandContext(ctx, "git", args...)
    cmd.Dir = dir

    // Run with timeout
    output, err := cmd.CombinedOutput()
    if err != nil {
        return "", fmt.Errorf("git command failed: %w", err)
    }

    return string(output), nil
}

// SafeGitClone clones repository safely.
func SafeGitClone(ctx context.Context, repoURL, targetDir string) error {
    // Validate URL format
    if !strings.HasPrefix(repoURL, "https://") &&
       !strings.HasPrefix(repoURL, "git://") &&
       !strings.HasPrefix(repoURL, "ssh://") {
        return errors.New("invalid repository URL")
    }

    // Run safely without shell
    ctx, cancel := context.WithTimeout(ctx, 5*time.Minute)
    defer cancel()

    _, err := RunGitCommand(ctx, []string{"clone", repoURL, targetDir}, ".")
    return err
}
```

### Rust
**Input validation**
```rust
use regex::Regex;
use std::error::Error;

lazy_static! {
    static ref PROJECT_NAME_REGEX: Regex =
        Regex::new(r"^[a-zA-Z0-9_-]+$").unwrap();
}

/// Validate project name is safe for file system.
pub fn validate_project_name(name: &str) -> Result<String, Box<dyn Error>> {
    let name = name.trim();

    if name.is_empty() {
        return Err("Project name cannot be empty".into());
    }

    if name.len() > 100 {
        return Err("Project name too long (max 100 chars)".into());
    }

    if !PROJECT_NAME_REGEX.is_match(name) {
        return Err(
            "Project name can only contain letters, numbers, hyphens, underscores".into()
        );
    }

    if name.starts_with('-') || name.starts_with('_') {
        return Err("Project name cannot start with - or _".into());
    }

    // Reserved names
    let reserved = ["con", "prn", "aux", "nul"];
    if reserved.contains(&name.to_lowercase().as_str()) {
        return Err("Project name is reserved".into());
    }

    Ok(name.to_string())
}

/// Validate email address format.
pub fn validate_email(email: &str) -> Result<String, Box<dyn Error>> {
    let email = email.trim().to_lowercase();

    if email.is_empty() {
        return Err("Email cannot be empty".into());
    }

    if email.len() > 254 {
        return Err("Email too long".into());
    }

    // Simple validation - for production use proper email validation crate
    if !email.contains('@') || !email.contains('.') {
        return Err("Invalid email format".into());
    }

    // Prevent email injection
    if email.contains('\n') || email.contains('\r') || email.contains('\0') {
        return Err("Email contains invalid characters".into());
    }

    Ok(email)
}
```

**Path traversal prevention**
```rust
use std::path::{Path, PathBuf};
use std::fs;
use std::io;

/// Safely join user path to base directory.
pub fn safe_path_join(
    base_dir: &Path,
    user_path: &str,
) -> Result<PathBuf, io::Error> {
    let base_dir = base_dir.canonicalize()?;
    let target = base_dir.join(user_path).canonicalize()?;

    // Ensure target is within base directory
    if !target.starts_with(&base_dir) {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "Path traversal detected",
        ));
    }

    Ok(target)
}

/// Create file safely with path traversal protection.
pub fn create_file_safely(
    base_dir: &Path,
    file_path: &str,
    content: &str,
) -> Result<(), io::Error> {
    let target = safe_path_join(base_dir, file_path)?;

    // Create parent directories
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)?;
    }

    // Write file
    fs::write(target, content)?;

    Ok(())
}
```

## Anti-patterns

### Hardcoded Secrets
**Bad:**
```python
API_KEY = "sk-ant-1234567890abcdef"  # NEVER DO THIS!
DATABASE_PASSWORD = "admin123"  # Committed to git!
```

**Good:**
```python
import keyring

API_KEY = keyring.get_password("sgsg", "claude_api_key")
DATABASE_PASSWORD = keyring.get_password("sgsg", "db_password")
```

### Trusting User Input
**Bad:**
```python
def delete_file(filename: str) -> None:
    # User could pass "../../etc/passwd"!
    os.remove(filename)
```

**Good:**
```python
def delete_file(filename: str, base_dir: Path) -> None:
    target = safe_path_join(base_dir, filename)
    target.unlink()
```

### Using `shell=True`
**Bad:**
```python
# Command injection vulnerability!
subprocess.run(f"git clone {user_repo}", shell=True)
```

**Good:**
```python
# No shell injection possible
subprocess.run(["git", "clone", user_repo], check=True)
```

### String Concatenation for SQL
**Bad:**
```python
# SQL injection vulnerability!
query = f"SELECT * FROM users WHERE email = '{email}'"
cursor.execute(query)
```

**Good:**
```python
# Parameterized query - safe
query = "SELECT * FROM users WHERE email = ?"
cursor.execute(query, (email,))
```

### Weak Random Number Generation
**Bad:**
```python
import random

# Predictable - NOT suitable for security
session_id = random.randint(0, 1000000)
```

**Good:**
```python
import secrets

# Cryptographically secure
session_id = secrets.token_hex(32)
```

## Examples

### Example 1: Comprehensive Input Validation
```python
from typing import Optional
import re
from pathlib import Path

class InputValidator:
    """Validate user inputs for security."""

    # Regex patterns
    PROJECT_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')
    EMAIL_PATTERN = re.compile(r'^[^@]+@[^@]+\.[^@]+$')

    # Reserved names
    RESERVED_NAMES = {
        'con', 'prn', 'aux', 'nul',
        'com1', 'com2', 'lpt1', 'lpt2',
    }

    @staticmethod
    def validate_project_name(name: str) -> str:
        """Validate project name for file system safety.

        Args:
            name: Project name to validate

        Returns:
            Validated and normalized project name

        Raises:
            ValueError: If name is invalid
        """
        name = name.strip()

        if not name:
            raise ValueError("Project name cannot be empty")

        if len(name) > 100:
            raise ValueError(
                f"Project name too long: {len(name)} chars (max 100)"
            )

        if not InputValidator.PROJECT_NAME_PATTERN.match(name):
            raise ValueError(
                f"Invalid project name: {name!r}. "
                f"Only letters, numbers, hyphens, and underscores allowed."
            )

        if name[0] in ('-', '_'):
            raise ValueError(
                f"Project name cannot start with {name[0]!r}"
            )

        if name.lower() in InputValidator.RESERVED_NAMES:
            raise ValueError(
                f"Project name {name!r} is reserved"
            )

        return name

    @staticmethod
    def validate_email(email: str) -> str:
        """Validate email address.

        Args:
            email: Email to validate

        Returns:
            Normalized email (lowercase)

        Raises:
            ValueError: If email is invalid
        """
        email = email.strip().lower()

        if not email:
            raise ValueError("Email cannot be empty")

        if len(email) > 254:  # RFC 5321
            raise ValueError("Email address too long")

        if not InputValidator.EMAIL_PATTERN.match(email):
            raise ValueError(f"Invalid email format: {email!r}")

        # Prevent email injection
        if any(c in email for c in ['\n', '\r', '\0', ';']):
            raise ValueError("Email contains invalid characters")

        return email

    @staticmethod
    def validate_url(url: str, allowed_schemes: Optional[list[str]] = None) -> str:
        """Validate URL format and scheme.

        Args:
            url: URL to validate
            allowed_schemes: List of allowed schemes (default: https, git, ssh)

        Returns:
            Validated URL

        Raises:
            ValueError: If URL is invalid
        """
        if allowed_schemes is None:
            allowed_schemes = ['https', 'git', 'ssh']

        url = url.strip()

        if not url:
            raise ValueError("URL cannot be empty")

        # Check scheme
        if not any(url.startswith(f"{scheme}://") for scheme in allowed_schemes):
            raise ValueError(
                f"Invalid URL scheme. Allowed: {', '.join(allowed_schemes)}"
            )

        # Prevent SSRF attacks - disallow localhost/private IPs
        dangerous_hosts = [
            'localhost',
            '127.0.0.1',
            '0.0.0.0',
            '[::]',
            '169.254',  # Link-local
            '10.',      # Private
            '172.16.',  # Private
            '192.168.', # Private
        ]

        url_lower = url.lower()
        if any(host in url_lower for host in dangerous_hosts):
            raise ValueError(
                "URLs pointing to localhost or private IPs are not allowed"
            )

        return url
```

### Example 2: Secure File Operations
```python
from pathlib import Path
import tempfile
import shutil
from typing import Callable

class SecureFileManager:
    """Manage files with security controls."""

    def __init__(self, base_dir: Path) -> None:
        """Initialize with base directory.

        Args:
            base_dir: Base directory for all operations
        """
        self.base_dir = base_dir.resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _validate_path(self, file_path: str) -> Path:
        """Validate path is within base directory.

        Args:
            file_path: User-provided file path

        Returns:
            Resolved absolute path

        Raises:
            ValueError: If path traversal detected
        """
        target = (self.base_dir / file_path).resolve()

        try:
            target.relative_to(self.base_dir)
        except ValueError:
            raise ValueError(
                f"Path traversal detected: {file_path!r} "
                f"points outside {self.base_dir}"
            ) from None

        return target

    def write_file(self, file_path: str, content: str) -> None:
        """Write file with atomic write and path validation.

        Args:
            file_path: Relative path within base directory
            content: File content

        Raises:
            ValueError: If path validation fails
        """
        target = self._validate_path(file_path)

        # Create parent directories
        target.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write using temporary file
        temp_fd, temp_path = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
        )

        try:
            # Write to temporary file
            with open(temp_fd, 'w', encoding='utf-8') as f:
                f.write(content)

            # Atomic rename
            Path(temp_path).replace(target)
        except Exception:
            # Clean up temporary file on error
            Path(temp_path).unlink(missing_ok=True)
            raise

    def read_file(self, file_path: str) -> str:
        """Read file with path validation.

        Args:
            file_path: Relative path within base directory

        Returns:
            File contents

        Raises:
            ValueError: If path validation fails
            FileNotFoundError: If file doesn't exist
        """
        target = self._validate_path(file_path)

        if not target.exists():
            raise FileNotFoundError(
                f"File not found: {file_path}"
            )

        return target.read_text(encoding='utf-8')

    def delete_file(self, file_path: str) -> None:
        """Delete file with path validation.

        Args:
            file_path: Relative path within base directory

        Raises:
            ValueError: If path validation fails
        """
        target = self._validate_path(file_path)
        target.unlink(missing_ok=True)

    def process_file_safely(
        self,
        input_path: str,
        output_path: str,
        processor: Callable[[str], str],
    ) -> None:
        """Process file with validation and error recovery.

        Args:
            input_path: Input file (relative path)
            output_path: Output file (relative path)
            processor: Function to process content

        Raises:
            ValueError: If path validation fails
            FileNotFoundError: If input file doesn't exist
        """
        # Read input with validation
        content = self.read_file(input_path)

        # Process content
        try:
            processed = processor(content)
        except Exception as e:
            raise ValueError(
                f"Failed to process {input_path}: {e}"
            ) from e

        # Write output with atomic write
        self.write_file(output_path, processed)
```

### Example 3: Secure API Client
```python
import requests
from typing import Optional, Dict, Any
import hashlib
import hmac
import time

class SecureAPIClient:
    """API client with security best practices."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: Optional[str] = None,
    ) -> None:
        """Initialize API client.

        Args:
            base_url: API base URL
            api_key: API key for authentication
            api_secret: API secret for request signing (optional)
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.api_secret = api_secret

        # Create session with security settings
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'SGSG/1.0',
            'X-API-Key': self.api_key,
        })

    def _sign_request(
        self,
        method: str,
        path: str,
        body: Optional[str] = None,
    ) -> str:
        """Generate HMAC signature for request.

        Args:
            method: HTTP method
            path: Request path
            body: Request body (for POST/PUT)

        Returns:
            HMAC signature
        """
        if not self.api_secret:
            return ""

        timestamp = str(int(time.time()))
        message = f"{method}{path}{timestamp}"

        if body:
            message += body

        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

        return f"{timestamp}:{signature}"

    def request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """Make authenticated API request.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: API endpoint path
            data: Request data (for POST/PUT)
            timeout: Request timeout in seconds

        Returns:
            Response data

        Raises:
            requests.HTTPError: If request fails
        """
        url = f"{self.base_url}/{path.lstrip('/')}"

        # Prepare request
        headers = {}
        body = None

        if data and method in ('POST', 'PUT', 'PATCH'):
            body = json.dumps(data)
            headers['Content-Type'] = 'application/json'

            # Sign request if secret provided
            if self.api_secret:
                headers['X-Signature'] = self._sign_request(method, path, body)

        # Make request with timeout and security settings
        response = self.session.request(
            method=method,
            url=url,
            data=body,
            headers=headers,
            timeout=timeout,
            # Security settings
            verify=True,  # Verify SSL certificates
            allow_redirects=False,  # Don't follow redirects
        )

        # Check response
        response.raise_for_status()

        # Validate content type
        content_type = response.headers.get('Content-Type', '')
        if 'application/json' not in content_type:
            raise ValueError(
                f"Unexpected content type: {content_type}"
            )

        return response.json()
```

## Security Checklist

Before deploying code:

- [ ] All user inputs validated at boundaries
- [ ] No SQL injection vulnerabilities (use parameterized queries)
- [ ] No command injection vulnerabilities (avoid `shell=True`)
- [ ] No path traversal vulnerabilities (validate file paths)
- [ ] No XSS vulnerabilities (escape HTML)
- [ ] API keys stored in OS keyring, not code
- [ ] Secrets not committed to git
- [ ] HTTPS used for all external requests
- [ ] SSL certificates validated
- [ ] CSRF protection enabled for state-changing requests
- [ ] Authentication and authorization implemented
- [ ] Rate limiting on public endpoints
- [ ] Error messages don't leak sensitive data
- [ ] Logging doesn't include secrets or PII
- [ ] Dependencies scanned for vulnerabilities
- [ ] Security headers configured (CSP, HSTS, etc.)
