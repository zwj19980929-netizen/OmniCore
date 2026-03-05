import base64

from agents.web_worker import WebWorker


def test_decode_bing_redirect_url_with_base64_payload():
    target = "https://www.reuters.com/world/middle-east/"
    encoded = base64.urlsafe_b64encode(target.encode("utf-8")).decode("ascii").rstrip("=")
    redirect = f"https://www.bing.com/ck/a?u=a1{encoded}"
    decoded = WebWorker._decode_redirect_url(redirect)
    assert decoded == target


def test_search_engine_domain_filter():
    assert WebWorker._is_search_engine_domain("https://www.bing.com/search?q=test") is True
    assert WebWorker._is_search_engine_domain("https://www.reuters.com/world/") is False
