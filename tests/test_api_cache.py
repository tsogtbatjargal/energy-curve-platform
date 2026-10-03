import redis

from energy_curves.api.cache import BYPASS, HIT, MISS, VersionCache

M = "a" * 64  # manifest SHA-256


class FlakyClient:
    """A Valkey stand-in that counts calls and can be switched off."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.calls = 0
        self.down = False

    def get(self, key: str) -> bytes | None:
        self.calls += 1
        if self.down:
            raise redis.ConnectionError("down")
        return self.data.get(key)

    def set(self, key: str, value: str, ex: int) -> None:
        self.calls += 1
        if self.down:
            raise redis.ConnectionError("down")
        self.data[key] = value.encode()


def test_hit_after_miss_and_new_version_is_a_new_key() -> None:
    cache = VersionCache(FlakyClient())  # type: ignore[arg-type]
    loads = []

    def load() -> dict[str, int]:
        loads.append(1)
        return {"n": len(loads)}

    assert cache.get_or_load(1, M, "curves", {}, load) == ({"n": 1}, MISS)
    assert cache.get_or_load(1, M, "curves", {}, load) == ({"n": 1}, HIT)
    assert cache.get_or_load(2, M, "curves", {}, load) == ({"n": 2}, MISS)
    assert cache.get_or_load(2, M, "curves", {"x": 1}, load) == ({"n": 3}, MISS)  # params in key
    other = "b" * 64  # same version, another dataset history
    assert cache.get_or_load(2, other, "curves", {"x": 1}, load) == ({"n": 4}, MISS)


def test_an_outage_costs_one_attempt_per_cooldown() -> None:
    client, now = FlakyClient(), [0.0]
    cache = VersionCache(client, cooldown_s=5.0, clock=lambda: now[0])  # type: ignore[arg-type]
    client.down = True
    for _ in range(3):
        assert cache.get_or_load(1, M, "curves", {}, lambda: {"ok": True}) == ({"ok": True}, BYPASS)
    assert client.calls == 1  # later requests skip Valkey during the cooldown
    client.down = False
    now[0] = 5.1
    assert cache.get_or_load(1, M, "curves", {}, lambda: {"ok": True})[1] == MISS


def test_a_failed_write_still_returns_the_loaded_value() -> None:
    client = FlakyClient()
    cache = VersionCache(client)  # type: ignore[arg-type]
    client.set = lambda *a, **k: (_ for _ in ()).throw(redis.TimeoutError("slow"))  # type: ignore[method-assign]
    assert cache.get_or_load(1, M, "curves", {}, lambda: [1, 2]) == ([1, 2], BYPASS)


def test_no_client_means_no_cache() -> None:
    assert VersionCache(None).get_or_load(1, M, "c", {}, lambda: 3) == (3, BYPASS)
