"""The one error type the domain functions raise for a bad request."""


class DomainError(ValueError):
    """A request the domain rules refuse: bad input, not found, or not allowed.

    The message says what is wrong in words a user can act on, so the API can
    return it as a 400. A ValueError subclass, so callers that only care that
    the input was invalid can catch ValueError.
    """
