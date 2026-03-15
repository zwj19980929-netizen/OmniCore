from utils.tool_evaluation_hook import evaluate_tool_result


def test_evaluate_tool_result_skips_successful_results(monkeypatch):
    printed = []

    monkeypatch.setattr("utils.tool_evaluation_hook.console.print", lambda message: printed.append(message))

    evaluate_tool_result(
        "web_worker",
        {"description": "fetch data"},
        {"success": True, "data": [{"title": "ok"}]},
        1,
    )

    assert printed == []


def test_evaluate_tool_result_prints_concise_failure(monkeypatch):
    printed = []

    monkeypatch.setattr("utils.tool_evaluation_hook.console.print", lambda message: printed.append(message))

    evaluate_tool_result(
        "web_worker",
        {"description": "fetch weather data from explicit page"},
        {"success": False, "error": "navigation landed on blocked page: 403 Forbidden", "count": 0},
        2,
    )

    assert len(printed) == 1
    assert "ToolFailure" in printed[0]
    assert "403 Forbidden" in printed[0]
    assert "fetch weather data from explicit page" in printed[0]
