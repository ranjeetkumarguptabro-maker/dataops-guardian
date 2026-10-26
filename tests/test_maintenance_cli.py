"""Tests for the maintenance command-line interface."""

import json

import pytest

from dataguardian import maintenance_cli


def test_main_success_returns_zero_and_prints_report(monkeypatch, capsys, tmp_path):
    expected_report = {
        "status": "dry_run",
        "operation": "rolling_restart",
        "changes_made": False,
    }
    captured = {}

    def fake_workflow(**kwargs):
        captured.update(kwargs)
        return expected_report

    monkeypatch.setattr(
        maintenance_cli,
        "run_maintenance_workflow",
        fake_workflow,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "dataops-guardian-maintenance",
            "--url",
            "http://localhost:8080/health",
            "--operation",
            "rolling_restart",
            "--report-dir",
            str(tmp_path),
        ],
    )

    result = maintenance_cli.main()

    assert result == 0
    assert captured["health_url"] == "http://localhost:8080/health"
    assert captured["operation"] == "rolling_restart"
    assert captured["dry_run"] is True
    assert captured["approved"] is False
    assert captured["report_dir"] == str(tmp_path)
    assert json.loads(capsys.readouterr().out) == expected_report


@pytest.mark.parametrize("status", ["failed", "blocked"])
def test_main_failed_or_blocked_returns_one(monkeypatch, capsys, status):
    report = {"status": status, "changes_made": False}

    monkeypatch.setattr(
        maintenance_cli,
        "run_maintenance_workflow",
        lambda **kwargs: report,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "dataops-guardian-maintenance",
            "--url",
            "http://localhost:8080/health",
            "--operation",
            "index_cleanup",
        ],
    )

    result = maintenance_cli.main()

    assert result == 1
    assert json.loads(capsys.readouterr().out) == report


def test_main_passes_optional_arguments(monkeypatch, capsys):
    captured = {}

    def fake_workflow(**kwargs):
        captured.update(kwargs)
        return {"status": "dry_run"}

    monkeypatch.setattr(
        maintenance_cli,
        "run_maintenance_workflow",
        fake_workflow,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "dataops-guardian-maintenance",
            "--url",
            "https://example.com/health",
            "--operation",
            "index_cleanup",
            "--timeout",
            "5",
            "--required-version",
            "8.12.0",
            "--actual-version",
            "8.12.0",
            "--elasticsearch-url",
            "https://example.com:9200",
            "--approve",
        ],
    )

    result = maintenance_cli.main()

    assert result == 0
    assert captured["timeout"] == 5.0
    assert captured["required_version"] == "8.12.0"
    assert captured["actual_version"] == "8.12.0"
    assert captured["elasticsearch_url"] == "https://example.com:9200"
    assert captured["approved"] is True
    assert captured["dry_run"] is True
    capsys.readouterr()


def test_execute_is_refused(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "dataops-guardian-maintenance",
            "--url",
            "http://localhost:8080/health",
            "--operation",
            "rolling_restart",
            "--execute",
            "--approve",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        maintenance_cli.main()

    assert exc_info.value.code == 2
