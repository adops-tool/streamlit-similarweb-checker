"""Normalization of user-supplied URLs into bare, lookup-ready domains."""

from __future__ import annotations

import re

_SCHEME_RE = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:)?//")
_PATH_START_RE = re.compile(r"[/\\?#]")
# Both prefixes are dropped only while a registrable name remains, so that
# "www.com" and "m.com" survive intact.
_WWW_RE = re.compile(r"^www\d*\.(?=[^.]+\.)")
_MOBILE_RE = re.compile(r"^(?:m|mobile)\.(?=[^.]+\.)")
_HOSTNAME_RE = re.compile(
    r"(?=.{4,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})"
)


def normalize_domain(value: object, *, strip_mobile_prefix: bool = True) -> str | None:
    """Reduce a raw URL or hostname to a canonical domain.

    Scheme, credentials, port, path, query, fragment, ``www``/``wwwN`` and a
    trailing dot are removed; internationalized names are converted to
    punycode. ``m.``/``mobile.`` prefixes are folded into the parent domain
    unless ``strip_mobile_prefix`` is disabled, because SimilarWeb reports
    them separately while outreach lists usually want the main site.

    Returns ``None`` when the input is not a syntactically valid public
    hostname (empty cells, IP addresses, single-label names, free text), so
    callers can skip it instead of spending an API request on it.
    """
    if value is None:
        return None

    host = str(value).strip().strip("\"'").lower()
    host = _SCHEME_RE.sub("", host, count=1)
    host = _PATH_START_RE.split(host, maxsplit=1)[0]
    host = host.rpartition("@")[2]
    host = host.partition(":")[0]
    host = host.strip(".")
    host = _WWW_RE.sub("", host)
    if strip_mobile_prefix:
        host = _MOBILE_RE.sub("", host)

    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None

    return host if _HOSTNAME_RE.fullmatch(host) else None
