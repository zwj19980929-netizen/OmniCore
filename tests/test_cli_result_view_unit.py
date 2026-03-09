from utils.cli_result_view import build_cli_result_view


def test_build_cli_result_view_marks_waiting_for_approval_as_non_failure():
    view = build_cli_result_view(
        {
            "success": False,
            "status": "waiting_for_approval",
            "output": "1 task is prepared and waiting for approval.",
            "error": "",
        }
    )

    assert view["title"] == "WAITING FOR APPROVAL"
    assert view["border_style"] == "yellow"
    assert view["body"] == "1 task is prepared and waiting for approval."
    assert view["is_failure"] is False


def test_build_cli_result_view_falls_back_to_failed_for_real_errors():
    view = build_cli_result_view(
        {
            "success": False,
            "status": "error",
            "output": "",
            "error": "request timed out",
        }
    )

    assert view["title"] == "FAILED"
    assert view["border_style"] == "red"
    assert view["body"] == "request timed out"
    assert view["is_failure"] is True
