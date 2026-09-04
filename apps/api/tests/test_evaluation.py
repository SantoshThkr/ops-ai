from app.evaluation import run_evaluation


def test_offline_evaluation_report_covers_day_eight_behaviors() -> None:
    report = run_evaluation()

    assert report.total == 15
    assert report.failed == 0
    assert report.passed == report.total
    assert report.pass_rate == 1.0
    assert {case.category for case in report.cases} == {
        "knowledge",
        "metrics",
        "safety",
        "incident",
        "mcp",
    }


def test_offline_evaluation_report_is_human_readable() -> None:
    output = run_evaluation().text()

    assert "total: 15" in output
    assert "pass_rate: 100.0%" in output
    assert "mcp/unknown_tool" in output
