# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 atlastimed
# The sign-in exchange follows huami-token, Copyright (c) 2020-2025 Kirill Snezhko,
# MIT License (see NOTICE.md).
"""Fetch the strap's Bluetooth key from its owner's Zepp account.

The key is made when the strap is paired with the Zepp app and is kept in the
account. Getting it takes four requests, the same ones the app makes: trade
the email and password for an access token, the access token for an app
token, list the account's devices (each with its key), and sign out again.

The requests and their fixed fields follow huami-token
(https://codeberg.org/argrento/huami-token, commit 1b326585), which Helios
used to download and run for this. Doing it here needs nothing beyond Python:
the connections are verified against this computer's own certificate store,
which the system keeps current, rather than a bundle pinned in a lockfile.

Nothing here prints or logs the password, the tokens or the key.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import json
import re
import secrets
import ssl
import urllib.parse
import urllib.request
import uuid
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .crypto import aes_cbc_encrypt

TIMEOUT = 20.0  # seconds for each request
MAX_RESPONSE = 1024 * 1024  # bytes of any reply that are read, before and after decompression

TOKENS_URL = "https://api-user-us2.zepp.com/v2/registrations/tokens"
LOGIN_URL = "https://api-mifit-us2.zepp.com/v2/client/login"
LOGOUT_URL = "https://api-mifit-us2.zepp.com/v1/client/logout"
DEVICES_URL = "https://api-mifit.zepp.com/users/{user_id}/devices"

# The app sends the sign-in form encrypted with a key and IV that are the
# same for everyone. It keeps nothing secret; TLS does that.
FORM_KEY = b"xeNtBVqzDc6tuNTh"
FORM_IV = b"MAAAYAAAAAAAAABg"

APP_VERSION = "9.12.5"
APP_BUILD = "151689"
APP_AGENT = f"Zepp/{APP_VERSION} (Pixel 4; Android 12; Density/2.75)"
CHANNEL = "a100900101016"

TOKENS_HEADERS = {
    "app_name": "com.huami.midong",
    "appname": "com.huami.midong",
    "cv": f"{APP_BUILD}_{APP_VERSION}",
    "v": "2.0",
    "appplatform": "android_phone",
    "vb": "202509151347",
    "vn": APP_VERSION,
    "user-agent": APP_AGENT,
    "x-hm-ekv": "1",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "accept-encoding": "gzip",
}
LOGIN_HEADERS = {
    "app_name": "com.huami.webapp",
    "appname": "com.huami.webapp",
    "origin": "https://user.zepp.com",
    "referer": "https://user.zepp.com/",
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.5",
}
DEVICES_HEADERS = {
    "hm-privacy-diagnostics": "false",
    "country": "US",
    "appplatform": "android_phone",
    "hm-privacy-ceip": "true",
    "timezone": "Europe/London",
    "channel": CHANNEL,
    "vb": "202509151347",
    "cv": f"{APP_BUILD}_{APP_VERSION}",
    "appname": "com.huami.midong",
    "v": "2.0",
    "vn": APP_VERSION,
    "lang": "en_US",
    "user-agent": APP_AGENT,
    "accept-encoding": "gzip",
}
LOGOUT_HEADERS = {
    "app_name": "com.huami.midong",
    "hm-privacy-ceip": "false",
    "accept-language": "en-US",
    "appname": "com.huami.midong",
    "cv": f"{APP_BUILD}_{APP_VERSION}",
    "v": "2.0",
    "appplatform": "android_phone",
    "vb": "202509151347",
    "vn": APP_VERSION,
    "user-agent": APP_AGENT,
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
}


class ZeppError(Exception):
    """Something the person at the terminal can act on, said in a sentence."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]  # names in lower case
    body: bytes


@dataclass(frozen=True)
class Session:
    app_token: str
    login_token: str
    user_id: str


@dataclass(frozen=True)
class Device:
    address: str  # AA:BB:CC:DD:EE:FF
    active: bool
    key: str  # 32 hex characters, or "" when the account holds none for it


# (method, URL, headers, body) -> Response. Tests pass their own.
Send = Callable[[str, str, dict[str, str], bytes | None], Response]


