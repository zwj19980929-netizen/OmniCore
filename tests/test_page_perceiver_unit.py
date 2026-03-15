import asyncio

from utils.page_perceiver import PagePerceiver, _StdlibStructureParser


class _Result:
    def __init__(self, success=True, data=None):
        self.success = success
        self.data = data


class _Toolkit:
    async def get_page_html(self):
        return _Result(
            True,
            """
            <html lang="en">
              <head><title>Example Page</title></head>
              <body>
                <nav><a href="/home">Home</a><a href="/news">News</a></nav>
                <main>
                  <h1>OpenAI API Updates</h1>
                  <p>This page describes the latest OpenAI API updates and pricing changes.</p>
                  <button id="accept">Accept</button>
                </main>
              </body>
            </html>
            """,
        )


def test_fallback_dom_parsing_works_without_bs4():
    perceiver = PagePerceiver()
    structure = asyncio.run(
        perceiver._fallback_dom_parsing(
            _Toolkit(),
            "https://example.com/openai-api",
            "",
        )
    )

    assert structure.title == "Example Page"
    assert any(block.content == "OpenAI API Updates" for block in structure.main_content_blocks)
    assert any("OpenAI API updates" in block.content for block in structure.main_content_blocks)
    assert any(elem.get("selector") == "#accept" for elem in structure.interactive_elements)
    assert structure.metadata.get("fallback") in {"html_parser", "beautifulsoup"}


def test_stdlib_selector_escapes_special_class_tokens():
    parser = _StdlibStructureParser()

    selector = parser._selector_for("nav", {"class": "lg:block hidden"})

    assert selector == "nav.lg\\:block.hidden"
