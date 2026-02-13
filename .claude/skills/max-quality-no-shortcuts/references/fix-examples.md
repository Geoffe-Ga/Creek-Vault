# Fix Examples: Root Cause vs Bypass

## Example 1: Complexity Too High (C901)

### Bypass (BAD)
```python
def process_order(order, user, payment, shipping):  # noqa: C901
    if order.status == "pending":
        if user.is_verified:
            if payment.is_valid:
                if shipping.is_available:
                    # 30 more lines...
```

### Proper Fix (GOOD)
```python
def process_order(order: Order, user: User, payment: Payment, shipping: Shipping) -> Result:
    """Process order through validation and fulfillment pipeline."""
    _validate_order_ready(order)
    _validate_user_eligible(user)
    _validate_payment(payment)
    _validate_shipping(shipping)
    return _fulfill_order(order, user, payment, shipping)

def _validate_order_ready(order: Order) -> None:
    if order.status != "pending":
        raise OrderError(f"Cannot process order with status: {order.status}")

def _validate_user_eligible(user: User) -> None:
    if not user.is_verified:
        raise UserError("User must be verified to place orders")
```

**Result**: Each function has complexity <= 3. Clear, testable units.

## Example 2: Type Errors

### Bypass (BAD)
```python
def get_config(key: str) -> str:
    config = load_config()
    return config.get(key)  # type: ignore[return-value]
```

### Proper Fix (GOOD)
```python
def get_config(key: str) -> str | None:
    """Get config value. Returns None if not found."""
    return load_config().get(key)

# Or for required keys:
def get_required_config(key: str) -> str:
    """Get required config value. Raises if not found."""
    value = load_config().get(key)
    if value is None:
        raise ConfigError(f"Required config key not found: {key}")
    return value
```

**Result**: Types accurately reflect reality. Explicit error handling.

## Example 3: Too Many Arguments

### Bypass (BAD)
```python
def create_user(  # pylint: disable=too-many-arguments
    username: str, email: str, password: str,
    first_name: str, last_name: str, phone: str,
    address: str, city: str, country: str,
) -> User:
    pass
```

### Proper Fix (GOOD)
```python
@dataclass
class UserRegistration:
    """User registration data."""
    username: str
    email: str
    password: str
    first_name: str
    last_name: str
    phone: str
    address: str
    city: str
    country: str

def create_user(registration: UserRegistration) -> User:
    """Create new user from registration data."""
    _validate_registration(registration)
    return User.from_registration(registration)
```

**Result**: Clean signature, grouped data, easy to extend.

## The Rare Legitimate Bypass

```python
# pylint: disable=no-member
# Reason: boto3 dynamically creates methods via __getattr__
# Reference: https://github.com/boto/boto3/issues/123
# Alternative considered: Custom wrapper (adds unnecessary complexity)
# Reviewed: 2026-01-27
# Review again: 2026-06-27 (when boto3 v2.0 releases)
response = client.get_item(TableName="users", Key={"id": user_id})
```

Requirements for legitimate bypasses:
- Explicit reason why bypass is necessary
- Link to external issue/documentation
- Alternatives considered
- Review date for reconsideration
- Targeted (specific line, not blanket disable)
