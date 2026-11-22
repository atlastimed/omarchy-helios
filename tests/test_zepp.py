import gzip
import json
import stat
import urllib.parse
from typing import ClassVar

import pytest

from helios import config, zepp

EMAIL, PASSWORD = "wearer+tag@example.com", "p@ss wörd/&=?#%+'\"🙂"
REDIRECT = (
    "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html"
    "?access=ACCESS123&refresh=REFRESH456&country_code=US"
)
KEY = "ab" * 16
# The sign-in form for EMAIL and PASSWORD as huami-token (commit 1b326585)
# encrypts it with PyCryptodome: Helios must send the same bytes.
GOLDEN = (
    "fbe0b963e25d87e7074eac5f89d69509d7cef8ee068b97d5611f08d94ffbf7ce8384e79cc32268e9f179b2e3d3949660"
    "b4f096f6c96d19b83f2369bf19d085c2c18637e6b89523ab51fa21a2f3498fba586b2e2a8e77e0846147a1deb8adaa76"
    "9c1a9e0d6a4253fde5dc26d1da6d301695f317d40c9eeac9d47aa538066a5dacd1ee061104d3463a4a5696e8b3755bcb"
    "c44a623169aaee796f0689075a7ef697b945afd8e5a652f927c25e93caeb4a09e595f8936553ccd64647892f7b7e7b7b"
    "8fc56546fd499189e3afae08e9d04529d10068e17568bd27823c187d1490dc5484e3df5035c5b59492b2a9c74b0cb3d6"
    "8239dfc092a8ca297de95cbd735659907cfee518c3d0cc76972043d513d6e275de6158dda3e414d72eb5d167c89db355"
    "1062ce4b7d4b32bc6b4f90e5f90c2711"
)


def item(address, active, key=KEY):
    return {"macAddress": address, "activeStatus": int(active), "additionalInfo": json.dumps({"auth_key": key})}


class Zepp:
    """Stands in for Zepp's servers: records each request, answers as they do."""

    def __init__(self, items=None, tokens=None, login=None, devices=None):
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.items = [item("AA:BB:CC:DD:EE:02", True)] if items is None else items
        self.tokens = tokens or zepp.Response(303, {"location": REDIRECT}, b"")
        session = {"token_info": {"login_token": "LOGIN", "app_token": "APP", "user_id": "7012345678"}}
        self.login = login or zepp.Response(200, {}, json.dumps(session).encode())
        self.devices = devices
        self.signed_out = False

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        if url == zepp.TOKENS_URL:
            return self.tokens
        if url == zepp.LOGIN_URL:
            return self.login
        if url == zepp.LOGOUT_URL:
            self.signed_out = True
            return zepp.Response(200, {}, b'{"result": "ok"}')
        return self.devices or zepp.Response(200, {}, json.dumps({"items": self.items}).encode())


def test_the_sign_in_form_is_what_the_app_sends():
    server = Zepp()
    zepp.fetch_devices(EMAIL, PASSWORD, server)
    method, url, headers, body = server.calls[0]
    assert (method, url) == ("POST", zepp.TOKENS_URL)
    assert body.hex() == GOLDEN
    assert headers["x-hm-ekv"] == "1" and headers["user-agent"].startswith("Zepp/")


def test_four_requests_fetch_the_key_and_sign_out():
    server = Zepp([item("aa:bb:cc:dd:ee:01", False, "11" * 16), item("AA:BB:CC:DD:EE:02", True, "0x" + KEY.upper())])
    found = zepp.fetch_devices(EMAIL, PASSWORD, server)
    assert found == [
        zepp.Device("AA:BB:CC:DD:EE:02", True, KEY),  # the active pairing first
        zepp.Device("AA:BB:CC:DD:EE:01", False, "11" * 16),
    ]
    assert [(m, urllib.parse.urlsplit(u).path) for m, u, _h, _b in server.calls] == [
        ("POST", "/v2/registrations/tokens"),
        ("POST", "/v2/client/login"),
        ("GET", "/users/7012345678/devices"),
        ("POST", "/v1/client/logout"),
    ]
    assert all(urllib.parse.urlsplit(u).scheme == "https" for _m, u, _h, _b in server.calls)
    login = dict(urllib.parse.parse_qsl(server.calls[1][3].decode()))
    assert login["code"] == "ACCESS123" and login["grant_type"] == "access_token"
    _m, url, headers, body = server.calls[2]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["userid"] == ["7012345678"] and len(query["r"]) == 2 and headers["apptoken"] == "APP" and body is None
    assert dict(urllib.parse.parse_qsl(server.calls[3][3].decode()))["login_token"] == "LOGIN"


