"""Client-IP resolution. Pure string logic, no Request/framework dependency, so
both `app/api/deps.py::client_ip` (audit trail) and `app/core/rate_limit.py`
(limiter key) resolve the same address the same way without either importing
the other -- `app/core` sits below `app/api` in the layering and must not
depend on it.
"""

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def resolve_client_ip(peer_host: str | None, forwarded_for: str | None) -> str | None:
    """The direct peer, unless that peer is loopback -- then the header it set.

    X-Forwarded-For is attacker-controlled by default: any remote client can
    send it, and honouring it unconditionally would let them forge the IP on
    their own audit rows. But a TCP connection whose *source* address is
    loopback cannot be spoofed by a remote attacker -- only a process already
    running on this host can open one. No reverse proxy is deployed yet (Phase
    7 deferred it), so this only fires when something local calls the API
    directly. It is the seam that makes a same-host reverse proxy usable later
    without editing every caller: point it at 127.0.0.1 and it starts working.
    """
    if peer_host is None:
        return None
    if peer_host in _LOOPBACK_HOSTS and forwarded_for:
        # Leftmost entry is the original client in the standard convention.
        return forwarded_for.split(",")[0].strip() or peer_host
    return peer_host
