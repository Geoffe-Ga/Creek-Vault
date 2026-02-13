"""Tests for creek.main module."""

from unittest.mock import patch

from creek.main import main


def test_main_runs() -> None:
    """Test that main() calls the CLI app."""
    with patch("creek.main.app") as mock_app:
        main()
        mock_app.assert_called_once()
