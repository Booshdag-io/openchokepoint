#!/usr/bin/env python3
# Handling: Unclassified — public
# SPDX-License-Identifier: Apache-2.0
"""Identifier source check (rule ID-001).

A real record (anything not marked `synthetic: true`) that carries a part
number, serial number or lot number must use an identifier that appears,
word for word, in at least one of that record's own cited sources. If no
cited source contains it, the identifier is invented and the record must be
marked `synthetic: true` (see docs/digital-thread.md and DATA-CONTRIBUTIONS.md).

Usage:
    python3 .github/scripts/identifier_source_check.py data/path/file.yaml [...]

Checked fields:
    Component.part_number, SerialItem.serial, MaterialLot.lot_number

Matching: case-insensitive, after removing HTML tags and decoding entities,
with all dash characters treated as "-" and runs of whitespace collapsed.
A source that cannot be fetched or read cannot confirm anything, so an
identifier with no readable source fails (fail closed).

Safety: only public https URLs are fetched; hosts that resolve to private,
loopback, link-local or reserved addresses are refused; redirects are
re-checked; responses are capped in size; the number of fetches is capped.
The script never runs contributor code. Exits non-zero on any failure.
"""
import html
import ipaddress
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

RULE = "ID-001"
FIELDS = {"Component": "part_number", "SerialItem": "serial", "MaterialLot": "lot_number"}
USER_AGENT = "OpenChokepoint identifier-check/1.0"  # no URL: sec.gov rejects agents that carry one
# sec.gov refuses requests whose User-Agent carries no contact email. CI sets
# this from the repository variable SOURCE_CHECK_CONTACT; it is never hard-coded.
CONTACT = os.environ.get("SOURCE_CHECK_CONTACT", "").strip()
if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", CONTACT):
    USER_AGENT += " " + CONTACT
MAX_BYTES = 5_000_000
MAX_FETCHES = 40
TIMEOUT = 20
DASHES = re.compile("[‐‑‒–—―−﹘﹣－]")
SPACE = re.compile(r"\s+")
DROP_BLOCKS = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.I | re.S)
TAG = re.compile(r"<[^>]+>")


class FetchError(Exception):
    pass


def normalize(text):
    text = html.unescape(text).replace(" ", " ")
    text = DASHES.sub("-", text)
    return SPACE.sub(" ", text).strip().casefold()


def page_text(raw):
    return normalize(TAG.sub(" ", DROP_BLOCKS.sub(" ", raw)))


def contains(text, identifier):
    """True if the normalized identifier appears in the normalized text.

    Also tries a compact form with whitespace removed on both sides, so an
    identifier split by inline markup (e.g. TR-<b>100</b>) still matches.
    """
    ident = normalize(identifier)
    if not ident:
        return False
    if ident in text:
        return True
    compact = ident.replace(" ", "")
    return len(compact) >= 4 and compact in text.replace(" ", "")


def is_public_https(url, resolver=socket.getaddrinfo):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return False
    try:
        infos = resolver(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_public_https(newurl):
            raise FetchError(f"redirect to a non-public or non-https URL refused: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_CheckedRedirects())


def fetch(url):
    """Return the page as normalized text, or raise FetchError."""
    if not is_public_https(url):
        raise FetchError("not a public https URL")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"})
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as resp:
            ctype = resp.headers.get_content_type()
            if not (ctype.startswith("text/") or ctype in ("application/xhtml+xml", "application/xml")):
                raise FetchError(f"content type {ctype} cannot be read as text")
            raw = resp.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise FetchError("page larger than the size cap")
            charset = resp.headers.get_content_charset() or "utf-8"
    except FetchError:
        raise
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code == 403 and not CONTACT:
            hint = "; the site may require a contact email: set the SOURCE_CHECK_CONTACT repository variable"
        raise FetchError(f"HTTP {exc.code}{hint}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FetchError(str(getattr(exc, "reason", exc)))
    return page_text(raw.decode(charset, errors="replace"))


def record_urls(obj):
    urls = list(obj.get("sources") or [])
    if obj.get("citation"):
        urls.append(obj["citation"])
    seen, out = set(), []
    for u in urls:
        if isinstance(u, str) and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def find_line(lines, field, value):
    pattern = re.compile(rf"^\s*{re.escape(field)}\s*:\s*['\"]?{re.escape(str(value))}['\"]?\s*(#.*)?$")
    for n, line in enumerate(lines, 1):
        if pattern.match(line):
            return n
    return 1


class Checker:
    def __init__(self, fetcher=fetch, max_fetches=MAX_FETCHES):
        self.fetcher = fetcher
        self.max_fetches = max_fetches
        self.cache = {}

    def get(self, url):
        if url not in self.cache:
            if len(self.cache) >= self.max_fetches:
                self.cache[url] = FetchError("fetch limit reached")
            else:
                try:
                    self.cache[url] = self.fetcher(url)
                except FetchError as exc:
                    self.cache[url] = exc
        return self.cache[url]

    def check_object(self, obj):
        """Return (status, detail). status is 'skip', 'pass' or 'fail'."""
        field = FIELDS.get(obj.get("type"))
        if not field or obj.get("synthetic") is True:
            return "skip", ""
        value = obj.get(field)
        if not isinstance(value, str) or not value.strip():
            return "skip", ""
        urls = record_urls(obj)
        if not urls:
            return "fail", "no cited source to confirm it against"
        unreadable = []
        for url in urls:
            result = self.get(url)
            if isinstance(result, FetchError):
                unreadable.append(f"{url} ({result})")
                continue
            if contains(result, value):
                return "pass", url
        if len(unreadable) == len(urls):
            return "fail", "no cited source could be read: " + "; ".join(unreadable)
        detail = "it does not appear in any cited source"
        if unreadable:
            detail += " (could not read: " + "; ".join(unreadable) + ")"
        return "fail", detail

    def check_file(self, path):
        text = Path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        doc = yaml.safe_load(text) or {}
        failures, passes = [], []
        for obj in doc.get("objects") or []:
            if not isinstance(obj, dict):
                continue
            status, detail = self.check_object(obj)
            if status == "skip":
                continue
            field = FIELDS[obj["type"]]
            entry = dict(path=str(path), id=obj.get("id", "?"), field=field, value=obj[field],
                         line=find_line(lines, field, obj[field]), detail=detail)
            (passes if status == "pass" else failures).append(entry)
        return passes, failures


def main(argv):
    paths = [p for p in argv if p.endswith((".yaml", ".yml")) and Path(p).name != "provenance.yaml"]
    if not paths:
        print("Identifier source check: no data files to check.")
        return 0
    checker = Checker()
    all_failures = []
    for path in paths:
        passes, failures = checker.check_file(path)
        for p in passes:
            print(f"ok   {p['path']}: {p['id']} {p['field']} \"{p['value']}\" found in {p['detail']}")
        for f in failures:
            msg = (f"{RULE} {f['id']}: {f['field']} \"{f['value']}\" is marked real (not synthetic: true) "
                   f"but {f['detail']}. Mark the record synthetic: true, or cite a public source that "
                   f"shows this exact identifier.")
            print(f"::error file={f['path']},line={f['line']},title={RULE} identifier not in source::{msg}")
        all_failures.extend(failures)
    if all_failures:
        print(f"Identifier source check failed: {len(all_failures)} identifier(s) not found in a cited source.")
        return 1
    print("Identifier source check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
