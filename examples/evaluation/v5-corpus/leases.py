from dataclasses import dataclass


@dataclass
class Lease:
    owner: str
    expires_at: float


class LeaseBook:
    def __init__(self):
        self._leases: dict[str, Lease] = {}
        self._heartbeats: dict[str, float] = {}

    def grant(self, resource: str, owner: str, now: float, ttl: float) -> Lease:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        previous = self._leases.get(resource)
        if previous is not None and now < previous.expires_at:
            raise ValueError("resource already leased")
        lease = Lease(owner, now + ttl)
        self._leases[resource] = lease
        self._heartbeats[resource] = now
        return lease

    def _active(self, resource: str, owner: str, now: float) -> Lease:
        lease = self._leases[resource]
        if lease.owner != owner or now >= lease.expires_at:
            raise PermissionError("owner mismatch or expired lease")
        return lease

    def heartbeat(self, resource: str, owner: str, now: float) -> None:
        self._active(resource, owner, now)
        self._heartbeats[resource] = now

    def renew(self, resource: str, owner: str, now: float, ttl: float) -> float:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        lease = self._active(resource, owner, now)
        lease.expires_at = now + ttl
        return lease.expires_at

    def healthy(self, resource: str, now: float, heartbeat_window: float) -> bool:
        if heartbeat_window < 0:
            raise ValueError("heartbeat window must not be negative")
        lease = self._leases.get(resource)
        if lease is None or now >= lease.expires_at:
            return False
        return now - self._heartbeats[resource] <= heartbeat_window