@pytest.mark.parametrize(
    "answer",
    [
        zepp.Response(400, {}, b'{"error": "bad"}'),
        zepp.Response(200, {}, b""),
        zepp.Response(303, {}, b""),  # a redirect with nowhere to go
        zepp.Response(303, {"location": "https://example.com/?refresh=only"}, b""),
    ],
)
def test_a_refused_sign_in_says_so_and_goes_no_further(answer):
    server = Zepp(tokens=answer)
    with pytest.raises(zepp.ZeppError, match="didn't accept that email and password"):
        zepp.fetch_devices(EMAIL, PASSWORD, server)
    assert len(server.calls) == 1


def test_too_many_attempts_is_told_apart():
    with pytest.raises(zepp.ZeppError, match="Try again later"):
        zepp.fetch_devices(EMAIL, PASSWORD, Zepp(tokens=zepp.Response(429, {}, b"")))


@pytest.mark.parametrize(
    "answer",
    [
        zepp.Response(500, {}, b"oops"),
        zepp.Response(200, {}, b"<html>not json</html>"),
        zepp.Response(200, {}, b"[1, 2]"),
        zepp.Response(200, {}, b'{"token_info": "none"}'),
        zepp.Response(200, {}, b'{"token_info": {"app_token": "A", "user_id": "1"}}'),
    ],
)
def test_a_second_step_that_goes_wrong_is_an_error_in_a_sentence(answer):
    with pytest.raises(zepp.ZeppError):
        zepp.fetch_devices(EMAIL, PASSWORD, Zepp(login=answer))


def test_the_session_is_ended_even_when_listing_devices_fails():
    server = Zepp(devices=zepp.Response(503, {}, b""))
    with pytest.raises(zepp.ZeppError, match="wouldn't list"):
        zepp.fetch_devices(EMAIL, PASSWORD, server)
    assert server.signed_out


def test_a_failed_sign_out_does_not_lose_the_key():
    class NoSignOut(Zepp):
        def __call__(self, method, url, headers, body):
            if url == zepp.LOGOUT_URL:
                raise zepp.ZeppError("Couldn't reach Zepp")
            return super().__call__(method, url, headers, body)

    assert zepp.fetch_devices(EMAIL, PASSWORD, NoSignOut())[0].key == KEY


def test_odd_device_entries_are_passed_over():
    items = [
        "not a device",
        {"macAddress": "not an address", "additionalInfo": "{}"},
        {"macAddress": "AA:BB:CC:DD:EE:03", "activeStatus": 1, "additionalInfo": "not json"},
        {"macAddress": "AA:BB:CC:DD:EE:04", "activeStatus": 0, "additionalInfo": json.dumps({"auth_key": "short"})},
        {"macAddress": "AA:BB:CC:DD:EE:05", "activeStatus": 0},
        item("AA:BB:CC:DD:EE:06", False),
    ]
    found = zepp.fetch_devices(EMAIL, PASSWORD, Zepp(items))
    assert [(d.address[-2:], d.key) for d in found] == [("03", ""), ("04", ""), ("05", ""), ("06", KEY)]
    assert zepp.fetch_devices(EMAIL, PASSWORD, Zepp(devices=zepp.Response(200, {}, b'{"items": "none"}'))) == []


def test_an_address_paired_twice_keeps_the_active_pairing():
    items = [item("AA:BB:CC:DD:EE:02", False, "11" * 16), item("AA:BB:CC:DD:EE:02", True, KEY)]
    assert zepp.fetch_devices(EMAIL, PASSWORD, Zepp(items)) == [zepp.Device("AA:BB:CC:DD:EE:02", True, KEY)]


def test_choosing_the_strap():
    one = zepp.Device("AA:BB:CC:DD:EE:01", False, "11" * 16)
    two = zepp.Device("AA:BB:CC:DD:EE:02", True, KEY)
    three = zepp.Device("AA:BB:CC:DD:EE:03", True, "22" * 16)
    assert zepp.choose([one], None) == one  # the only device
    assert zepp.choose([two, one], None) == two  # the only active one
    assert zepp.choose([two, one], "aa:bb:cc:dd:ee:01") == one  # the one asked for
    with pytest.raises(zepp.ZeppError, match="several devices"):
        zepp.choose([two, three, one], None)
    # What this computer's Bluetooth has seen settles a tie, and only a tie.
    assert zepp.choose([two, three, one], None, nearby=("aa:bb:cc:dd:ee:03",)) == three
    assert zepp.choose([two, one], None, nearby=("AA:BB:CC:DD:EE:01",)) == two  # an old strap, still remembered
    assert zepp.choose([one], None, nearby=("AA:BB:CC:DD:EE:99",)) == one  # a neighbour's strap
    with pytest.raises(zepp.ZeppError, match="several devices"):
        zepp.choose([two, three], None, nearby=("AA:BB:CC:DD:EE:02", "AA:BB:CC:DD:EE:03"))
    with pytest.raises(zepp.ZeppError, match="No device with address"):
        zepp.choose([two, one], "AA:BB:CC:DD:EE:99")
    with pytest.raises(zepp.ZeppError, match="no paired devices"):
        zepp.choose([], None)
    with pytest.raises(zepp.ZeppError, match="holds no key"):
        zepp.choose([zepp.Device("AA:BB:CC:DD:EE:04", True, "")], None)


