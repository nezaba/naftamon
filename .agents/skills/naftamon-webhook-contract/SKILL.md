---
name: naftamon-webhook-contract
description: Interpreta y valida el JSON que realmente emite el Webhook Gateway de NaftaMon Desktop para diseñar ingestión, fixtures o compatibilidad.
---

# Contrato del Webhook Gateway de NaftaMon

Antes de definir campos, identidad o semántica de alertas, lee [web/docs/webhook-contract.md](../../../web/docs/webhook-contract.md).

- Basa cualquier esquema en esa documentación y, si hace falta, verifica src/webhookgateway.cpp y src/core.h.
- El POST periódico es un snapshot, no un evento. El mensaje de prueba no tiene alerts/servers y no debe modificar el estado.
- complete false puede contener servidores ok junto con error/pending. Reconciliar solo servidores ok; conservar alertas de error/pending como no confirmadas.
- alerts vacío solo significa cero alertas cuando el snapshot completo es exitoso.
- source es una constante, no un ID de instalación. server es un nombre editable; no supongas IDs estables.
- Usa la etiqueta de ruta propuesta para agrupar instalaciones mientras no exista instance_id. Esa etiqueta no autentica al emisor.
- No añadas campos ficticios al contrato ni afirmes garantías que el Gateway no da. Documenta cualquier extensión como propuesta compatible y opcional.
