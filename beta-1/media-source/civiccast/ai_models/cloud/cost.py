# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Per-token USD cost + estimate (S13, decision A: $USD/token + estimate).

Money is :class:`decimal.Decimal`, never float — a per-token rate over realized
prompt+completion token counts. Local tiers (``cost_per_token_usd == 0``) cost
$0; cloud tiers are priced from the hard-coded catalog rate. The adapters return
the provider's realized token counts so the UI can show estimate-vs-actual.
"""

from __future__ import annotations

from decimal import Decimal

from civiccast.ai_models.catalog import catalog_tier, catalog_tier_for_feature
from civiccast.ai_models.models import AiFeature


def estimate_cost_usd(
    model_key: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    feature: AiFeature | None = None,
) -> Decimal:
    """Estimate the USD cost of a call to ``model_key`` over the given token counts.

    When ``feature`` is given the rate is resolved within THAT feature's catalog
    tiers (``catalog_tier_for_feature``) so cost can never bind to another feature's
    copy of a shared key (prior M6); without it the global first-match
    (``catalog_tier``) is used. Raises ``KeyError`` if the key is unknown (or not
    offered for ``feature``) and ``ValueError`` on negative token counts. Returns
    ``Decimal("0")`` for local (free) tiers.
    """

    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("token counts must not be negative")
    if feature is not None:
        tier = catalog_tier_for_feature(feature, model_key)
        if tier is None:
            raise KeyError(f"Unknown model key {model_key!r} for feature {feature!r}")
    else:
        tier = catalog_tier(model_key)  # raises KeyError on an unknown key
    rate = Decimal(str(tier.cost_per_token_usd))
    if rate == 0:
        return Decimal("0")
    total_tokens = Decimal(prompt_tokens + completion_tokens)
    return total_tokens * rate


__all__ = ["estimate_cost_usd"]
