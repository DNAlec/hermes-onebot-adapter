from onebot_adapter.app import is_loopback_bind, resolve_bind_hosts


def test_resolve_bind_hosts_default_loopback():
    assert resolve_bind_hosts("127.0.0.1", None, None, None) == (
        "127.0.0.1", "127.0.0.1", "127.0.0.1",
    )


def test_resolve_bind_hosts_onebot_only_exposes_reverse_ws():
    assert resolve_bind_hosts("127.0.0.1", "0.0.0.0", None, None) == (
        "0.0.0.0", "127.0.0.1", "127.0.0.1",
    )


def test_resolve_bind_hosts_bare_host_exposes_all():
    assert resolve_bind_hosts("0.0.0.0", None, None, None) == (
        "0.0.0.0", "0.0.0.0", "0.0.0.0",
    )


def test_is_loopback_bind():
    assert is_loopback_bind("127.0.0.1")
    assert is_loopback_bind("::1")
    assert is_loopback_bind("localhost")
    assert not is_loopback_bind("0.0.0.0")
    assert not is_loopback_bind("::")
    assert not is_loopback_bind("192.168.1.1")
