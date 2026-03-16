from utils.search_engine_profiles import (
    build_direct_search_urls,
    decode_search_redirect_url,
    get_search_result_selectors,
    looks_like_search_results_url,
)


def test_looks_like_search_results_url_supports_all_primary_engines():
    assert looks_like_search_results_url("https://www.bing.com/search?q=hefei+weather") is True
    assert looks_like_search_results_url("https://www.baidu.com/s?wd=%E5%90%88%E8%82%A5%E5%A4%A9%E6%B0%94") is True
    assert looks_like_search_results_url("https://duckduckgo.com/?q=hefei+weather") is True
    assert looks_like_search_results_url("https://www.google.com/search?q=hefei+weather") is True
    assert looks_like_search_results_url("https://www.sogou.com/web?query=%E5%90%88%E8%82%A5%E5%A4%A9%E6%B0%94") is True


def test_looks_like_search_results_url_rejects_search_homepages():
    assert looks_like_search_results_url("https://www.bing.com/") is False
    assert looks_like_search_results_url("https://www.baidu.com/") is False
    assert looks_like_search_results_url("https://duckduckgo.com/") is False
    assert looks_like_search_results_url("https://www.google.com/") is False
    assert looks_like_search_results_url("https://www.sogou.com/") is False


def test_decode_search_redirect_url_handles_multiple_engines():
    google = "https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Fstory&sa=U"
    ddg = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fstory"
    sogou = "https://www.sogou.com/link?url=https%3A%2F%2Fexample.com%2Fstory"

    assert decode_search_redirect_url(google) == "https://example.com/story"
    assert decode_search_redirect_url(ddg) == "https://example.com/story"
    assert decode_search_redirect_url(sogou) == "https://example.com/story"


def test_get_search_result_selectors_includes_engine_specific_entries():
    selectors = get_search_result_selectors("https://www.google.com/search?q=test")

    assert "#search .tF2Cxc" in selectors
    assert "#search .g" in selectors
    assert ".result" in selectors


def test_build_direct_search_urls_includes_sogou_profile():
    urls = build_direct_search_urls("hefei weather")

    assert any(profile.name == "Sogou" for profile, _url in urls)
    assert any("sogou.com/web?query=" in url for _profile, url in urls)
