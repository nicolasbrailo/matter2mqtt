class RequestError(Exception):
    """Something the caller got wrong: a malformed payload, an unknown device, a value out of
    range, a busy resource. Layers below the bridge raise it instead of reporting errors
    themselves; the bridge turns it into an {"error": ...} message on mqtt.
    """


def describe(e):
    """What the client sees: a RequestError is its own message (the caller's mistake, stated
    plainly); anything else keeps its type, because it's a bug or an SDK failure."""
    return str(e) if isinstance(e, RequestError) else repr(e)
