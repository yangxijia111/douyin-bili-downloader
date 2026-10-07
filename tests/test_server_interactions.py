"""Offline regressions for user-facing submission and task controls."""

import asyncio

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from cli.link_input import expand_link_inputs, extract_urls_from_text  # noqa: E402
from config import ConfigLoader  # noqa: E402
from server.app import _fork_config, build_app  # noqa: E402
from server.jobs import DownloadJob, JobManager  # noqa: E402

DOUYIN = "https://www.douyin.com/video/123"
BILI = "https://www.bilibili.com/video/BV1GJ411x7h7"
COUNTS = {"total": 1, "success": 1, "failed": 0, "skipped": 0}


def make_app(tmp_path):
    config = ConfigLoader(None)
    config.update(path=str(tmp_path), database=False)
    return build_app(config)


def test_extract_all_links_in_source_order_and_expand_cli_share_text():
    text = f"分享 {DOUYIN}，B站 {BILI}。第三条 v.kuaishou.com/abc/\n{DOUYIN}"
    expected = [DOUYIN, BILI, "https://v.kuaishou.com/abc/"]
    assert extract_urls_from_text(text) == expected
    assert len(extract_urls_from_text(text, deduplicate=False)) == 4
    assert expand_link_inputs([text, BILI, "av123", "无效输入"]) == expected + ["av123", "无效输入"]


def test_extract_bv_inside_url_does_not_create_a_second_job():
    assert extract_urls_from_text(f"{BILI} 另一个 BV1GJ411x7h7") == [BILI, "BV1GJ411x7h7"]
    assert extract_urls_from_text("ftp://www.douyin.com/video/123") == []


def test_parse_endpoint_reports_multiple_links_duplicates_and_channels(tmp_path):
    app = make_app(tmp_path)
    channels = "https://weixin.qq.com/sph/test-id"
    with TestClient(app) as client:
        response = client.post("/api/v1/links/parse", json={"text": f"{DOUYIN} {BILI} {DOUYIN} {channels}"})
        assert response.status_code == 200
        body = response.json()
        assert body["urls"] == [DOUYIN, BILI, channels]
        assert body["count"] == 3
        assert body["duplicate_count"] == 1
        assert [item["platform"] for item in body["items"]] == ["douyin", "bilibili", "channels"]
        assert body["items"][2]["supported"] is False
        assert "视频号" in body["items"][2]["message"]


@pytest.mark.parametrize("url", [
    "   ", "请下载这个视频", f"{DOUYIN} {BILI}",
    "https://www.douyin.com/video/bad-id", "https://v.douyin.com/",
    "https://www.douyin.com.evil.com/video/123", "https://weixin.qq.com/sph/test-id",
])
def test_download_rejects_bad_input_without_creating_jobs(tmp_path, url):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/v1/download", json={"url": url})
        assert response.status_code == 400
        assert isinstance(response.json()["detail"], str)
        assert client.get("/api/v1/jobs").json()["jobs"] == []


@pytest.mark.parametrize("updates", [
    {"thread": 0}, {"thread": "five"}, {"rate_limit": 0},
    {"retry_times": -1}, {"mode": "post"}, {"mode": ["unsupported"]},
    {"comments": "yes"}, {"number": {"post": -1}}, {"start_time": "2026-02-30"},
    {"start_time": "2026-10-04", "end_time": "2026-10-03"},
])
def test_config_rejects_invalid_values_before_mutating_shared_state(tmp_path, updates):
    app = make_app(tmp_path)
    original = _fork_config(app.state.deps.config, None).config
    with TestClient(app) as client:
        response = client.put("/api/v1/config", json={"updates": updates})
        assert response.status_code == 400
        assert app.state.deps.config.config == original


def test_config_accepts_fractional_rate_and_preserves_zero_retries(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        response = client.put("/api/v1/config", json={"updates": {"rate_limit": 0.5, "retry_times": 0}})
        assert response.status_code == 200
        assert app.state.deps.retry_handler.max_retries == 0
        assert client.get("/api/v1/stats").json()["runtime"]["retry_times"] == 0


def test_download_rejects_invalid_platform_limit(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/v1/download", json={
            "url": BILI, "overrides": {"bilibili": {"number": {"user": -1}}},
        })
        assert response.status_code == 400
        assert "bilibili.number.user" in response.json()["detail"]


