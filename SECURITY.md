# Security Policy / Política de segurança

This repository is a proof-of-value demo for MongoDB Atlas. It is not a production service.
Este repositório é uma PoV (prova de valor) de MongoDB Atlas, não um serviço em produção.

## Reporting a vulnerability / Como reportar uma vulnerabilidade

Please **do not open a public issue**. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository.
Por favor, **não abra issue pública**: use **Security → Report a vulnerability** neste repositório (canal privado).

Include what you found, how to reproduce it and the affected file or endpoint. You will get an answer within
5 business days. / Inclua o que encontrou, como reproduzir e o arquivo ou endpoint afetado; a resposta vem em até
5 dias úteis.

## Scope / Escopo

- Code in this repository (backend, frontend, scripts, Docker and CI files).
- Out of scope: the demo data (synthetic), third-party services and MongoDB Atlas itself
  (report those to MongoDB: https://www.mongodb.com/security).
- No credentials are stored in this repository; configuration comes from environment variables.
  If you find a secret here, report it right away.
