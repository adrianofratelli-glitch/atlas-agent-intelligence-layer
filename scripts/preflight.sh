#!/usr/bin/env bash
# Pré-voo da demo. Rode ANTES de toda apresentação — leva ~15s.
#
#   ./scripts/preflight.sh            # completo (inclui MCP e uma chamada de LLM)
#   ./scripts/preflight.sh --quick    # só dados e configuração, sem custo de token
#
# Saída: 0 pronto · 1 há falha bloqueante · 2 pronto com ressalvas.
set -uo pipefail
cd "$(dirname "$0")/../backend"
exec .venv/bin/python scripts/preflight.py "$@"
