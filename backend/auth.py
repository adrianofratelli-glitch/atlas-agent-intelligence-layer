"""Identidade por JWT — fecha o gap "user_key vem do payload".

Fluxo demo: POST /api/auth/token troca um user_key cadastrado por um JWT
assinado (HS256, segredo local). A partir daí o backend resolve a identidade
do TOKEN (claim `sub`), nunca do corpo da request — impersonar outro usuário
exigiria o segredo de assinatura, não apenas trocar uma string no JSON.

Produção substitui a EMISSÃO pelo IdP corporativo (OIDC/JWKS): a validação
continua a mesma, só troca a chave de verificação. `AUTH_REQUIRED=1` liga o
modo estrito (request sem token válido é rejeitada); desligado, o backend
aceita o fallback do payload para manter a demo plug-and-play, com warning.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException, Request

logger = logging.getLogger("poc.auth")

JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_TTL_HOURS = float(os.getenv("JWT_TTL_HOURS", "8"))
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "0") == "1"
_ALGO = "HS256"

if not JWT_SECRET:
    # Segredo efêmero por processo: tokens valem só até o restart. Suficiente
    # para demo; produção define JWT_SECRET (ou usa JWKS do IdP).
    JWT_SECRET = os.urandom(32).hex()
    logger.warning(
        "JWT_SECRET não definida — usando segredo efêmero (tokens invalidam no "
        "restart). Defina a env var para tokens estáveis."
    )
if AUTH_REQUIRED:
    logger.info("AUTH_REQUIRED=1 — requests sem Bearer token válido serão rejeitadas")
else:
    logger.warning(
        "AUTH_REQUIRED desligado (modo demo): user_key do payload é aceito como "
        "fallback quando não há token. Ligue com AUTH_REQUIRED=1."
    )


def issue_token(user_key: str, area: str, name: str = "", tier: str = "standard",
                rate_limit_multiplier: float = 1.0) -> dict:
    """Assina um JWT para uma identidade demo JÁ VALIDADA em app_users.

    `tier`/`rate_limit_multiplier` vêm de area_profiles (documento, editável
    por update_one) e são gravados como claims próprias — dimensão de "request
    context" separada de `area`/`sub`, no espírito de tokens que carregam
    security+tenant+request context como campos distintos. O valor é uma FOTO
    do momento da emissão: mudar o multiplicador no banco só afeta tokens
    emitidos depois (o TTL curto do token é o mecanismo de propagação, não uma
    query por request).
    """
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=JWT_TTL_HOURS)
    token = jwt.encode(
        {"sub": user_key, "area": area, "tier": tier,
         "rate_limit_multiplier": rate_limit_multiplier, "name": name,
         "iat": int(now.timestamp()), "exp": int(exp.timestamp()),
         "iss": "intelligence-layer-poc"},
        JWT_SECRET, algorithm=_ALGO,
    )
    return {"access_token": token, "token_type": "bearer",
            "expires_at": exp.isoformat()}


def resolve_user_key(request: Request, fallback: str | None) -> str | None:
    """A identidade do turno: claim `sub` do Bearer token.

    Com token válido, o `sub` VENCE qualquer user_key do payload (impersonação
    via body deixa de existir). Sem token: 401 se AUTH_REQUIRED, senão fallback
    do payload (modo demo).
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        try:
            claims = jwt.decode(token, JWT_SECRET, algorithms=[_ALGO],
                                issuer="intelligence-layer-poc")
            subject = claims.get("sub")
            if not isinstance(subject, str) or not subject:
                raise HTTPException(status_code=401, detail="Token sem identidade.")
            return subject
        except jwt.InvalidTokenError as exc:
            raise HTTPException(status_code=401, detail="Token inválido.") from exc
    if AUTH_REQUIRED:
        raise HTTPException(status_code=401, detail="Bearer token obrigatório.")
    return fallback


def _decode_claims(request: Request) -> dict:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return {}
    try:
        return jwt.decode(header[7:].strip(), JWT_SECRET, algorithms=[_ALGO],
                          issuer="intelligence-layer-poc")
    except jwt.InvalidTokenError:
        return {}


def resolve_tier(request: Request, fallback: str = "standard") -> str:
    """Claim `tier` do Bearer token, sem round-trip ao banco."""
    return _decode_claims(request).get("tier") or fallback


def resolve_rate_limit_multiplier(request: Request, fallback: float = 1.0) -> float:
    """Claim `rate_limit_multiplier` do Bearer token — diferencia o limite por
    tier direto do token assinado, sem nova query por request."""
    value = _decode_claims(request).get("rate_limit_multiplier")
    return float(value) if isinstance(value, (int, float)) else fallback