def https(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
    """One HTTPS request, with the server's certificate and name checked
    against the system's trust store. Redirects are returned, not followed:
    the sign-in's answer is one."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ZeppError(f"Refusing to send to {url}: not an https address.")
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    context = ssl.create_default_context()
    proxy = _proxy_for(parts.hostname)
    if proxy is None:
        connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=TIMEOUT, context=context)
    else:
        # Through the proxy as a tunnel: TLS is still end to end with Zepp,
        # and it is still Zepp's certificate and name that are checked.
        host, port, credentials = proxy
        connection = http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=context)
        connection.set_tunnel(parts.hostname, parts.port or 443, headers=credentials)
    try:
        connection.putrequest(method, target, skip_accept_encoding=True)
        for name, value in headers.items():
            connection.putheader(name, value)
        if "accept" not in headers:
            connection.putheader("accept", "*/*")
        if body is not None:
            connection.putheader("content-length", str(len(body)))
        connection.endheaders(body)
        reply = connection.getresponse()
        data = reply.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ZeppError("Zepp's reply was too long to be what was asked for.")
        if (reply.getheader("content-encoding") or "").lower() == "gzip":
            inflater = zlib.decompressobj(wbits=31)
            data = inflater.decompress(data, MAX_RESPONSE)
            if inflater.unconsumed_tail:
                raise ZeppError("Zepp's reply was too long to be what was asked for.")
        return Response(reply.status, {name.lower(): value for name, value in reply.getheaders()}, data)
    except ssl.SSLCertVerificationError as error:
        raise ZeppError(
            f"{parts.hostname} did not prove it is Zepp ({error.verify_message}). Nothing was sent. "
            "If a proxy or security program inspects this computer's connections, that is why."
        ) from None
    except (OSError, http.client.HTTPException, zlib.error) as error:
        raise ZeppError(f"Couldn't reach Zepp ({parts.hostname}): {error or type(error).__name__}.") from None
    finally:
        connection.close()


def _proxy_for(hostname: str) -> tuple[str, int, dict[str, str]] | None:
    """The proxy the environment names for HTTPS (https_proxy, no_proxy), as
    host, port and the headers that sign in to it, or None to connect direct."""
    address = urllib.request.getproxies().get("https")
    if not address or urllib.request.proxy_bypass(hostname):
        return None
    proxy = urllib.parse.urlsplit(address if "://" in address else f"http://{address}")
    if proxy.scheme != "http" or not proxy.hostname:
        raise ZeppError("The https_proxy in your environment is not an http:// proxy address Helios can use.")
    headers = {}
    if proxy.username:
        login = f"{urllib.parse.unquote(proxy.username)}:{urllib.parse.unquote(proxy.password or '')}"
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(login.encode()).decode()
    return proxy.hostname, proxy.port or 8080, headers


def _form(fields: Mapping[str, object]) -> bytes:
    return urllib.parse.urlencode(fields, doseq=True).encode()


def _json(response: Response, what: str) -> dict:
    try:
        data = json.loads(response.body)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ZeppError(f"Zepp's reply to {what} was not what Helios expects; its service may have changed.")
    return data


def sign_in(email: str, password: str, send: Send = https) -> Session:
    form = {
        "emailOrPhone": email,
        "state": "REDIRECTION",
        "client_id": "HuaMi",
        "password": password,
        "redirect_uri": "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html",
        "region": "us-west-2",
        "token": ["access", "refresh"],
        "country_code": "US",
    }
    response = send("POST", TOKENS_URL, TOKENS_HEADERS, aes_cbc_encrypt(_form(form), FORM_KEY, FORM_IV))
    # A good sign-in is answered with a redirect that carries the tokens.
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(response.headers.get("location", "")).query)
    access = query.get("access", [""])[0]
    if response.status != 303 or not access:
        if response.status == 429:
            raise ZeppError("Zepp is refusing sign-ins for now (too many attempts). Try again later.")
        raise ZeppError(
            "Zepp didn't accept that email and password. Check them, and that the account has a password: "
            'one made with "Sign in with Apple" or Google needs one set in the Zepp app first.'
        )

    login = {
        "code": access,
        "device_id": str(uuid.uuid4()),
        "device_model": "android_phone",
        "app_version": APP_VERSION,
        "dn": "api-mifit.zepp.com,api-user.zepp.com,api-mifit.zepp.com,api-watch.zepp.com,"
        "app-analytics.zepp.com,auth.zepp.com,api-analytics.zepp.com",
        "third_name": "huami",
        "source": f"com.huami.watch.hmwatchmanager:{APP_VERSION}:{APP_BUILD}",
        "app_name": "com.huami.midong",
        "country_code": "US",
        "grant_type": "access_token",
        "allow_registration": "false",
        "lang": "en",
        "countryState": "US-NY",
    }
    response = send("POST", LOGIN_URL, LOGIN_HEADERS, _form(login))
    if response.status != 200:
        raise ZeppError(f"Zepp refused the sign-in at its second step (status {response.status}).")
    info = _json(response, "the sign-in").get("token_info")
    info = info if isinstance(info, dict) else {}
    app_token, login_token, user_id = (str(info.get(name) or "") for name in ("app_token", "login_token", "user_id"))
    if not (app_token and login_token and user_id):
        raise ZeppError("Zepp signed in but sent no session; its service may have changed.")
    return Session(app_token, login_token, user_id)


def devices(session: Session, send: Send = https) -> list[Device]:
    """The account's devices, active pairings first, one per address."""
    once = str(uuid.uuid4())
    query = {
        "r": [once, once],  # the app sends it twice
        "enableMultiDeviceOnMultiType": ["true", "true"],
        "userid": session.user_id,
        "appid": str(secrets.randbits(64)),
        "channel": CHANNEL,
        "country": "US",
        "cv": f"{APP_BUILD}_{APP_VERSION}",
        "device": "android_32",
        "device_type": "android_phone",
        "enableMultiDevice": "true",
        "lang": "en_US",
        "timezone": "Europe/London",
        "v": "2.0",
    }
    headers = {**DEVICES_HEADERS, "x-request-id": str(uuid.uuid4()), "apptoken": session.app_token}
    url = DEVICES_URL.format(user_id=urllib.parse.quote(session.user_id, safe="")) + "?" + _form(query).decode()
    response = send("GET", url, headers, None)
    if response.status != 200:
        raise ZeppError(f"Zepp wouldn't list the account's devices (status {response.status}).")
    items = _json(response, "the device list").get("items")
    found: dict[str, Device] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        address = str(item.get("macAddress") or "").upper()
        if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
            continue
        try:
            extra = json.loads(item.get("additionalInfo") or "{}")
        except (TypeError, ValueError):
            extra = {}
        key = str(extra.get("auth_key") or "") if isinstance(extra, dict) else ""
        key = key.removeprefix("0x").lower()
        device = Device(address, bool(item.get("activeStatus")), key if re.fullmatch(r"[0-9a-f]{32}", key) else "")
        # An address paired more than once: keep the active pairing's key.
        if address not in found or (device.active and not found[address].active):
            found[address] = device
    return sorted(found.values(), key=lambda d: not d.active)


def sign_out(session: Session, send: Send = https) -> None:
    """End the session just made, so no token outlives its one use. A failure
    here loses nothing, and is not reported."""
    with contextlib.suppress(ZeppError):
        send("POST", LOGOUT_URL, LOGOUT_HEADERS, _form({"login_token": session.login_token, "os_verison": "vnull"}))


def fetch_devices(email: str, password: str, send: Send = https) -> list[Device]:
    session = sign_in(email, password, send)
    try:
        return devices(session, send)
    finally:
        sign_out(session, send)


def choose(found: list[Device], address: str | None, nearby: tuple[str, ...] = ()) -> Device:
    """The strap among the account's devices: the one at `address` if the
    user gave one; otherwise the account's only active device, or its only
    device. `nearby` (addresses this computer has seen) only settles a tie:
    what the account says is paired now outranks what Bluetooth remembers."""
    if not found:
        raise ZeppError("Your Zepp account has no paired devices. Pair the strap in the Zepp app, then try again.")
    if address:
        matches = [d for d in found if d.address == address.upper()]
        if not matches:
            listed = ", ".join(d.address for d in found)
            raise ZeppError(f"No device with address {address.upper()} in your Zepp account. It has: {listed}.")
    else:
        active = [d for d in found if d.active]
        matches = active or found
        if len(matches) > 1:
            seen = {a.upper() for a in nearby}
            matches = [d for d in matches if d.address in seen] or matches
        if len(matches) != 1:
            listed = ", ".join(d.address for d in found)
            raise ZeppError(f"Your Zepp account has several devices ({listed}). Pass the strap's address.")
    device = matches[0]
    if not device.key:
        raise ZeppError(f"Zepp holds no key for {device.address}. Pair it again in the Zepp app, then try again.")
    return device
