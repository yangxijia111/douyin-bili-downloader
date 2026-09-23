"""证书生命周期测试：生成 / 状态 / 安装 / 卸载（certutil 全程 mock）。

卸载的红线（P1）：
* 只允许删除**当前用户 Root 存储中指纹等于本机 CA** 的证书；
* 绝不按名称模糊删除、绝不触碰其它证书、绝不操作计算机级存储。
"""

from __future__ import annotations

import datetime
import subprocess
import sys
from unittest import mock

import pytest

from channels.interceptor import CertificateManager

cryptography = pytest.importorskip("cryptography")


@pytest.fixture(autouse=True)
def _pretend_windows(monkeypatch):
    """非 Windows 的 CI 上也验证这些 mock 化的 Windows 证书逻辑（不 skip）。"""
    if sys.platform != "win32":
        monkeypatch.setattr(sys, "platform", "win32")


def _make_ca(tmp_path) -> CertificateManager:
    """生成一张真实的自签 CA 证书放进临时 confdir（避免碰 ~/.mitmproxy）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "douyin-bili-downloader Test CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    confdir = tmp_path / ".mitmproxy"
    confdir.mkdir()
    (confdir / "mitmproxy-ca-cert.cer").write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
    )
    return CertificateManager(confdir)


def _run_result(returncode=0, stdout=b"", stderr=b""):
    proc = mock.Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestCertificateInfo:
    def test_info_fields(self, tmp_path):
        manager = _make_ca(tmp_path)
        with mock.patch.object(CertificateManager, "is_installed", return_value=True):
            info = manager.certificate_info()
        assert info["exists"] is True
        assert info["installed"] is True
        assert info["path"].endswith("mitmproxy-ca-cert.cer")
        assert info["sha256_fingerprint"] and len(info["sha256_fingerprint"]) == 64
        assert info["sha1_fingerprint"] and len(info["sha1_fingerprint"]) == 40
        assert "douyin-bili-downloader Test CA" in (info["subject"] or "")
        assert info["not_before"] and info["not_after"]
        # 有效期方向正确。
        assert info["not_after"] > info["not_before"]

    def test_info_missing_cert(self, tmp_path):
        manager = CertificateManager(tmp_path / "nope")
        info = manager.certificate_info()
        assert info["exists"] is False
        assert info["installed"] is False
        assert info["sha256_fingerprint"] is None

    def test_sha256_fingerprint(self, tmp_path):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        manager = _make_ca(tmp_path)
        expected = x509.load_pem_x509_certificate(
            manager.ca_cert_path.read_bytes()
        ).fingerprint(hashes.SHA256()).hex()
        assert manager.fingerprint_sha256() == expected


class TestUninstall:
    def test_non_windows_raises(self, tmp_path):
        manager = CertificateManager(tmp_path)
        with mock.patch("channels.interceptor.sys") as fake_sys:
            fake_sys.platform = "linux"
            with pytest.raises(RuntimeError):
                manager.uninstall()

    def test_missing_cert_file_reports_error(self, tmp_path):
        manager = CertificateManager(tmp_path / "empty")
        ok, detail = manager.uninstall()
        assert ok is False
        assert "指纹" in detail or "证书" in detail

    def test_uninstall_deletes_only_exact_fingerprint_from_user_store(self, tmp_path):
        manager = _make_ca(tmp_path)
        fp = manager.fingerprint_sha1()
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if "-delstore" in args:
                assert args[-1] == fp, "必须按精确指纹删除，不得按名称"
                assert "-user" in args and "Root" in args
                return _run_result(0)
            if "-user" in args:  # 查询当前用户存储
                still_there = "-delstore" not in calls[0] if calls else True
                return _run_result(0, stdout=fp.encode() if still_there else b"")
            return _run_result(0)  # 机器存储查询：空

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            ok, detail = manager.uninstall()
        assert ok is True
        assert any("-delstore" in c for c in calls)

    def test_uninstall_is_idempotent_when_not_installed(self, tmp_path):
        manager = _make_ca(tmp_path)
        with mock.patch.object(
            CertificateManager, "_store_output", return_value="no certs here"
        ):
            ok, detail = manager.uninstall()
        assert ok is True
        assert "未发现" in detail

    def test_uninstall_failure_reports_detail(self, tmp_path):
        manager = _make_ca(tmp_path)
        fp = manager.fingerprint_sha1()

        def fake_run(args, **kwargs):
            if "-delstore" in args:
                return _run_result(5, stderr="access denied".encode())
            return _run_result(0, stdout=fp.encode())

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            ok, detail = manager.uninstall()
        assert ok is False
        assert "access denied" in detail

    def test_uninstall_reports_machine_store_copy(self, tmp_path):
        """用户存储已清但机器存储仍有同指纹证书：成功但明确提示。"""
        manager = _make_ca(tmp_path)
        fp = manager.fingerprint_sha1()
        deleted = []

        def fake_run(args, **kwargs):
            if "-delstore" in args:
                deleted.append(True)
                return _run_result(0)
            if "-user" in args:
                return _run_result(0, stdout=b"" if deleted else fp.encode())
            return _run_result(0, stdout=fp.encode())  # 机器存储：仍有

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            ok, detail = manager.uninstall()
        assert ok is True
        assert deleted, "必须先执行过用户存储删除"
        assert "计算机" in detail or "管理员" in detail


class TestInstallSafety:
    def test_install_targets_user_store(self, tmp_path):
        manager = _make_ca(tmp_path)
        with mock.patch.object(subprocess, "run", return_value=_run_result(0)) as run:
            ok, _ = manager.install()
        assert ok is True
        args = run.call_args[0][0]
        assert args[:3] == ["certutil", "-addstore", "-user"]
        assert args[-1] == str(manager.ca_cert_path)
