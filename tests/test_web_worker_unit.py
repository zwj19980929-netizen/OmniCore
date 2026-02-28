from agents.web_worker import WebWorker


class FakeResponse:
    def __init__(self, text: str, content_type: str = "text/html; charset=utf-8"):
        self.text = text
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        return None


def test_extract_static_text_blocks_prefers_long_content():
    worker = WebWorker()
    html = """
    <html>
      <body>
        <article>
          <p>This is a long article paragraph with enough content to be captured by static text extraction.</p>
          <p>This is another detailed paragraph that should also be available for summary-style tasks.</p>
        </article>
      </body>
    </html>
    """

    blocks = worker._extract_static_text_blocks(html, limit=3)

    assert len(blocks) >= 2
    assert "paragraph" in blocks[0]["text"].lower()


def test_prefers_static_text_for_summary_tasks():
    worker = WebWorker()

    assert worker._prefers_static_text("总结这篇文章内容") is True
    assert worker._prefers_static_text("collect latest links") is False


def test_static_fetch_returns_text_mode_when_summary_task(monkeypatch):
    worker = WebWorker()
    html = """
    <html>
      <body>
        <main>
          <p>This is a long summary paragraph with enough information to be returned directly.</p>
          <p>This second paragraph provides additional useful context for the summary task.</p>
        </main>
      </body>
    </html>
    """

    def _fake_get(*_args, **_kwargs):
        return FakeResponse(html)

    monkeypatch.setattr("agents.web_worker.requests.get", _fake_get)

    result = worker._static_fetch("https://example.com/article", "summary this article", limit=5)

    assert result["success"] is True
    assert result["mode"] == "static_fetch_text"
    assert "text" in result["data"][0]
