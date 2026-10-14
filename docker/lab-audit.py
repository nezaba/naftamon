#!/usr/bin/env python3
"""Assert that the running lab uses only its isolated mock configuration."""

import base64
import configparser
import os
import zlib
from pathlib import Path


def decode_password(value):
    data = base64.b64decode(value, validate=True)
    for _ in range(5):
        data = zlib.decompress(data[4:])[::-1]
        data = base64.b64decode(data, validate=True)
    return data.decode("utf-8")


assert os.environ["HOME"] == "/tmp/naftamon-home"
assert os.environ["NAFTAMON_CONFIG"] == "/lab-state/config/naftamon.ini"
assert not Path(os.environ["HOME"], ".nagstamon").exists()

config = configparser.ConfigParser(interpolation=None)
files = config.read(os.environ["NAFTAMON_CONFIG"], encoding="utf-8")
assert len(files) == 1, "lab config is missing"
servers = config["servers"]
assert servers["size"] == "1", "expected exactly one test server"
assert servers[r"1\url"] == "http://thruk:18765/thruk/cgi-bin"
assert servers[r"1\username"] == "admin"
password = servers.get(r"1\password")
if password is None:
    password = decode_password(servers[r"1\password_obfuscated"])
assert password == "secret", "unexpected test credential"
print("Isolated mock configuration: OK")
