"""What a link's host says of where it leads: this machine and its own network, however one of their addresses is
written, and the headers of a request put together from several sources."""

from __future__ import annotations

import pytest

from videocap.sites import host_of, is_private_address, is_private_host, merge_http_headers, spelled_ipv4


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
        # The other ways to write 127.0.0.1, which what connects takes for it.
        "127.1", "2130706433", "0x7f.0.0.1", "0177.0.0.1", "0x7f000001", "017700000001", "127.0.0.1.",
        "::1", "::ffff:127.0.0.1", "fe80::1", "fc00::1", "fe80::1%25eth0",
        "localhost", "LOCALHOST", "localhost.", "a.localhost", "nas", "printer.local", "db.internal", "router.lan", "x.home.arpa",
        # Ends in a number and is no address: nothing public is called that.
        "1.2.3.4.5", "999.1.1.1", "1.1.1.0x100",
    ],
)  # fmt: skip
def test_a_host_of_this_machine_or_its_network_is_told_however_it_is_written(host: str) -> None:
    assert is_private_host(host)


@pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1", "site.example", "example.com", "example.com.", "a.b.co", "2606:4700:4700::1111", "134744072", "010.010.010.010", ""])
def test_a_host_of_the_public_internet_is_not_taken_for_one_of_this_network(host: str) -> None:
    assert not is_private_host(host)


def test_a_host_made_of_numbers_stands_for_the_address_a_browser_reads_it_as() -> None:
    assert [spelled_ipv4(host) for host in ("127.1", "2130706433", "0x7f.0.0.1", "0177.0.0.1", "1.2.3", "0")] == ["127.0.0.1"] * 4 + ["1.2.0.3", "0.0.0.0"]
    assert spelled_ipv4("site.example") is None and spelled_ipv4("1.2.3.4.5") == "" and spelled_ipv4("256.1.1.1") == ""


@pytest.mark.parametrize("url", ["http://[::1]:8080/a", "http://LOCALHOST./a", "http://127.1/a", "http://0x7f.0.0.1:81/a", "http://user@2130706433/a", "http://[::ffff:7f00:1]/a"])
def test_an_address_is_told_by_the_host_that_a_link_is_read_to_have(url: str) -> None:
    assert is_private_host(host_of(url))


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.1.1", "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.5", "ff02::1", "not an address"])
def test_an_address_that_is_not_of_the_public_internet_is_private(address: str) -> None:
    assert is_private_address(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_an_address_of_the_public_internet_is_not(address: str) -> None:
    assert not is_private_address(address)


def test_headers_are_merged_with_the_later_source_winning_whatever_the_case_of_a_name() -> None:
    merged = merge_http_headers({"referer": "https://page.example/", "Origin": "https://page.example"}, {"Referer": " https://owner.example/ "})
    assert merged == {"Referer": "https://owner.example/", "Origin": "https://page.example"}
