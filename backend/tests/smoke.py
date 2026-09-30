"""Smoke test pós-deploy: valida o caminho crítico contra o backend NO AR.

Uso:  python tests/smoke.py [BASE_URL]   (default http://127.0.0.1:8010)

Não substitui os testes unitários — responde só "o deploy está de pé?":
health, auth, isolamento básico e métricas. Sai com código != 0 em falha
(encaixa em CI/CD).
"""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
FAILURES: list[str] = []


def call(method: str, path: str, body: dict | None = None,
         headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


print(f"Smoke test contra {BASE}\n")

# 1. Health: Atlas alcançável, config de modelo ativa
status, body = call("GET", "/api/health")
check("health responde 200", status == 200, f"status={status}")
check("Atlas ping ok", body.get("ping") == "ok")
check("model_config ativa", bool(body.get("primary_model")))

# 2. Auth: identidade cadastrada emite token; desconhecida é rejeitada
status, tok = call("POST", "/api/auth/token", {"user_key": "cliente-demo"})
check("token para identidade demo", status == 200 and "access_token" in tok)
status, _ = call("POST", "/api/auth/token", {"user_key": "nao-existe-xyz"})
check("identidade desconhecida rejeitada", status in (400, 401, 403, 503))

bearer = {"Authorization": f"Bearer {tok.get('access_token', '')}"}

# 3. Guardrails: regras da área carregadas
status, rules = call("GET", "/api/guardrails/rules?area=default")
check("policy de guardrail carregada", status == 200 and bool(rules.get("policy")))

# 4. Eventos escopados: sem admin key, area=all deve ser negado QUANDO
#    ADMIN_API_KEY está configurada (em modo demo aberto, 200 é aceitável)
status, _ = call("GET", "/api/guardrails/events?area=all")
check("events area=all responde (200 demo / 401 com admin key)",
      status in (200, 401))

# 5. Memória: inspeção com token funciona
status, mem = call("GET", "/api/memory/cliente-demo", headers=bearer)
check("memória longa inspecionável", status == 200 and "facts" in mem)

# 6. Métricas expostas
status, met = call("GET", "/api/metrics")
check("métricas expostas", status == 200 and "routes" in met)

print()
if FAILURES:
    print(f"FALHOU: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
    sys.exit(1)
print("Smoke test OK")
