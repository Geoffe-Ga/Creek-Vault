"""Tests for creek_vault.main module."""

from creek_vault.main import main


def test_main_runs() -> None:
    """Test that main() runs without error."""
    main()  # Should print "Hello from creek-vault!"
