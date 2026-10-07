"""Session diagnostics describe the current capture attempt; all system effects are mocked."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from channels.diagnostics import COUNTER_KEYS
from channels.patches import JsBundlePatch
from config import ConfigLoader
from server.channels import ChannelsSessionManager


class _KnownPatch(JsBundlePatch):
    def apply(self, body: str) -> str:
        return body + "/* compatibility patch */"


async def test_restart_clears_prior_capture_evidence_but_stop_keeps_last_snapshot(tmp_path):
    manager = ChannelsSessionManager(ConfigLoader(None), Mock())
    manager._cert_manager = SimpleNamespace(
        ca_cert_path=tmp_path / "absent.cer", ensure_ca=Mock(), is_installed=Mock(return_value=True),
    )
    manager._connection_monitor = SimpleNamespace(
        refresh=Mock(), snapshot=Mock(return_value={}), aclose=AsyncMock(),
    )
    interceptor = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    downloader = SimpleNamespace(aclose=AsyncMock())
    task_hub = SimpleNamespace(cancel_all=Mock(), wait_idle=AsyncMock())
    registry = manager.patch_registry
    registry.register(_KnownPatch(name="known", signature=("fixture",)))
    with ExitStack() as mocks:
        for replacement in (
            patch("server.channels.mitmproxy_available", return_value=True),
            patch("channels.proxy_recovery.recover_stale_proxy", return_value={"action": "none"}),
            patch("server.channels.ChannelsInterceptor", return_value=interceptor),
            patch("server.channels.SystemProxyManager"),
            patch("channels.downloader.ChannelsDownloader", return_value=downloader),
            patch("channels.task_hub.ChannelsTaskHub", return_value=task_hub),
            patch("channels.mse_capture.MseCaptureStore"),
            patch("channels.worker.run_auto_download_worker", new_callable=AsyncMock),
        ):
            mocks.enter_context(replacement)
        try:
            await manager.start()
            for counter in (
                "proxy_connections", "target_domain_connections", "html_pages_seen",
                "injected_pages",
            ):
                manager.diagnostics.incr(counter)
            manager.diagnostics.record_heartbeat(page_type="home", buttons_created=1)
            manager.diagnostics.record_feeds(1, strategy="page_network_hook")
            manager.diagnostics.record_host("channels.weixin.qq.com", source="http", target=True)
            registry.process("fixture.js", "fixture")
            assert (await manager.start())["diagnostics"]["stage"] == "feed_captured"

            stopped = await manager.stop()
            assert stopped["running"] is False
            assert stopped["diagnostics"]["stage"] == "feed_captured"

            restarted = await manager.start()
            assert restarted["diagnostics"]["stage"] == "no_proxy"
            assert all(restarted["diagnostics"]["counters"][key] == 0 for key in COUNTER_KEYS)
            assert restarted["diagnostics"]["observed_hosts"] == []
            assert restarted["diagnostics"]["strategy_stats"] == {}
            assert restarted["diagnostics"]["counters"]["patch_applied"] == 0
            registry.process("fixture.js", "fixture")
            assert manager.status()["diagnostics"]["counters"]["patch_applied"] == 1
        finally:
            await manager.stop()
