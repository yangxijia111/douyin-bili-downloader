"""Read-only connection checks distinguish WeChat from unrelated proxy clients."""

from unittest.mock import patch

from channels.connection import ConnectionMonitor, summarize_connections


def test_wechat_using_clash_does_not_count_as_sniffer_connection():
    processes = {10: "Weixin.exe", 11: "WeChatAppEx.exe", 20: "chrome.exe"}
    connections = [(11, "127.0.0.1", 7897), (20, "127.0.0.1", 8899)]
    state = summarize_connections(processes, connections, 8899)
    assert state["wechat_running"] is True
    assert state["proxy_connected"] is False
    assert state["other_proxy_ports"] == [7897]


def test_ipv6_loopback_and_classic_wechat_are_supported():
    state = summarize_connections(
        {10: "WeChat.exe", 11: "wechatappex.EXE"},
        [(10, "::1", 8899), (11, "::ffff:127.0.0.1", 8899)], 8899,
    )
    assert state["proxy_connected"] is True
    assert state["other_proxy_ports"] == []


def test_remote_media_connections_are_not_reported_as_another_local_proxy():
    state = summarize_connections({10: "Weixin.exe"}, [(10, "1.2.3.4", 443)], 8899)
    assert state["proxy_connected"] is False
    assert state["other_proxy_ports"] == []
    assert state["active_connections"] == 1


async def test_polling_is_nonblocking_and_single_flight():
    with patch("channels.connection.read_connection_state", return_value={
        "supported": True, "wechat_running": True, "proxy_connected": False,
        "other_proxy_ports": [7897],
    }) as read:
        monitor = ConnectionMonitor()
        monitor.refresh(8899)
        monitor.refresh(8899)
        assert monitor.snapshot()["checking"] is True
        await monitor.wait_idle()
        assert read.call_count == 1
        assert monitor.snapshot()["other_proxy_ports"] == [7897]
        monitor.refresh(8899)
        assert read.call_count == 1
        await monitor.aclose()


async def test_failed_read_is_unknown_instead_of_no_wechat():
    with patch("channels.connection.read_connection_state", side_effect=OSError("denied")):
        monitor = ConnectionMonitor()
        monitor.refresh(8899)
        await monitor.wait_idle()
        assert monitor.snapshot()["proxy_connected"] is None
        assert monitor.snapshot()["wechat_running"] is None
        await monitor.aclose()


async def test_port_change_invalidates_cached_result():
    with patch("channels.connection.read_connection_state", return_value={
        "supported": True, "proxy_connected": True,
    }) as read:
        monitor = ConnectionMonitor()
        monitor.refresh(8899)
        await monitor.wait_idle()
        monitor.refresh(8900)
        await monitor.wait_idle()
        assert read.call_count == 2
        assert read.call_args.args == (8900,)
        await monitor.aclose()
