from agents.paod import evaluate_success_criteria


def test_evaluate_success_criteria_supports_generator_expressions():
    result = {
        "success": True,
        "data": [
            {"title": "Story 1", "link": "https://example.com/1"},
            {"title": "Story 2", "link": "https://example.com/2"},
        ],
    }

    ok = evaluate_success_criteria(
        [
            "result.success == True",
            "all(item.title and item.link for item in result.data)",
        ],
        result,
    )

    assert ok is True


def test_evaluate_success_criteria_supports_safe_dict_get_calls_inside_generator():
    result = {
        "success": True,
        "data": [
            {"title": "Story 1", "link": "https://example.com/1"},
            {"title": "Story 2", "link": "https://example.com/2"},
        ],
    }

    ok = evaluate_success_criteria(
        [
            "all(item.get('title') and item.get('link') for item in result.data)",
        ],
        result,
    )

    assert ok is True


def test_evaluate_success_criteria_supports_slices_inside_generators():
    result = {
        "success": True,
        "data": [
            {"title": "Story 1", "link": "https://example.com/1"},
            {"title": "Story 2", "link": "https://example.com/2"},
            {"title": "Story 3", "link": "https://example.com/3"},
            {"title": "Story 4", "link": "https://example.com/4"},
        ],
    }

    ok = evaluate_success_criteria(
        [
            "all('title' in item and 'link' in item for item in result.data[:3])",
        ],
        result,
    )

    assert ok is True


def test_evaluate_success_criteria_treats_link_and_url_as_aliases():
    result = {
        "success": True,
        "data": [
            {"title": "Story 1", "link": "https://example.com/1"},
            {"title": "Story 2", "link": "https://example.com/2"},
        ],
    }

    ok = evaluate_success_criteria(
        [
            "all('title' in item and 'url' in item for item in result.data)",
            "all(item.url for item in result.data)",
            "all(item.get('url') for item in result.data)",
        ],
        result,
    )

    assert ok is True


def test_evaluate_success_criteria_treats_name_and_title_as_aliases():
    result = {
        "success": True,
        "data": [
            {"name": "Model 1", "url": "https://example.com/1"},
            {"name": "Model 2", "url": "https://example.com/2"},
        ],
    }

    ok = evaluate_success_criteria(
        [
            "all('title' in item for item in result.data)",
            "all(item.title for item in result.data)",
        ],
        result,
    )

    assert ok is True
