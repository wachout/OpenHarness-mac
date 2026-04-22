"""Tavily AI search tool."""

from __future__ import annotations

import os

import httpx
from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.utils.network_guard import NetworkGuardError, fetch_public_http_response


class TavilySearchInput(BaseModel):
    """Arguments for a Tavily search."""

    query: str = Field(description="Search query")
    max_results: int = Field(default=5, ge=1, le=20, description="Maximum number of results")
    api_key: str | None = Field(default=None, description="Tavily API key (or set TAVILY_API_KEY env var)")


class TavilySearchTool(BaseTool):
    """Search the web using Tavily AI search engine."""

    name = "tavily_search"
    description = "Search the web using Tavily AI. Use this for accurate, AI-optimized search results."
    input_model = TavilySearchInput

    def is_read_only(self, arguments: TavilySearchInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: TavilySearchInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        del context

        api_key = arguments.api_key or os.environ.get("TAVILY_API_KEY")
        if not api_key:
            return ToolResult(
                output="Tavily API key not found. Set TAVILY_API_KEY environment variable or pass api_key parameter.",
                is_error=True,
            )

        endpoint = "https://api.tavily.com/search"

        try:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                response = await client.post(
                    endpoint,
                    json={
                        "query": arguments.query,
                        "max_results": arguments.max_results,
                        "api_key": api_key,
                    },
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            return ToolResult(output=f"Tavily API error: {exc.response.status_code} - {exc.response.text}", is_error=True)
        except httpx.HTTPError as exc:
            return ToolResult(output=f"Tavily request failed: {exc}", is_error=True)
        except (OSError, NetworkGuardError) as exc:
            return ToolResult(output=f"Network error: {exc}", is_error=True)

        results = data.get("results", [])
        if not results:
            return ToolResult(output="No search results found.")

        lines = [f"Search results for: {arguments.query} (via Tavily AI)"]
        for index, result in enumerate(results, start=1):
            title = result.get("title", "Untitled")
            url = result.get("url", "")
            snippet = result.get("content", "")
            score = result.get("score", "")
            lines.append(f"{index}. {title}")
            lines.append(f"   URL: {url}")
            if snippet:
                lines.append(f"   {snippet[:300]}{'...' if len(snippet) > 300 else ''}")
            if score:
                lines.append(f"   Relevance: {score:.2f}")

        return ToolResult(output="\n".join(lines))


class TavilySearchResultsToolInput(BaseModel):
    """Arguments for getting full content from Tavily search results."""

    query: str = Field(description="Search query to get detailed results")
    max_results: int = Field(default=5, ge=1, le=20, description="Maximum number of results")
    api_key: str | None = Field(default=None, description="Tavily API key")


class TavilySearchResultsTool(BaseTool):
    """Get detailed search results with full content from Tavily."""

    name = "tavily_search_results"
    description = "Get detailed search results with full content from Tavily AI search."
    input_model = TavilySearchResultsToolInput

    def is_read_only(self, arguments: TavilySearchResultsToolInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: TavilySearchResultsToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        del context

        api_key = arguments.api_key or os.environ.get("TAVILY_API_KEY")
        if not api_key:
            return ToolResult(
                output="Tavily API key not found. Set TAVILY_API_KEY environment variable or pass api_key parameter.",
                is_error=True,
            )

        endpoint = "https://api.tavily.com/search"

        try:
            async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
                response = await client.post(
                    endpoint,
                    json={
                        "query": arguments.query,
                        "max_results": arguments.max_results,
                        "api_key": api_key,
                        "include_answer": True,
                        "include_raw_content": True,
                    },
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            return ToolResult(output=f"Tavily API error: {exc.response.status_code}", is_error=True)
        except httpx.HTTPError as exc:
            return ToolResult(output=f"Tavily request failed: {exc}", is_error=True)
        except (OSError, NetworkGuardError) as exc:
            return ToolResult(output=f"Network error: {exc}", is_error=True)

        results = data.get("results", [])
        answer = data.get("answer", "")

        lines = [f"Detailed search results for: {arguments.query}"]

        if answer:
            lines.append(f"\nAI Answer:\n{answer}\n")

        lines.append(f"\nFound {len(results)} results:\n")

        for index, result in enumerate(results, start=1):
            title = result.get("title", "Untitled")
            url = result.get("url", "")
            raw_content = result.get("raw_content", "")
            score = result.get("score", "")

            lines.append(f"{index}. {title}")
            lines.append(f"   URL: {url}")
            if score:
                lines.append(f"   Relevance: {score:.2f}")
            if raw_content:
                lines.append(f"   Content: {raw_content[:500]}{'...[truncated]' if len(raw_content) > 500 else ''}")
            lines.append("")

        return ToolResult(output="\n".join(lines))