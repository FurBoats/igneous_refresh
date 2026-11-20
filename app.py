#!/usr/bin/env python3
"""igneous_refresh — trade base e-hentai cookies for a freshly issued igneous.

How it works: exhentai.org issues a fresh `igneous` cookie (Set-Cookie) to a
request that carries valid base cookies (ipb_member_id + ipb_pass_hash) but
*no* igneous, provided the request comes from a network/IP the site accepts.
This service does exactly that one request — through REFRESH_PROXY when set —
and hands the merged cookie set back as JSON.

HTTP API (JSON in, JSON out):

  POST {REFRESH_PATH}          (default /refresh)
  {"cookie": "ipb_member_id=1234567; ipb_pass_hash=0123456789abcdef"}
  or
  {"cookies": {"ipb_member_id": "1234567", "ipb_pass_hash": "0123456789abcdef"}}
  or
  {"ipb_member_id": "1234567", "ipb_pass_hash": "0123456789abcdef"}

  -> 200 {"ok": true, "igneous": "...",
          "cookies": {"ipb_member_id": "...", "ipb_pass_hash": "...",
                      "igneous": "..."},
          "cookie": "ipb_member_id=...; ipb_pass_hash=...; igneous=..."}
  -> 400 {"ok": false, "error": "..."}   bad input
  -> 502 {"ok": false, "error": "..."}   upstream did not issue a usable
                                         igneous (missing, empty, "mystery"
                                         or "null" all count as failure)

  GET /healthz -> {"ok": true}           (never triggers a refresh)

Environment:
  REFRESH_PROXY   Proxy used for the refresh request. http, https, socks4,
                  socks4a, socks5 and socks5h URLs, with optional auth, e.g.
                  http://user:pass@host:3128 or socks5://user:pass@host:1080.
                  Empty (default) = connect directly.
  REFRESH_PATH    The only path that accepts refresh requests.
                  Default: /refresh
  PORT            Listen port. Default: 8080
  REFRESH_TIMEOUT Upstream timeout in seconds. Default: 30
  TARGET_URL      Upstream page to request. Default: https://exhentai.org/
                  (exists for testing; leave unset in production)

Cookie values are never written to the logs.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from curl_cffi import requests as cffi_requests

# libcurl (under curl_cffi) silently honors proxy environment variables —
# including NO_PROXY bypasses — even when an explicit proxy is configured.
# This process exists to make exactly one kind of request, and REFRESH_PROXY
# must be the only thing deciding how it leaves the host, so scrub them.
for _var in (
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
):
    os.environ.pop(_var, None)

TARGET_URL = os.environ.get("TARGET_URL", "https://exhentai.org/")
REFRESH_PROXY = os.environ.get("REFRESH_PROXY", "").strip()
REFRESH_PATH = os.environ.get("REFRESH_PATH", "/refresh").strip() or "/refresh"
if not REFRESH_PATH.startswith("/"):
    REFRESH_PATH = "/" + REFRESH_PATH
PORT = int(os.environ.get("PORT", "8080"))
TIMEOUT = float(os.environ.get("REFRESH_TIMEOUT", "30"))

BASE_COOKIE_NAMES = ("ipb_member_id", "ipb_pass_hash")

# exhentai answers a request it will not grant access to with a placeholder
# igneous instead of a real one ("mystery"), and a missing/empty or literal
# "null" value is just as unusable. All of them mean the refresh failed.
INVALID_IGNEOUS_VALUES = {"", "mystery", "null"}
REFRESH_FAILED_ERROR = (
    "Refresh failed: please check your account details, or this account "
    "does not have permission to access https://exhentai.org."
)
PROXY_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")
MAX_BODY = 64 * 1024


def parse_cookie_header(header):
    out = {}
    for part in header.split(";"):
        if "=" in part:
            name, value = part.split("=", 1)
            out[name.strip()] = value.strip()
    return out


def extract_base_cookies(payload):
    """Pull ipb_member_id / ipb_pass_hash out of any accepted input shape.

    Anything else the caller sent — in particular a stale igneous — is
    dropped on purpose: the upstream only issues a fresh igneous to a
    request that does not already carry one.
    """
    raw = {}
    if isinstance(payload, dict):
        cookies_obj = payload.get("cookies")
        if isinstance(cookies_obj, dict):
            raw.update({str(k): str(v) for k, v in cookies_obj.items()})
        cookie_str = payload.get("cookie")
        if isinstance(cookie_str, str):
            raw.update(parse_cookie_header(cookie_str))
        for name in BASE_COOKIE_NAMES:
            if payload.get(name):
                raw[name] = str(payload[name])
    base = {}
    for name in BASE_COOKIE_NAMES:
        value = (raw.get(name) or "").strip()
        if value:
            base[name] = value
    return base


def cookies_to_dict(jar):
    if jar is None:
        return {}
    get_dict = getattr(jar, "get_dict", None)
    if callable(get_dict):
        try:
            return {str(k): str(v) for k, v in get_dict().items()}
        except Exception:
            pass
    try:
        return {str(k): str(v) for k, v in jar.items()}
    except Exception:
        return {}


def fetch_fresh_cookies(base_cookies):
    """GET the target without an igneous; return (status, merged cookies)."""
    session = cffi_requests.Session(impersonate="chrome")
    proxies = None
    if REFRESH_PROXY:
        proxies = {"http": REFRESH_PROXY, "https": REFRESH_PROXY}
    resp = session.get(
        TARGET_URL,
        cookies=base_cookies,
        proxies=proxies,
        timeout=TIMEOUT,
        allow_redirects=True,
    )
    merged = dict(base_cookies)
    merged.update(cookies_to_dict(session.cookies))
    for hop in list(getattr(resp, "history", []) or []) + [resp]:
        merged.update(cookies_to_dict(getattr(hop, "cookies", None)))
    return resp.status_code, merged


def build_cookie_header(cookies):
    ordered = [n for n in ("ipb_member_id", "ipb_pass_hash", "igneous") if n in cookies]
    ordered += [n for n in cookies if n not in ordered]
    return "; ".join(f"{name}={cookies[name]}" for name in ordered)


class Handler(BaseHTTPRequestHandler):
    server_version = "igneous_refresh/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep logs free of anything sensitive
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != REFRESH_PATH:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {"ok": False, "error": "missing or oversized body"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "body is not valid JSON"})
            return
        base = extract_base_cookies(payload)
        missing = [n for n in BASE_COOKIE_NAMES if n not in base]
        if missing:
            self._send_json(
                400,
                {"ok": False, "error": "missing base cookie(s): " + ", ".join(missing)},
            )
            return
        try:
            status, merged = fetch_fresh_cookies(base)
        except Exception as exc:  # proxy down, TLS failure, timeout, ...
            self._send_json(
                502, {"ok": False, "error": f"upstream request failed: {exc.__class__.__name__}"}
            )
            return
        igneous = (merged.get("igneous") or "").strip()
        if igneous.lower() in INVALID_IGNEOUS_VALUES:
            self._send_json(502, {"ok": False, "error": REFRESH_FAILED_ERROR})
            return
        self._send_json(
            200,
            {
                "ok": True,
                "igneous": igneous,
                "cookies": merged,
                "cookie": build_cookie_header(merged),
            },
        )


def main():
    if REFRESH_PROXY:
        scheme = urlsplit(REFRESH_PROXY).scheme.lower()
        if scheme not in PROXY_SCHEMES:
            sys.exit(
                f"REFRESH_PROXY scheme '{scheme}' is not supported; "
                f"use one of: {', '.join(PROXY_SCHEMES)}"
            )
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(
        f"igneous_refresh listening on :{PORT}, refresh path {REFRESH_PATH}, "
        f"proxy {'set' if REFRESH_PROXY else 'direct'}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
