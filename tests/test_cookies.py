import http.cookiejar
from pathlib import Path

from bot.services.chrome_cookies import (
    format_netscape_cookies,
    has_authenticated_cookies,
    write_cookies_atomically,
)


def test_format_netscape_cookies_roundtrip(tmp_path: Path) -> None:
    cookies = [
        {
            "domain": ".youtube.com",
            "name": "SAPISID",
            "value": "secret_value_123",
            "path": "/",
            "secure": True,
            "expires": 1900000000.0,
        },
        {
            "domain": "accounts.google.com",
            "name": "SESSION_ONLY",
            "value": "abc\tdef\n",
            "path": "/",
            "secure": False,
            "expires": -1,
        },
        {
            "domain": ".example.org",
            "name": "UNRELATED",
            "value": "should_be_filtered",
            "path": "/",
            "secure": False,
            "expires": 1900000000,
        },
    ]

    content = format_netscape_cookies(cookies)
    assert "UNRELATED" not in content
    assert ".youtube.com\tTRUE\t/\tTRUE\t1900000000\tSAPISID\tsecret_value_123" in content
    assert "accounts.google.com\tFALSE\t/\tFALSE\t0\tSESSION_ONLY\tabcdef" in content

    cookie_path = tmp_path / "youtube.txt"
    write_cookies_atomically(cookie_path, content)
    assert cookie_path.exists()
    assert list(tmp_path.iterdir()) == [cookie_path]

    jar = http.cookiejar.MozillaCookieJar()
    jar.load(str(cookie_path), ignore_discard=True, ignore_expires=True)
    names = {c.name for c in jar}
    assert names == {"SAPISID", "SESSION_ONLY"}


def test_has_authenticated_cookies() -> None:
    logged_out = [
        {"domain": ".youtube.com", "name": "VISITOR_INFO1_LIVE", "value": "xyz"},
        {"domain": ".youtube.com", "name": "PREF", "value": "tz=UTC"},
    ]
    assert has_authenticated_cookies(logged_out) is False

    logged_in = [
        {"domain": ".youtube.com", "name": "SID", "value": "111"},
        {"domain": ".youtube.com", "name": "__Secure-3PSID", "value": "222"},
    ]
    assert has_authenticated_cookies(logged_in) is True
