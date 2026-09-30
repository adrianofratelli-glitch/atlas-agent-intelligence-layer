#!/usr/bin/env bash
# Recria os venvs desta PoV do zero. Idempotente.
#
# São TRÊS, de propósito — os dois auxiliares existem porque as dependências
# deles brigam com o venv principal (verificado com `uv pip install --dry-run`):
#
#   backend/.venv   Python 3.14  runtime da PoV + pov-shared[tracing]
#   .venv-eval      Python 3.12  pov-shared[eval] (Ragas): trocaria anthropic 0.109 -> 1.8 (major)
#   .venv-memory    Python 3.12  mem0ai + fastembed: trocam jiter/protobuf e puxam openai+qdrant
#
# Os auxiliares são gitignorados; este script é o que permite reproduzi-los.
# Nenhum deles é necessário para rodar a demo — só para o eval com Ragas e para
# o benchmark de memória com Mem0.
set -euo pipefail

cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo "uv não encontrado (brew install uv)"; exit 1; }

echo "==> backend/.venv (runtime + pov-shared[tracing])"
[ -d backend/.venv ] || uv venv backend/.venv
uv pip install --python backend/.venv/bin/python -r backend/requirements.txt
uv pip install --python backend/.venv/bin/python -e "../_shared[tracing]"
uv pip check --python backend/.venv/bin/python

echo "==> .venv-eval (pov-shared[eval] — Ragas)"
[ -d .venv-eval ] || uv venv --python 3.12 .venv-eval
uv pip install --python .venv-eval/bin/python -e "../_shared[eval]"
uv pip check --python .venv-eval/bin/python

echo "==> .venv-memory (Mem0 + fastembed, para scripts/memory_benchmark.py --backend mem0)"
[ -d .venv-memory ] || uv venv --python 3.12 .venv-memory
uv pip install --python .venv-memory/bin/python \
  "mem0ai==2.1.0" fastembed "pymongo>=4.17,<5" "python-dotenv==1.*" "anthropic>=0.109"
uv pip check --python .venv-memory/bin/python

echo
echo "OK. Testes:      cd backend && .venv/bin/python -m unittest discover -s tests"
echo "    Caos:        cd backend && CHAOS=1 .venv/bin/python scripts/chaos_suite.py"
echo "    Eval:        cd backend && .venv/bin/python eval_agent.py"
echo "    Mem0 bench:  cd backend && ../.venv-memory/bin/python scripts/memory_benchmark.py --backend mem0"
