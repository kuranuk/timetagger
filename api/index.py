"""Vercel Python Function: TimeTagger API only.

Static assets live in /public and are served by Vercel Edge Network; vercel.json
rewrites /api/v2/* to this function.
"""
import os
import sys
import json
from pathlib import Path
from base64 import b64decode

# Ensure the repo root is on sys.path so `timetagger` is importable even when
# the package is not pip-installed (Vercel only installs requirements.txt deps).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Point data directory at /tmp (writable on Vercel) before importing timetagger,
# because timetagger.server._utils creates the data dir at import time.
os.environ.setdefault("TIMETAGGER_DATADIR", "/tmp/_timetagger")

import bcrypt
import asgineer

from timetagger.server import authenticate, AuthException, api_handler_triage, get_webtoken_unsafe
from timetagger import config


def _load_credentials():
    d = {}
    for s in config.credentials.replace(";", ",").split(","):
        name, _, hash = s.partition(":")
        if name and hash:
            d[name] = hash
    return d


_CREDENTIALS = _load_credentials()


async def get_webtoken(request):
    """Exchange credentials for a webtoken (Vercel-compatible version).

    Only supports the usernamepassword method — localhost and proxy auth
    are not meaningful on a serverless platform.
    """
    auth_info = json.loads(b64decode(await request.get_body()))
    method = auth_info.get("method", "unspecified")

    if method == "usernamepassword":
        user = auth_info.get("username", "").strip()
        pw = auth_info.get("password", "").strip()
        hash = _CREDENTIALS.get(user, "")
        if user and hash and bcrypt.checkpw(pw.encode(), hash.encode()):
            token = await get_webtoken_unsafe(user)
            return 200, {}, dict(token=token)
        else:
            return 403, {}, "Invalid credentials"
    else:
        return 401, {}, f"Invalid authentication method: {method}"


@asgineer.to_asgi
async def app(request):
    path = request.path

    if path == "/api/v2/bootstrap_authentication":
        return await get_webtoken(request)

    if not path.startswith("/api/v2/"):
        return 404, {}, "not found"

    subpath = path.removeprefix("/api/v2/").strip("/")

    try:
        auth_info, db = await authenticate(request)
    except AuthException as err:
        return 401, {}, f"unauthorized: {err}"

    try:
        return await api_handler_triage(request, subpath, auth_info, db)
    finally:
        await db.close()
