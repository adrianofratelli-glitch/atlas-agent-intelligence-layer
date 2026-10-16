"""Queryable Encryption: perfil sensível do cliente (CPF, telefone, renda) — demo STANDALONE.

Fora do caminho do agente, do MCP Server, do $vectorSearch e do $search (ADR-011): nada que
eles leem é cifrado. O padrão demonstrado é "texto mascarado vetorizável + PII cifrada":

    customer_sensitive_profile {
      user_key            (claro: escopo do tenant, filtro de servidor)
      resumo_mascarado    (claro: texto com PII mascarada pelo pov-shared; este sim pode
                           ganhar autoEmbed/BM25)
      cpf, telefone       (cifrados, consulta por IGUALDADE)
      renda_mensal        (cifrado, consulta por FAIXA — range só vale para tipo numérico/data;
                           CPF/telefone são string, então só igualdade)
    }

Explicit encryption (`AsyncClientEncryption.encrypt` / `encrypt_expression`) com
`bypass_query_analysis=True` no cliente que decifra: dispensa crypt_shared/mongocryptd, só
precisa do pymongocrypt (`pip install "pymongo[encryption]"`, ver requirements-qe.txt).

KMS local SÓ PARA DEMO: chave mestra de 96 bytes em backend/secrets/qe-local-master-key.bin
(gitignored, criada com permissão 0600 se não existir). Em produção: AWS/Azure/GCP KMS.

Escreve só em POC_test (scripts/isolation.py recusa a demo).

    cd backend && .venv/bin/python scripts/qe_sensitive_profile.py            # reset + demo
    cd backend && .venv/bin/python scripts/qe_sensitive_profile.py --json r.json

Docs (lidas antes de codar):
  https://www.mongodb.com/docs/manual/core/queryable-encryption/fundamentals/manual-encryption/
  https://www.mongodb.com/docs/manual/core/queryable-encryption/qe-create-encrypted-collection/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.isolation import use_test_databases  # noqa: E402

COLLECTION = "customer_sensitive_profile"
KEY_VAULT_COLLECTION = "qe_key_vault"
MASTER_KEY_PATH = Path(__file__).resolve().parents[1] / "secrets" / "qe-local-master-key.bin"
CONTENTION = 0          # baixa cardinalidade de escrita concorrente na demo
RENDA_MIN, RENDA_MAX = 0, 1_000_000
RANGE_SPARSITY, RANGE_TRIM = 1, 4

# Mapa de campos cifrados: IMUTÁVEL após o create; mudar = dropar e recriar a collection.
ENCRYPTED_FIELDS = {"fields": [
    {"path": "cpf", "bsonType": "string", "queries": [{"queryType": "equality",
                                                         "contention": CONTENTION}]},
    {"path": "telefone", "bsonType": "string", "queries": [{"queryType": "equality",
                                                              "contention": CONTENTION}]},
    {"path": "renda_mensal", "bsonType": "int", "queries": [{
        "queryType": "range", "contention": CONTENTION, "sparsity": RANGE_SPARSITY,
        "trimFactor": RANGE_TRIM, "min": RENDA_MIN, "max": RENDA_MAX}]},
]}
ENCRYPTED_PATHS = tuple(f["path"] for f in ENCRYPTED_FIELDS["fields"])

# Perfis SINTÉTICOS: CPFs com dígito verificador válido, gerados para a demo, sem dono real.
PROFILES = [
    {"user_key": "cliente-demo", "cpf": "529.982.247-25", "telefone": "(62) 98123-4567",
     "renda_mensal": 8_500, "obs": "Prefere e-mail; CPF 529.982.247-25 confirmado no cadastro."},
    {"user_key": "ana.vendas", "cpf": "111.444.777-35", "telefone": "(11) 99876-5432",
     "renda_mensal": 4_200, "obs": "Telefone (11) 99876-5432 só para entrega."},
    {"user_key": "marina.fin", "cpf": "390.533.447-05", "telefone": "(21) 97654-3210",
     "renda_mensal": 15_000, "obs": "Cliente do financeiro, renda declarada."},
]


def load_master_key(path: Path = MASTER_KEY_PATH) -> bytes:
    """96 bytes da chave local; cria (0600) na primeira vez. Nunca impressa nem versionada."""
    if path.exists():
        key = path.read_bytes()
        if len(key) != 96:
            raise SystemExit(f"chave local com {len(key)} bytes (esperado 96): {path}")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(96)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def masked_summary(profile: dict) -> str:
    """Texto CLARO e vetorizável: a PII sai mascarada (pov-shared), nunca o valor cifrado."""
    text = (f"Cliente {profile['user_key']}. {profile.get('obs', '')} "
            f"Contato {profile['telefone']}, documento {profile['cpf']}.")
    try:
        import guardrails  # pov-shared
        return guardrails.mask_pii(text).text
    except ImportError:  # sem pov-shared o resumo não sai: não arriscar PII em claro
        return f"Cliente {profile['user_key']}. (resumo indisponível sem pov-shared)"


def require_test_db(name: str) -> None:
    if not name.endswith("_test"):
        raise SystemExit(f"[qe] RECUSADO: {name} não é banco de teste")


class QEDemo:
    """Clientes e ClientEncryption da demo. Async, como o resto do repo."""

    def __init__(self, uri: str, db_name: str, master_key: bytes):
        from bson.codec_options import CodecOptions
        from pymongo import AsyncMongoClient
        from pymongo.asynchronous.encryption import AsyncClientEncryption
        from pymongo.encryption_options import AutoEncryptionOpts

        require_test_db(db_name)
        self.db_name = db_name
        self.kms = {"local": {"key": master_key}}
        self.key_vault_ns = f"{db_name}.{KEY_VAULT_COLLECTION}"
        self.plain = AsyncMongoClient(uri, appname="intelligence-layer-qe-plain")
        self.ce = AsyncClientEncryption(self.kms, self.key_vault_ns, self.plain, CodecOptions())
        # Cliente "da aplicação": decifra a resposta sozinho; bypass_query_analysis porque a
        # cifragem das queries é explícita (sem crypt_shared).
        self._uri = uri
        self.app = self._app_client()
        self.keys: dict[str, object] = {}

    def _app_client(self):
        from pymongo import AsyncMongoClient
        from pymongo.encryption_options import AutoEncryptionOpts

        return AsyncMongoClient(self._uri, appname="intelligence-layer-qe-app",
                                auto_encryption_opts=AutoEncryptionOpts(
                                    self.kms, self.key_vault_ns, bypass_query_analysis=True))

    async def close(self) -> None:
        await self.ce.close()
        await self.app.close()
        await self.plain.close()

    async def reset(self) -> None:
        db = self.plain[self.db_name]
        coll = await db.get_collection(COLLECTION).options()
        if coll:
            await db.drop_collection(COLLECTION, encrypted_fields=ENCRYPTED_FIELDS)
        await db[KEY_VAULT_COLLECTION].drop()
        await db[KEY_VAULT_COLLECTION].create_index(
            "keyAltNames", unique=True,
            partialFilterExpression={"keyAltNames": {"$exists": True}})
        fields = {"fields": [dict(f, keyId=None) for f in ENCRYPTED_FIELDS["fields"]]}
        _, created = await self.ce.create_encrypted_collection(db, COLLECTION, fields, "local")
        self.keys = {f["path"]: f["keyId"] for f in created["fields"]}
        # user_key em claro: escopo do tenant aplicado NO SERVIDOR em toda consulta
        await db[COLLECTION].create_index("user_key")
        # O cliente que decifra guarda o encryptedFields da collection em cache: depois de
        # recriá-la com DEKs novas, o cliente velho insere com as keyIds antigas e o servidor
        # recusa ("Mismatched keyId", code 6726301). Cliente novo a cada reset.
        await self.app.close()
        self.app = self._app_client()

    async def _enc(self, path: str, value, *, query: bool = False):
        from pymongo.encryption import Algorithm, QueryType
        from pymongo.encryption_options import RangeOpts

        if path == "renda_mensal":
            return await self.ce.encrypt(
                int(value), Algorithm.RANGE, self.keys[path], contention_factor=CONTENTION,
                range_opts=RangeOpts(sparsity=RANGE_SPARSITY, trim_factor=RANGE_TRIM,
                                     min=RENDA_MIN, max=RENDA_MAX))
        return await self.ce.encrypt(
            str(value), Algorithm.INDEXED, self.keys[path], contention_factor=CONTENTION,
            query_type=QueryType.EQUALITY if query else None)

    async def insert_profiles(self, profiles=PROFILES) -> int:
        coll = self.app[self.db_name][COLLECTION]
        for p in profiles:
            doc = {"user_key": p["user_key"], "resumo_mascarado": masked_summary(p)}
            for path in ENCRYPTED_PATHS:
                doc[path] = await self._enc(path, p[path])
            await coll.insert_one(doc)
        return len(profiles)

    async def find_by(self, path: str, value, user_key: str | None = None) -> list[dict]:
        """Igualdade em campo cifrado; `user_key` (claro) restringe o tenant no servidor."""
        if path not in ("cpf", "telefone"):
            raise ValueError("igualdade só em cpf/telefone")
        if not isinstance(value, str) or not value:
            return []  # nunca vira operador: só string vai para o encrypt
        flt = {path: await self._enc(path, value, query=True)}
        if user_key is not None:
            flt["user_key"] = user_key
        return await self.app[self.db_name][COLLECTION].find(flt, {"__safeContent__": 0}) \
            .to_list(20)

    async def find_renda_between(self, low: int, high: int,
                                 user_key: str | None = None) -> list[dict]:
        from pymongo.encryption import Algorithm, QueryType
        from pymongo.encryption_options import RangeOpts

        low, high = max(RENDA_MIN, int(low)), min(RENDA_MAX, int(high))
        expr = await self.ce.encrypt_expression(
            {"$and": [{"renda_mensal": {"$gte": low}}, {"renda_mensal": {"$lte": high}}]},
            Algorithm.RANGE, self.keys["renda_mensal"], query_type=QueryType.RANGE,
            contention_factor=CONTENTION,
            range_opts=RangeOpts(sparsity=RANGE_SPARSITY, trim_factor=RANGE_TRIM,
                                 min=RENDA_MIN, max=RENDA_MAX))
        flt = dict(expr)
        if user_key is not None:
            flt = {"$and": [flt, {"user_key": user_key}]}
        return await self.app[self.db_name][COLLECTION].find(flt, {"__safeContent__": 0}) \
            .to_list(20)

    async def raw(self, user_key: str) -> dict | None:
        """O que o DBA/backup vê: Binary subtype 6 nos campos cifrados."""
        return await self.plain[self.db_name][COLLECTION].find_one({"user_key": user_key})


async def run_demo(demo: QEDemo) -> dict:
    from bson.binary import Binary

    await demo.reset()
    inserted = await demo.insert_profiles()
    raw = await demo.raw("cliente-demo")
    cipher = {p: isinstance(raw.get(p), Binary) and raw[p].subtype == 6 for p in ENCRYPTED_PATHS}
    by_cpf = await demo.find_by("cpf", "529.982.247-25")
    by_tel = await demo.find_by("telefone", "(11) 99876-5432")
    wrong_tenant = await demo.find_by("cpf", "529.982.247-25", user_key="ana.vendas")
    absent = await demo.find_by("cpf", "000.000.000-00")
    renda = await demo.find_renda_between(4_000, 9_000)
    return {
        "database": demo.db_name, "inserted": inserted,
        "stored_as_ciphertext": cipher,
        "masked_summary_has_cpf": "529.982.247-25" in (raw.get("resumo_mascarado") or ""),
        "equality_cpf": [d["user_key"] for d in by_cpf],
        "equality_cpf_decrypted": [d["cpf"] for d in by_cpf],
        "equality_telefone": [d["user_key"] for d in by_tel],
        "equality_wrong_tenant": [d["user_key"] for d in wrong_tenant],
        "equality_absent": len(absent),
        "range_renda_4k_9k": sorted(d["user_key"] for d in renda),
    }


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    try:
        import pymongocrypt  # noqa: F401
    except ImportError:
        print("[qe] BLOQUEADO: falta pymongocrypt — pip install -r requirements-qe.txt")
        return 3
    main_db, _ = use_test_databases(what="demo de Queryable Encryption")
    demo = QEDemo(os.environ["MONGODB_URI"], main_db, load_master_key())
    try:
        report = await run_demo(demo)
    finally:
        await demo.close()
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    ok = all(report["stored_as_ciphertext"].values()) and report["equality_cpf"] == ["cliente-demo"] \
        and report["equality_wrong_tenant"] == [] and report["equality_absent"] == 0 \
        and report["range_renda_4k_9k"] == ["ana.vendas", "cliente-demo"] \
        and not report["masked_summary_has_cpf"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
