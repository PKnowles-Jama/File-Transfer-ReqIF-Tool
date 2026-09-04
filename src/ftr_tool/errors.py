class JamaError(RuntimeError):
    """A human-readable Jama REST error."""


class ValidationError(ValueError):
    """Invalid user input or configuration file."""

