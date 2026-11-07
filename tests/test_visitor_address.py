from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from videocap.ui.context import visitor_address


@pytest.mark.parametrize(
    ("ip_address", "is_cloud", "expected"),
    [
        ("93.184.216.34", False, "93.184.216.34"),
        ("93.184.216.34", True, "93.184.216.34"),
        # A LAN visitor's address tells the devices of a home apart.
        ("192.168.1.20", False, "192.168.1.20"),
        # Behind the cloud's proxy, a private address may be the proxy's, shared by everyone.
        ("10.12.0.5", True, ""),
        ("127.0.0.1", False, ""),
        ("::1", False, ""),
        (None, False, ""),
        ("", False, ""),
        ("not an address", False, ""),
        # Under AppTest, st.context.ip_address is a mock.
        (MagicMock(), False, ""),
    ],
)
def test_only_an_address_that_tells_visitors_apart_counts(ip_address: object, is_cloud: bool, expected: str) -> None:
    assert visitor_address(ip_address, is_cloud) == expected