def test_validation_response_has_readable_message_and_original_detail(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        response = client.post("/api/v1/download", json={"overrides": []})
        assert response.status_code == 422
        body = response.json()
        assert isinstance(body["detail"], list)
        assert "url" in body["message"]


def test_retry_rejects_running_jobs(tmp_path):
    app = make_app(tmp_path)

    async def wait_forever(_url):
        await asyncio.Event().wait()

    app.state.job_manager.executor = wait_forever
    with TestClient(app) as client:
        job_id = client.post("/api/v1/download", json={"url": DOUYIN}).json()["job_id"]
        response = client.post(f"/api/v1/jobs/{job_id}/retry")
        assert response.status_code == 409
        assert len(client.get("/api/v1/jobs").json()["jobs"]) == 1
        client.post(f"/api/v1/jobs/{job_id}/cancel")


def test_job_config_snapshot_is_independent_without_overrides():
    base = ConfigLoader(None)
    forked = _fork_config(base, None)
    base.update(video=False, number={"post": 7})
    assert forked.get("video") is True
    assert forked.get("number")["post"] == 0


async def test_immediate_cancel_finalizes_a_task_before_it_starts():
    calls = []

    async def executor(url):
        calls.append(url)
        return COUNTS

    manager = JobManager(executor=executor)
    job = await manager.submit(DOUYIN)
    await manager.cancel(job.job_id)
    assert job.status == "cancelled"
    assert job.finished_at is not None
    assert job.cancel_requested is True
    assert calls == []


async def test_paused_pending_job_does_not_start_until_resumed():
    entered = asyncio.Event()

    async def executor(_url):
        entered.set()
        return COUNTS

    manager = JobManager(executor=executor)
    job = await manager.submit(DOUYIN)
    await manager.set_paused(job.job_id, True)
    await asyncio.sleep(0.01)
    assert job.status == "pending"
    assert not entered.is_set()
    assert manager._active_jobs == 0
    await manager.set_paused(job.job_id, False)
    await asyncio.wait_for(job._task, timeout=1)
    assert job.status == "success"


async def test_concurrency_reduction_applies_to_previously_queued_jobs():
    started = [asyncio.Event() for _ in range(3)]
    releases = [asyncio.Event() for _ in range(3)]

    async def executor(url):
        index = int(url)
        started[index].set()
        await releases[index].wait()
        return COUNTS

    manager = JobManager(executor=executor, max_concurrency=2)
    jobs = [await manager.submit(str(index)) for index in range(3)]
    await asyncio.wait_for(asyncio.gather(started[0].wait(), started[1].wait()), timeout=1)
    manager.set_max_concurrency(1)
    releases[0].set()
    await asyncio.wait_for(jobs[0]._task, timeout=1)
    await asyncio.sleep(0.01)
    assert not started[2].is_set()
    releases[1].set()
    await asyncio.wait_for(started[2].wait(), timeout=1)
    releases[2].set()
    await manager.shutdown()


async def test_shutdown_cancels_paused_jobs_and_copies_override_inputs():
    async def executor(_url, _overrides):
        return COUNTS

    overrides = {"number": {"post": 2}}
    manager = JobManager(executor=executor)
    job = await manager.submit(DOUYIN, overrides=overrides)
    overrides["number"]["post"] = 999
    await manager.set_paused(job.job_id, True)
    assert job.overrides["number"]["post"] == 2
    await asyncio.wait_for(manager.shutdown(), timeout=1)
    assert job.status == "cancelled"
    assert not job.paused


def test_cancelled_progress_reflects_partial_completion():
    job = DownloadJob("test", DOUYIN)
    job.total = 10
    job.processed = 2
    job.status = "cancelled"
    assert job.progress_percent() == 20
