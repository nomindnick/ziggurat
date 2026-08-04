"""Outbound push delivery (item 3.6).

The SINGLE egress choke point for anything that leaves the box for the operator's
phone. Every publish goes through ``outbound.publish``, which runs the Rule-5
outbound scrub (``assert_publishable``) FIRST. No other module POSTs to ntfy —
enforced by a test that greps the tree.
"""

from ziggurat.push.outbound import (
    NtfyConfig,
    OutboundBoundaryError,
    PublishResult,
    assert_publishable,
    league_private_strings,
    load_ntfy_config,
    publish,
)

__all__ = [
    "NtfyConfig",
    "OutboundBoundaryError",
    "PublishResult",
    "assert_publishable",
    "league_private_strings",
    "load_ntfy_config",
    "publish",
]
