"""Bind typographically equivalent quotations to actual unchanged source bytes."""


def actual_source_quote(quote: str, source: str) -> str | None:
    if not quote or not source:
        return None
    if quote in source:
        return quote
    # Model line wrapping and a sentence-ending period are presentation. Do
    # not remove internal punctuation, words, ellipses, or negation.
    wanted = ''.join(c for c in quote if not c.isspace()).rstrip('。.')
    if not wanted:
        return None
    positions = [i for i, c in enumerate(source) if not c.isspace()]
    compact = ''.join(source[i] for i in positions)
    start = compact.find(wanted)
    if start < 0:
        return None
    return source[positions[start]:positions[start + len(wanted) - 1] + 1]
