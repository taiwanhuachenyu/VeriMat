from src.service.rate_limit import PrincipalRateLimiter


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_token_bucket_is_principal_scoped_and_refills_deterministically():
    clock = Clock()
    limiter = PrincipalRateLimiter(requests_per_minute=60, burst=2, clock=clock)
    assert limiter.allow(tenant_id="tenant-a", principal_id="one") == (True, 0)
    assert limiter.allow(tenant_id="tenant-a", principal_id="one") == (True, 0)
    allowed, retry_after = limiter.allow(tenant_id="tenant-a", principal_id="one")
    assert not allowed and retry_after == 1
    assert limiter.allow(tenant_id="tenant-a", principal_id="two") == (True, 0)
    clock.now = 1.0
    assert limiter.allow(tenant_id="tenant-a", principal_id="one") == (True, 0)
    assert limiter.configured_bucket_count() == 2
