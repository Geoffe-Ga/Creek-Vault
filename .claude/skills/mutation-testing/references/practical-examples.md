# Mutation Testing Practical Examples

## Example 1: Password Validation

```python
# Code
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password too short. Minimum {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password too long. Maximum {MAX_PASSWORD_LENGTH} characters.")

# HIGH-VALUE Tests
def test_password_min_length_exact():
    """Kills: MIN_PASSWORD_LENGTH = 8 -> 7, 9"""
    assert MIN_PASSWORD_LENGTH == 8
    validate_password("a" * 8)  # Should pass
    with pytest.raises(ValueError, match="Password too short"):
        validate_password("a" * 7)  # Should fail

def test_password_max_length_exact():
    """Kills: MAX_PASSWORD_LENGTH = 128 -> 127, 129"""
    assert MAX_PASSWORD_LENGTH == 128
    validate_password("a" * 128)  # Should pass
    with pytest.raises(ValueError, match="Password too long"):
        validate_password("a" * 129)  # Should fail

def test_password_error_messages_exact():
    """Kills: error message string mutations"""
    with pytest.raises(ValueError) as exc:
        validate_password("short")
    error = str(exc.value)
    assert "Password too short" in error
    assert "8 characters" in error
```

## Example 2: State Machine

```python
# Code
class OrderStateMachine:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"

    def __init__(self):
        self.state = self.PENDING

    def start_processing(self):
        if self.state != self.PENDING:
            raise ValueError(f"Cannot process order in state: {self.state}")
        self.state = self.PROCESSING

    def complete(self):
        if self.state != self.PROCESSING:
            raise ValueError(f"Cannot complete order in state: {self.state}")
        self.state = self.COMPLETED

# HIGH-VALUE Tests
def test_state_constants_exact():
    """Kills: state string mutations"""
    assert OrderStateMachine.PENDING == "pending"
    assert OrderStateMachine.PROCESSING == "processing"
    assert OrderStateMachine.COMPLETED == "completed"

def test_initial_state_exact():
    """Kills: wrong initial state"""
    order = OrderStateMachine()
    assert order.state == OrderStateMachine.PENDING
    assert order.state != OrderStateMachine.PROCESSING

def test_state_transition_exact():
    """Kills: missing state updates, wrong transitions"""
    order = OrderStateMachine()
    order.start_processing()
    assert order.state == OrderStateMachine.PROCESSING
    assert order.state != OrderStateMachine.PENDING
    order.complete()
    assert order.state == OrderStateMachine.COMPLETED

def test_invalid_transitions_blocked():
    """Kills: missing validation checks"""
    order = OrderStateMachine()
    with pytest.raises(ValueError, match="Cannot complete order in state: pending"):
        order.complete()
    assert order.state == OrderStateMachine.PENDING  # State unchanged
```

## Common Mutation Categories

| Mutation Type | How to Kill It |
|---------------|----------------|
| Constant Mutations | Test exact value with equality assertions |
| Operator Mutations | Test boundary cases and edge values |
| Boolean Mutations | Test both True and False branches explicitly |
| String Mutations | Test exact string contents with equality |
| Collection Mutations | Test exact size, order, and contents |
| None Mutations | Test both None and not-None cases |
| Return Mutations | Test exact return values, not just type |

## Red Flags (Tests That Don't Kill Mutants)

- `assert result` - truthy test misses specific values
- `assert function() is not None` - doesn't test what the value IS
- `assert len(result) > 0` - doesn't test exact size or contents
- `assert "error" in output` - weak string matching
- Tests with no assertions - coverage without verification
