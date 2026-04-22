---
name: tavily_search
description: Search the web using Tavily AI search engine. Use this when you need accurate, AI-optimized web search results.
---

# tavily_search

Search the web using Tavily AI-powered search engine.

## When to use

- When you need more accurate and contextual search results than standard search
- When researching topics that require AI-optimized relevance ranking
- When you need to gather current information from the web

## Prerequisites

You need a Tavily API key:
1. Sign up at https://tavily.com
2. Get your API key
3. Set environment variable: `TAVILY_API_KEY=your_key`

Or pass it directly in the tool call if supported.

## Usage

```json
{
  "query": "your search query here",
  "max_results": 5
}
```

## Output

Returns top search results with:
- Title
- URL
- Snippet/description
- Relevance score (if available)

## Notes

- Tavily provides AI-optimized search results
- More focused on relevance than traditional keyword matching
- Good for research and fact-gathering tasks