class FakeConnection:
    """What http.client would be handed, with a canned reply."""

    made: ClassVar[list] = []
    reply: ClassVar[tuple] = (200, {}, b"{}")

    def __init__(self, host, port, timeout, context):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.request: list = []
        FakeConnection.made.append(self)

    def set_tunnel(self, host, port=None, headers=None):
        self.request.append(("tunnel", host, port, headers))

    def putrequest(self, method, target, skip_accept_encoding=False):
        self.request.append((method, target, skip_accept_encoding))

    def putheader(self, name, value):
        self.request.append((name, value))

    def endheaders(self, body):
        self.request.append(("body", body))

    def getresponse(self):
        status, headers, body = FakeConnection.reply

        class Reply:
            def read(self, limit):
                return body[:limit]

            def getheader(self, name):
                return {key.lower(): value for key, value in headers.items()}.get(name.lower())

            def getheaders(self):
                return list(headers.items())

        reply = Reply()
        reply.status = status
        return reply

    def close(self):
        self.request.append("closed")


@pytest.fixture
def connection(monkeypatch):
    FakeConnection.made = []
    monkeypatch.setattr(zepp.http.client, "HTTPSConnection", FakeConnection)
    return FakeConnection


def test_requests_go_out_over_verified_https(connection):
    connection.reply = (303, {"Location": REDIRECT, "Content-Encoding": "identity"}, b"")
    response = zepp.https("POST", zepp.TOKENS_URL, {"user-agent": "x", "accept-encoding": "gzip"}, b"body")
    (made,) = connection.made
    assert (made.host, made.port, made.timeout) == ("api-user-us2.zepp.com", 443, zepp.TIMEOUT)
    # The default context: certificates and host names checked against the system's store.
    assert made.context.verify_mode == zepp.ssl.CERT_REQUIRED and made.context.check_hostname
    assert made.request[0] == ("POST", "/v2/registrations/tokens", True)
    assert ("content-length", "4") in made.request and ("accept", "*/*") in made.request
    assert made.request[-1] == "closed"
    # The redirect is handed back, not followed.
    assert response.status == 303 and response.headers["location"] == REDIRECT and len(connection.made) == 1


def test_a_compressed_reply_is_read(connection):
    connection.reply = (200, {"Content-Encoding": "gzip"}, gzip.compress(b'{"items": []}'))
    assert zepp.https("GET", "https://api-mifit.zepp.com/users/1/devices?a=b", {}, None).body == b'{"items": []}'
    assert connection.made[0].request[0] == ("GET", "/users/1/devices?a=b", True)


def test_a_reply_too_long_to_be_right_is_refused(connection, monkeypatch):
    monkeypatch.setattr(zepp, "MAX_RESPONSE", 1000)
    connection.reply = (200, {}, bytes(5000))
    with pytest.raises(zepp.ZeppError, match="too long"):
        zepp.https("GET", "https://api-mifit.zepp.com/x", {}, None)
    # Small on the wire, enormous once unpacked.
    connection.reply = (200, {"Content-Encoding": "gzip"}, gzip.compress(bytes(200_000)))
    with pytest.raises(zepp.ZeppError, match="too long"):
        zepp.https("GET", "https://api-mifit.zepp.com/x", {}, None)


def test_nothing_is_sent_anywhere_but_https():
    for url in ("http://api-user-us2.zepp.com/v2/registrations/tokens", "file:///etc/passwd", "zepp"):
        with pytest.raises(zepp.ZeppError):
            zepp.https("POST", url, {}, b"secret")


def test_a_certificate_that_does_not_check_out_is_explained(monkeypatch):
    class Untrusted(FakeConnection):
        def endheaders(self, body):
            error = zepp.ssl.SSLCertVerificationError("certificate verify failed")
            error.verify_message = "unable to get local issuer certificate"
            raise error

    monkeypatch.setattr(zepp.http.client, "HTTPSConnection", Untrusted)
    with pytest.raises(zepp.ZeppError, match="did not prove it is Zepp"):
        zepp.https("POST", zepp.TOKENS_URL, {}, b"body")

    class Offline(FakeConnection):
        def putrequest(self, *args, **kwargs):
            raise OSError("Network is unreachable")

    monkeypatch.setattr(zepp.http.client, "HTTPSConnection", Offline)
    with pytest.raises(zepp.ZeppError, match="Couldn't reach Zepp"):
        zepp.https("POST", zepp.TOKENS_URL, {}, b"body")


