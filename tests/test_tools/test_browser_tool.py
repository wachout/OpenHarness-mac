"""Tests for browser automation tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openharness.tools.base import ToolExecutionContext
from openharness.tools.browser_tool import (
    BrowserNavigateTool,
    BrowserGetContentTool,
    BrowserScreenshotTool,
    BrowserClickTool,
    BrowserExecuteJsTool,
    BrowserNavigateInput,
    BrowserGetContentInput,
    BrowserScreenshotInput,
    BrowserClickInput,
    BrowserExecuteJsInput,
    _BrowserManager,
    PLAYWRIGHT_AVAILABLE,
)

pytestmark = pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed")


def _make_mock_page(
    title="Test Page",
    url="https://example.com",
    text_content="Hello World from Chromium",
    html_content="<html><body><h1>Hello World</h1></body></html>",
):
    """Create a mock Playwright Page."""
    page = AsyncMock()
    page.url = url
    page.title = AsyncMock(return_value=title)
    page.goto = AsyncMock()
    page.close = AsyncMock()
    page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n")
    page.content = AsyncMock(return_value=html_content)
    page.evaluate = AsyncMock(return_value=text_content)
    page.wait_for_load_state = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.click = AsyncMock()
    return page


def _make_mock_browser(pages=None):
    """Create a mock Playwright Browser."""
    browser = AsyncMock()
    browser.new_page = AsyncMock(side_effect=pages or [_make_mock_page()])
    browser.close = AsyncMock()
    return browser


# ---------------------------------------------------------------------------
# Unit tests with mocked Playwright
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_navigate_tool(tmp_path):
    """Test browser navigation to a simple page."""
    mock_page = _make_mock_page()

    with patch.object(_BrowserManager, "get_browser", return_value=_make_mock_browser([mock_page])):
        # Reset singleton so it picks up the mock
        _BrowserManager._browser = None
        _BrowserManager._playwright = None

        tool = BrowserNavigateTool()
        result = await tool.execute(
            BrowserNavigateInput(url="https://example.com"),
            ToolExecutionContext(cwd=tmp_path),
        )

    assert result.is_error is False
    assert "Successfully navigated" in result.output
    assert "example.com" in result.output
    mock_page.goto.assert_called_once()


@pytest.mark.asyncio
async def test_browser_get_content_tool(tmp_path):
    """Test extracting text content from a webpage."""
    mock_page = _make_mock_page(text_content="Hello World from Chromium")

    with patch.object(_BrowserManager, "get_browser", return_value=_make_mock_browser([mock_page])):
        _BrowserManager._browser = None
        _BrowserManager._playwright = None

        tool = BrowserGetContentTool()
        result = await tool.execute(
            BrowserGetContentInput(
                url="https://example.com",
                extract_text_only=True,
            ),
            ToolExecutionContext(cwd=tmp_path),
        )

    assert result.is_error is False
    assert "example.com" in result.output
    mock_page.evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_browser_get_content_html_mode(tmp_path):
    """Test extracting HTML from a webpage."""
    mock_page = _make_mock_page(
        html_content="<html><body><h1>Hello</h1></body></html>"
    )

    with patch.object(_BrowserManager, "get_browser", return_value=_make_mock_browser([mock_page])):
        _BrowserManager._browser = None
        _BrowserManager._playwright = None

        tool = BrowserGetContentTool()
        result = await tool.execute(
            BrowserGetContentInput(
                url="https://example.com",
                extract_text_only=False,
            ),
            ToolExecutionContext(cwd=tmp_path),
        )

    assert result.is_error is False
    mock_page.content.assert_called_once()


@pytest.mark.asyncio
async def test_browser_screenshot_tool_base64(tmp_path):
    """Test taking a screenshot and returning base64."""
    mock_page = _make_mock_page()

    with patch.object(_BrowserManager, "get_browser", return_value=_make_mock_browser([mock_page])):
        _BrowserManager._browser = None
        _BrowserManager._playwright = None

        tool = BrowserScreenshotTool()
        result = await tool.execute(
            BrowserScreenshotInput(
                url="https://example.com",
                full_page=False,
            ),
            ToolExecutionContext(cwd=tmp_path),
        )

    assert result.is_error is False
    assert "Screenshot captured" in result.output
    assert "base64" in result.output
    mock_page.screenshot.assert_called_once()


@pytest.mark.asyncio
async def test_browser_screenshot_save_to_file(tmp_path):
    """Test saving screenshot to a file."""
    mock_page = _make_mock_page()
    output_path = str(tmp_path / "screenshot.png")

    with patch.object(_BrowserManager, "get_browser", return_value=_make_mock_browser([mock_page])):
        _BrowserManager._browser = None
        _BrowserManager._playwright = None

        tool = BrowserScreenshotTool()
        result = await tool.execute(
            BrowserScreenshotInput(
                url="https://example.com",
                output_path=output_path,
            ),
            ToolExecutionContext(cwd=tmp_path),
        )

    assert result.is_error is False
    assert "saved" in result.output.lower()


@pytest.mark.asyncio
async def test_browser_execute_js_tool(tmp_path):
    """Test executing JavaScript on a webpage."""
    mock_page = _make_mock_page()
    mock_page.evaluate = AsyncMock(return_value="Test Page")

    with patch.object(_BrowserManager, "get_browser", return_value=_make_mock_browser([mock_page])):
        _BrowserManager._browser = None
        _BrowserManager._playwright = None

        tool = BrowserExecuteJsTool()
        result = await tool.execute(
            BrowserExecuteJsInput(
                url="https://example.com",
                script="document.title",
            ),
            ToolExecutionContext(cwd=tmp_path),
        )

    assert result.is_error is False
    assert "JavaScript execution result" in result.output
    mock_page.evaluate.assert_called_once_with("document.title")


@pytest.mark.asyncio
async def test_browser_execute_js_dict_result(tmp_path):
    """Test JS execution returning a dict result."""
    mock_page = _make_mock_page()
    mock_page.evaluate = AsyncMock(return_value={"key": "value", "count": 42})

    with patch.object(_BrowserManager, "get_browser", return_value=_make_mock_browser([mock_page])):
        _BrowserManager._browser = None
        _BrowserManager._playwright = None

        tool = BrowserExecuteJsTool()
        result = await tool.execute(
            BrowserExecuteJsInput(
                url="https://example.com",
                script="Object.assign({}, {key: 'value', count: 42})",
            ),
            ToolExecutionContext(cwd=tmp_path),
        )

    assert result.is_error is False
    assert "value" in result.output
    assert "42" in result.output


@pytest.mark.asyncio
async def test_browser_click_tool(tmp_path):
    """Test clicking an element on a page."""
    mock_page = _make_mock_page(url="https://example.com/clicked")

    with patch.object(_BrowserManager, "get_browser", return_value=_make_mock_browser([mock_page])):
        _BrowserManager._browser = None
        _BrowserManager._playwright = None

        tool = BrowserClickTool()
        result = await tool.execute(
            BrowserClickInput(
                url="https://example.com",
                selector="button.submit",
            ),
            ToolExecutionContext(cwd=tmp_path),
        )

    assert result.is_error is False
    assert "Clicked element" in result.output
    mock_page.click.assert_called_once_with("button.submit")


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_navigate_invalid_url(tmp_path):
    """Test navigation with invalid URL."""
    tool = BrowserNavigateTool()
    result = await tool.execute(
        BrowserNavigateInput(url="not-a-valid-url"),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is True
    assert "Invalid URL" in result.output


@pytest.mark.asyncio
async def test_browser_navigate_credentials_in_url(tmp_path):
    """Test navigation rejects URLs with embedded credentials."""
    tool = BrowserNavigateTool()
    result = await tool.execute(
        BrowserNavigateInput(url="https://user:pass@example.com"),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is True


# ---------------------------------------------------------------------------
# Read-only policy tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_tools_are_read_only():
    """Test that navigation, content, and screenshot tools are read-only."""
    navigate_tool = BrowserNavigateTool()
    content_tool = BrowserGetContentTool()
    screenshot_tool = BrowserScreenshotTool()

    assert navigate_tool.is_read_only(BrowserNavigateInput(url="https://example.com")) is True
    assert content_tool.is_read_only(BrowserGetContentInput(url="https://example.com")) is True
    assert screenshot_tool.is_read_only(BrowserScreenshotInput(url="https://example.com")) is True


@pytest.mark.asyncio
async def test_browser_click_and_js_not_read_only():
    """Test that click and JS execution tools are not read-only."""
    click_tool = BrowserClickTool()
    js_tool = BrowserExecuteJsTool()

    assert click_tool.is_read_only(BrowserClickInput(url="https://example.com", selector="button")) is False
    assert js_tool.is_read_only(BrowserExecuteJsInput(url="https://example.com", script="1+1")) is False
