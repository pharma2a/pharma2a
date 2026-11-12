"""La route de la carte publique. Celle du SDK omet `skills` quand la liste est vide
(protobuf ne sérialise pas un champ répété vide) alors que la spécification l'exige, et
ne pose ni Cache-Control ni Last-Modified sans qu'on le lui demande. Ici : les trois."""
from __future__ import annotations
import hashlib
import json
from email.utils import formatdate
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from a2a.server.request_handlers.response_helpers import agent_card_to_dict

CARTE = "/.well-known/agent-card.json"


def route_carte(carte, cache_control: str = "public, max-age=3600", modifie_le: float | None = None) -> Route:
    d = agent_card_to_dict(carte)
    d.setdefault("skills", [])
    corps = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    etag = '"' + hashlib.sha256(corps).hexdigest()[:32] + '"'
    en_tetes = {"ETag": etag, "Cache-Control": cache_control,
                "Last-Modified": formatdate(modifie_le, usegmt=True)}

    async def servir(request: Request) -> Response:
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=en_tetes)
        if request.method == "HEAD":
            return Response(status_code=200, headers=en_tetes, media_type="application/json")
        return JSONResponse(d, headers=en_tetes)

    return Route(CARTE, servir, methods=["GET", "HEAD"])