def test_the_key_is_saved_private_and_whole(tmp_path):
    path = tmp_path / "helios" / "auth.toml"
    config.save_auth("aa:bb:cc:dd:ee:02", KEY, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    auth = config.load_auth(path)
    assert (auth.address, auth.key.hex()) == ("AA:BB:CC:DD:EE:02", KEY)
    # Replacing it leaves no half-written or stray file behind.
    config.save_auth("AA:BB:CC:DD:EE:02", "cd" * 16, path)
    assert config.load_auth(path).key.hex() == "cd" * 16
    assert [f.name for f in path.parent.iterdir()] == ["auth.toml"]


def test_what_was_typed_is_cleaned_of_control_characters():
    from helios.cli import clean_line

    assert clean_line("  wearer@example.com \n") == "wearer@example.com"
    # A Backspace or an arrow key the terminal echoed instead of applying.
    assert clean_line("\x7fwearer@example.com") == "wearer@example.com"
    assert clean_line("wearer\x1b[D@example.com\x08") == "wearer[D@example.com"
    assert clean_line("\x7f\x7f") == ""


@pytest.mark.parametrize("erase", ["^?", "^H"])
def test_backspace_edits_the_email_whatever_the_terminal_calls_erase(erase):
    """Type the email with a correction, in a real pseudo-terminal whose
    erase key does or doesn't match the Backspace sent."""
    import os
    import pty
    import select
    import subprocess
    import sys
    import time

    script = (
        "import subprocess, sys\n"
        f"subprocess.run(['stty', 'erase', {erase!r}])\n"
        "from helios import cli, zepp\n"
        "def fetch(email, password):\n"
        "    print('GOT', repr(email), repr(password), flush=True)\n"
        "    raise zepp.ZeppError('stopped before any request')\n"
        "zepp.fetch_devices = fetch\n"
        "cli._auth(None)\n"
    )
    controller, terminal = pty.openpty()
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    child = subprocess.Popen([sys.executable, "-c", script], stdin=terminal, stdout=terminal, stderr=terminal, env=env)
    os.close(terminal)

    def read_until(marker: bytes, seconds: float = 10.0) -> bytes:
        seen = b""
        deadline = time.monotonic() + seconds
        while marker not in seen and time.monotonic() < deadline:
            if select.select([controller], [], [], 0.2)[0]:
                try:
                    seen += os.read(controller, 4096)
                except OSError:
                    break
        return seen

    try:
        read_until(b"email: ")
        os.write(controller, b"wearex\x7fr@example.com\n")  # a slip, Backspace, and on
        read_until(b"type): ")
        os.write(controller, b"secret\n")
        output = read_until(b"stopped before any request")
    finally:
        child.wait(timeout=10)
        os.close(controller)
    assert b"GOT 'wearer@example.com' 'secret'" in output
    assert b"secret\r\n" not in output.split(b"GOT")[0]  # the password is not echoed


def test_a_proxy_in_the_environment_is_tunnelled_through(connection, monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    connection.reply = (200, {}, b"{}")
    zepp.https("GET", "https://api-mifit.zepp.com/x", {}, None)
    assert connection.made[-1].host == "api-mifit.zepp.com" and "tunnel" not in [
        r[0] for r in connection.made[-1].request
    ]
    monkeypatch.setenv("https_proxy", "http://me:p%40ss@proxy.example:3128")
    zepp.https("GET", "https://api-mifit.zepp.com/x", {}, None)
    made = connection.made[-1]
    # The connection goes to the proxy; TLS and its checks are still with Zepp.
    assert (made.host, made.port) == ("proxy.example", 3128)
    assert made.request[0] == ("tunnel", "api-mifit.zepp.com", 443, {"Proxy-Authorization": "Basic bWU6cEBzcw=="})
    assert made.context.check_hostname
    monkeypatch.setenv("no_proxy", "zepp.com")
    zepp.https("GET", "https://api-mifit.zepp.com/x", {}, None)
    assert connection.made[-1].host == "api-mifit.zepp.com"
    monkeypatch.delenv("no_proxy")
    monkeypatch.setenv("https_proxy", "socks5://proxy.example:1080")
    with pytest.raises(zepp.ZeppError, match="proxy"):
        zepp.https("GET", "https://api-mifit.zepp.com/x", {}, None)
