# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S26 subscription paywall (OPTIONAL, V1.x, default OFF).

Net-new module that gates selected VOD/live behind an email magic-link or a
Stripe subscription. **Default OFF** — a station that never enables it sees
empty tables + zero behavior change (DC-1). Card data NEVER touches CivicCast
(Stripe-hosted Checkout + Customer Portal only — PCI SAQ-A scope, DC-4).

Three durable tables (migration ``0059_paywall_access``):

* ``paywall_configs`` — one row per station. The ``enabled`` flag is the
  master switch (DC-1); ``tiers`` is a JSON list of ``{tier_id, name,
  price_id, interval}`` rows that map a CivicCast tier to a Stripe price
  id. The provider is pinned to ``stripe`` today (single-provider literal
  with room for ``mock`` in tests).
* ``access_grants`` — one row per email + scope pair. A grant says "this
  email has access to this asset / series / *" and was granted via
  ``subscription``, ``comp`` (operator-issued comp), or ``magic_link``
  (one-shot redemption). The optional ``expires_at`` lets comp grants
  expire without surviving forever.
* ``subscriptions`` — one row per Stripe subscription. Mirrors Stripe state
  (Stripe is the source of truth; the signed webhook reconciles this
  table). ``status`` is one of ``active``/``past_due``/``canceled``/
  ``incomplete``; the gate considers ``active`` + ``past_due`` (with
  grace) as "has access" — anything else means "no access".

Stripe-hosted only: Checkout sessions and the Customer Portal are created
on Stripe's side; CivicCast only receives the redirect URLs and the signed
webhook events. Webhook signature verification is enforced at the router
(DC-3); a bad signature is a 401, never a silent 200.

Magic-link tokens are short-lived (default 15 min), HMAC-signed with the
station's per-config secret, and single-use (DC-5) — redemption marks the
underlying grant ``magic_link`` and the token cannot be replayed.

The package follows the eas / metadata / reporting / underwriting / agenda
layout — bound-param SQL, lazy session factory, role-gated staff routes
(``setup_admin`` for config), unauthenticated public routes with their own
rate limiting (slice 3 wiring), 503 over silent 200 when DI is unwired.
"""
