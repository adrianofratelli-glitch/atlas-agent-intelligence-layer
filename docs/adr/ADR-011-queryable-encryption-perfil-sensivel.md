# ADR-011: Queryable Encryption para PII do perfil — isolada, fora do caminho do agente

**Status:** Aceito
**Data:** 2026-10-16
**Contexto do cliente/conta:** PoV Intelligence Layer (demonstração standalone)

## Contexto
O agente mascara PII antes de LLM, cache, memória e trace (`pov-shared mask_pii`), mas o dado
de identificação (CPF, telefone, renda) ainda precisaria morar em algum lugar. A pergunta de
cliente é "o DBA, o operador da nuvem ou quem levar o backup consegue ler o CPF?".

Documentação lida antes de codar:
- https://www.mongodb.com/docs/manual/core/queryable-encryption/fundamentals/manual-encryption/
  (explicit encryption: algoritmos `indexed`/`range`, `contention` obrigatório, `queryType` nas
  leituras, `bypassQueryAnalysis` para decifrar sem análise de query);
- https://www.mongodb.com/docs/manual/core/queryable-encryption/qe-create-encrypted-collection/
  (`encryptedFields`, `queryType: "equality"` para string, `"range"` com `min`/`max`/`sparsity`/
  `trimFactor` para tipos numéricos e datas).

## Decisão
1. **Collection nova `customer_sensitive_profile`, só no banco de teste**, criada por
   `ClientEncryption.create_encrypted_collection` (uma DEK por campo):
   `cpf` e `telefone` (string, igualdade), `renda_mensal` (int, faixa 0–1.000.000). Range em
   CPF/telefone não existe: a doc só oferece range para numérico/data.
2. **Padrão: texto mascarado vetorizável + PII cifrada.** `user_key` e `resumo_mascarado`
   (texto com PII trocada por `<CPF>`/`<TELEFONE>` pelo pov-shared) ficam em claro. Campo cifrado
   não é vetorizável nem indexável pelo Atlas Search: o servidor só vê ciphertext. Por isso nada
   que o MCP Server, o `$vectorSearch` ou o `$search` precisam ler é cifrado.
3. **Explicit encryption, sem `crypt_shared`.** `AsyncClientEncryption.encrypt`/`encrypt_expression`
   e um cliente com `bypass_query_analysis=True` que decifra a resposta sozinho. Dependência só
   do `pymongocrypt` (`backend/requirements-qe.txt`), fora do runtime da demo.
4. **KMS local só para demo.** Chave mestra de 96 bytes em `backend/secrets/` (gitignored,
   criada com 0600). Produção: AWS/Azure/GCP KMS; a PoV irmã `atlas-queryable-encryption` mostra
   auto-encryption com `crypt_shared` e o argumento completo.
5. **Escopo de tenant no servidor:** toda consulta leva `user_key` (claro) junto do predicado
   cifrado.
6. **Fora da UI.** Script standalone (`backend/scripts/qe_sensitive_profile.py`) + testes; a
   demo ao vivo não muda.

## Alternativas consideradas
- **Cifrar campos de `agent_memory` ou do cache.** Quebraria o `$vectorSearch`/BM25 e o MCP
  (que leria Binary subtype 6). Rejeitado.
- **CSFLE determinístico.** Só igualdade e vaza frequência; QE dá igualdade e faixa com
  ciphertext aleatório.
- **Auto-encryption com `crypt_shared`.** Melhor ergonomia, mas exige baixar a biblioteca
  enterprise; para um script de demonstração, explicit encryption basta.

## Consequências
- Medido no POC_test: os 3 campos gravados como Binary subtype 6; igualdade por CPF e telefone
  devolve o titular certo e decifrado; CPF certo com `user_key` errado devolve vazio; faixa
  4.000–9.000 devolve exatamente os 2 perfis; resumo mascarado sem CPF.
- Achado: o cliente que decifra guarda o `encryptedFields` em cache; recriar a collection com
  DEKs novas exige cliente novo (senão `Mismatched keyId`, code 6726301).
- Igualdade é exata: um caractere invisível no valor consultado não casa (a normalização é do
  chamador).
