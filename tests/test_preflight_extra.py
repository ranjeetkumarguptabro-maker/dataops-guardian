import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dataguardian.preflight import is_http_url, main, run_preflight, save_report


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "ftp://example.com",
        "https://user:secret@example.com/health",
        "https://example.com/health#fragment",
        "https://example.com:invalid/health",
        "https://example.com:0/health",
        "/health",
    ],
)
def test_invalid_urls(url):
    assert is_http_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://example.com/health",
        " HTTPS://example.com/health ",
        "https://example.com:9200/health",
    ],
)
def test_valid_urls(url):
    assert is_http_url(url) is True


def test_non_numeric_timeout_skips_health():
    with patch("dataguardian.preflight.check_health") as health:
        report = run_preflight("https://example.com/health", timeout="invalid")
    assert report["status"] == "failed"
    health.assert_not_called()


def test_boolean_timeout_skips_health():
    with patch("dataguardian.preflight.check_health") as health:
        report = run_preflight("https://example.com/health", timeout=True)
    assert report["status"] == "failed"
    health.assert_not_called()


def test_infinite_timeout_fails():
    report = run_preflight("https://example.com/health", timeout=float("inf"))
    assert report["status"] == "failed"


def test_missing_version_source_fails():
    with patch(
        "dataguardian.preflight.check_health",
        return_value={"status": "healthy"},
    ):
        report = run_preflight("https://example.com/health", required_version="8.15.0")
    check = next(c for c in report["checks"] if c["name"] == "version_check")
    assert check["status"] == "failed"


def test_invalid_elasticsearch_url_skips_discovery():
    with (
        patch(
            "dataguardian.preflight.check_health", return_value={"status": "healthy"}
        ),
        patch("dataguardian.preflight.get_elasticsearch_version") as discovery,
    ):
        report = run_preflight(
            "https://example.com/health",
            required_version="8.15.0",
            elasticsearch_url="https://user:secret@example.com:9200",
        )
    discovery.assert_not_called()
    check = next(c for c in report["checks"] if c["name"] == "version_check")
    assert check["status"] == "failed"


def test_invalid_timeout_skips_version_discovery():
    with (
        patch("dataguardian.preflight.check_health") as health,
        patch("dataguardian.preflight.get_elasticsearch_version") as discovery,
    ):
        report = run_preflight(
            "https://example.com/health",
            timeout=0,
            required_version="8.15.0",
            elasticsearch_url="https://example.com:9200",
        )
    assert report["status"] == "failed"
    health.assert_not_called()
    discovery.assert_not_called()


def test_discovery_failure_uses_fallback_message():
    with (
        patch(
            "dataguardian.preflight.get_elasticsearch_version",
            return_value={"status": "failed"},
        ),
        patch(
            "dataguardian.preflight.check_health",
            return_value={"status": "healthy"},
        ),
    ):
        report = run_preflight(
            "https://example.com/health",
            required_version="8.15.0",
            elasticsearch_url="https://example.com:9200",
        )
    check = next(c for c in report["checks"] if c["name"] == "version_check")
    assert check["message"] == "Elasticsearch version discovery failed"


def test_discovered_missing_version_is_compared():
    with (
        patch(
            "dataguardian.preflight.get_elasticsearch_version",
            return_value={"status": "passed", "version": None},
        ),
        patch(
            "dataguardian.preflight.check_version",
            return_value={"status": "failed", "message": "Version missing"},
        ) as compare,
        patch(
            "dataguardian.preflight.check_health",
            return_value={"status": "healthy"},
        ),
    ):
        report = run_preflight(
            "https://example.com/health",
            required_version="8.15.0",
            elasticsearch_url="https://example.com:9200",
        )
    compare.assert_called_once_with(None, "8.15.0")
    assert report["status"] == "failed"


def test_report_structure():
    with patch(
        "dataguardian.preflight.check_health",
        return_value={"status": "healthy"},
    ):
        report = run_preflight("https://example.com/health")
    assert report["component"] == "dataguardian-preflight"
    assert report["check_count"] == len(report["checks"])
    assert report["failed_count"] == sum(
        c["status"] == "failed" for c in report["checks"]
    )


def test_save_report_writes_json(tmp_path):
    report = {"status": "passed", "failed_count": 0}
    path = save_report(report, str(tmp_path))
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == report


def test_save_report_creates_nested_directory(tmp_path):
    directory = tmp_path / "nested" / "reports"
    path = save_report({"status": "passed"}, str(directory))
    assert path.exists()


def test_save_report_propagates_write_error(tmp_path):
    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            save_report({"status": "passed"}, str(tmp_path))


def test_cli_requires_url(monkeypatch):
    monkeypatch.delenv("TARGET_HEALTH_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["dataguardian-preflight"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_cli_returns_zero_when_passed(monkeypatch, tmp_path, capsys):
    report = {"status": "passed", "failed_count": 0, "checks": []}
    monkeypatch.setattr(
        "sys.argv",
        [
            "dataguardian-preflight",
            "--url",
            "https://example.com/health",
            "--report-dir",
            str(tmp_path),
        ],
    )
    with (
        patch("dataguardian.preflight.run_preflight", return_value=report),
        patch(
            "dataguardian.preflight.save_report", return_value=tmp_path / "report.json"
        ) as save,
    ):
        result = main()
    assert result == 0
    save.assert_called_once_with(report, str(tmp_path))
    assert json.loads(capsys.readouterr().out) == report


def test_cli_returns_one_when_failed(monkeypatch, tmp_path):
    report = {"status": "failed", "failed_count": 1, "checks": []}
    monkeypatch.setattr(
        "sys.argv",
        [
            "dataguardian-preflight",
            "--url",
            "https://example.com/health",
            "--report-dir",
            str(tmp_path),
        ],
    )
    with (
        patch("dataguardian.preflight.run_preflight", return_value=report),
        patch(
            "dataguardian.preflight.save_report", return_value=tmp_path / "report.json"
        ),
    ):
        assert main() == 1


def test_cli_returns_two_if_report_save_fails(monkeypatch, tmp_path):
    report = {"status": "passed", "failed_count": 0, "checks": []}
    monkeypatch.setattr(
        "sys.argv",
        [
            "dataguardian-preflight",
            "--url",
            "https://example.com/health",
            "--report-dir",
            str(tmp_path),
        ],
    )
    with (
        patch("dataguardian.preflight.run_preflight", return_value=report),
        patch("dataguardian.preflight.save_report", side_effect=OSError("disk full")),
    ):
        assert main() == 2
