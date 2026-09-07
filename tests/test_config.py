"""Tests for typed settings validation.

These cover the production-only validators, which the endpoint tests never
reach because the test client runs in the `local` environment.
"""

import pytest
from pydantic import ValidationError

from app_assignment_1.config import Settings


def test_production_requires_api_key():
    """Production without APP_API_KEY fails at construction, not at request time."""
    with pytest.raises(ValidationError, match="APP_API_KEY must be set"):
        Settings(environment="production", api_key="")


def test_production_forbids_debug():
    """Debug mode in production is rejected so internals never leak."""
    with pytest.raises(ValidationError, match="APP_DEBUG must be false"):
        Settings(environment="production", debug=True, api_key="a-secret")


def test_port_out_of_range_rejected():
    """A port outside 1-65535 fails the field constraint."""
    with pytest.raises(ValidationError):
        Settings(port=99999)


def test_unknown_key_rejected():
    """A typo'd setting is a mistake, not something to silently ignore."""
    with pytest.raises(ValidationError):
        Settings(prot=8000)
