import os, time, json, httpx
from typing import Set
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.routing import Mount
from jose import jwt

from mcp.server.sse import create_sse_app
from .server import build_application

ISSUER   = os.environ["MCP_AUTH_ISSUER"].rstrip("/")
AUDIENCE = os.environ["MCP_AUTH_AUDIENCE"]

# mappa: soggetto (tool/prompt) -> scopes richiesti
REQUIRED_SCOPES = {
    # tools
    "query": {"execute:query"},
    # prompts
    "cosmetiq-context-prompt": {"execute:cosmetiq-context-prompt"},
    # lascia libero quello di bootstrap, oppure aggiungi scope se vuoi
    "duckdb-motherduck-initial-prompt": set(),
}

# --- JWKS cache ---
_JWKS = {"keys": None, "ts": 0}
async def get_jwks():
    if _JWKS["keys"] and time.time() - _JWKS["ts"] < 600:
        return _JWKS["keys"]
    async with httpx.AsyncClient(timeout=5) as client:
        wk = (await client.get(f"{ISSUER}/.well-known/openid-configuration")).json()
        jwks = (await client.get(wk["jwks_uri"])).json()
    _JWKS.update({"keys": jwks, "ts": time.time()})
    return jwks

def scopes_from_claims(claims) -> Set[str]:
    if "scp" in claims and isinstance(claims["scp"], list):
        return set(claims["scp"])
    if "scope" in claims and isinstance(claims["scope"], str):
        return set(claims["scope"].split())
    return set()

async def verify_bearer(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse({"error": "Missing Bearer token"}, status_code=401)
    token = auth.split(" ", 1)[1]
    try:
        claims = jwt.decode(
            token,
            await get_jwks(),
            algorithms=["RS256", "ES256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"verify_at_hash": False},
        )
        request.state.claims = claims
        return None
    except Exception as e:
        return JSONResponse({"error": f"Invalid token: {e}"}, status_code=401)

async def enforce_scopes(request: Request):
    # le chiamate RPC SSE arrivano come POST JSON
    try:
        body_bytes = await request.body()
        if not body_bytes:
            return None, body_bytes
        payload = json.loads(body_bytes)
    except Exception:
        return JSONResponse({"error": "Bad JSON body"}, status_code=400), b""

    method = payload.get("method", "")
    params = payload.get("params", {}) or {}

    subject = None
    if method == "tools/call":
        subject = params.get("name")
    elif method == "prompts/get":
        subject = params.get("name")

    if subject:
        need = REQUIRED_SCOPES.get(subject, set())
        have = scopes_from_claims(request.state.claims)
        missing = sorted(need - have)
        if missing:
            return JSONResponse({"error": f"Missing scopes: {', '.join(missing)}"}, status_code=403), b""

    # restituiamo anche il body, per reiniettarlo a valle
    return None, body_bytes

def make_receive_with(body: bytes):
    sent = {"done": False}
    async def _receive():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}
    return _receive

# costruisci il tuo MCP server come già fai
server, init_opts = build_application(
    db_path=os.environ.get("DB_PATH", "md:"),
    motherduck_token=os.environ.get("motherduck_token"),
    home_dir=os.environ.get("HOME"),
    saas_mode=os.environ.get("SAAS_MODE", "").lower() in {"1","true","yes"},
    read_only=os.environ.get("READ_ONLY", "").lower() in {"1","true","yes"},
)

sse_app = create_sse_app(server)

async def guarded(scope, receive, send):
    if scope["type"] == "http" and scope["path"].startswith("/sse"):
        request = Request(scope, receive=receive)
        # 1) verifica token
        resp = await verify_bearer(request)
        if resp:
            return await resp(scope, receive, send)
        # 2) enforcement su POST (RPC)
        if request.method == "POST":
            resp, body = await enforce_scopes(request)
            if resp:
                return await resp(scope, receive, send)
            # reinietta il body per l'app SSE
            receive = make_receive_with(body)
    return await sse_app(scope, receive, send)

app = Starlette(routes=[Mount("/", guarded)])
