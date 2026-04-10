"""Vercel Python Function: TimeTagger API only.

Static assets live in /public and are served by Vercel Edge Network; vercel.ts
rewrites /api/v2/* to this function.
"""
import asgineer

from timetagger.server import authenticate, AuthException, api_handler_triage
from timetagger.__main__ import get_webtoken


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
