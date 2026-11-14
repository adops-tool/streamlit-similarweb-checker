import pytest

from traffic_checker.domains import normalize_domain


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "example.com"),
        ("  Example.COM  ", "example.com"),
        ("https://www.example.com/path?a=1#fragment", "example.com"),
        ("http://example.com:8080/", "example.com"),
        ("//cdn.example.com/lib.js", "cdn.example.com"),
        ("ftp://user:secret@files.example.org/pub", "files.example.org"),
        ("www2.example.com", "example.com"),
        ("example.com.", "example.com"),
        ("example.com?utm_source=x", "example.com"),
        ("example.com#top", "example.com"),
        ("example.com\\path", "example.com"),
        ('"example.com"', "example.com"),
        ("m.example.com", "example.com"),
        ("mobile.example.com", "example.com"),
        ("www.m.example.com", "example.com"),
        ("m.news.bbc.co.uk:443/world", "news.bbc.co.uk"),
        ("sub.domain.example.co.uk", "sub.domain.example.co.uk"),
        ("bücher.de", "xn--bcher-kva.de"),
        ("https://пример.рф/страница", "xn--e1afmkfd.xn--p1ai"),
    ],
)
def test_normalizes_to_bare_domain(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "nan",
        float("nan"),
        "localhost",
        "just some text",
        "192.168.0.1",
        "http://[::1]:8080/",
        "exa mple.com",
        "exa_mple.com",
        "-example.com",
        "example-.com",
        "example..com",
        "example.c",
        '=HYPERLINK("http://evil.test")',
        "a" * 64 + ".com",
        ".".join(["a" * 60] * 5) + ".com",
    ],
)
def test_rejects_non_domains(raw):
    assert normalize_domain(raw) is None


@pytest.mark.parametrize("raw", ["www.com", "m.com", "mobile.de"])
def test_prefix_is_kept_when_it_is_the_registrable_name(raw):
    assert normalize_domain(raw) == raw


def test_mobile_prefix_can_be_preserved():
    assert normalize_domain("https://m.example.com/", strip_mobile_prefix=False) == "m.example.com"
    assert normalize_domain("www.example.com", strip_mobile_prefix=False) == "example.com"
