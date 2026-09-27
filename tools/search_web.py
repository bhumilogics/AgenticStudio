"""
Search & Web tools.

pip installs (pick what you need):
    pip install langchain-community duckduckgo-search
    pip install langchain-community tavily-python           # TAVILY_API_KEY
    pip install langchain-google-community                  # GOOGLE_API_KEY + GOOGLE_CSE_ID
    pip install langchain-community                         # Serper: SERPER_API_KEY
    pip install langchain-community                         # Bing: BING_SUBSCRIPTION_KEY + BING_SEARCH_URL
    pip install langchain-community                         # Searx: SEARX_HOST
    pip install langchain-community                         # Brave: BRAVE_API_KEY (or local browser key)
    pip install wikipedia
    pip install arxiv
    pip install xmltodict                                   # PubMed
    pip install beautifulsoup4                              # WebBaseLoader
"""

from __future__ import annotations

from ._env import require_env


def get_tools() -> list:
    tools = []

    # --- DuckDuckGo (no API key needed) ---
    try:
        from langchain_community.tools import DuckDuckGoSearchRun, DuckDuckGoSearchResults

        tools.append(DuckDuckGoSearchRun(name="duckduckgo_search"))
        tools.append(DuckDuckGoSearchResults(name="duckduckgo_search_results"))
    except ImportError:
        pass

    # --- Tavily (built for LLM agents; needs TAVILY_API_KEY) ---
    try:
        require_env("TAVILY_API_KEY")
        from langchain_community.tools.tavily_search import TavilySearchResults

        tools.append(TavilySearchResults(name="tavily_search", max_results=5))
    except Exception:
        pass

    # --- Google Programmable Search ---
    try:
        require_env("GOOGLE_API_KEY")
        require_env("GOOGLE_CSE_ID")
        from langchain_community.utilities import GoogleSearchAPIWrapper
        from langchain_core.tools import Tool

        wrapper = GoogleSearchAPIWrapper()
        tools.append(
            Tool(
                name="google_search",
                description="Search Google for recent/general web results.",
                func=wrapper.run,
            )
        )
    except Exception:
        pass

    # --- Serper.dev (cheaper Google Search API alternative) ---
    try:
        require_env("SERPER_API_KEY")
        from langchain_community.utilities import GoogleSerperAPIWrapper
        from langchain_core.tools import Tool

        wrapper = GoogleSerperAPIWrapper()
        tools.append(Tool(name="google_serper_search", description="Search via Serper (Google results).", func=wrapper.run))
    except Exception:
        pass

    # --- Bing ---
    try:
        require_env("BING_SUBSCRIPTION_KEY")
        require_env("BING_SEARCH_URL")
        from langchain_community.utilities import BingSearchAPIWrapper
        from langchain_core.tools import Tool

        wrapper = BingSearchAPIWrapper()
        tools.append(Tool(name="bing_search", description="Search Bing.", func=wrapper.run))
    except Exception:
        pass

    # --- SearXNG (self-hosted metasearch) ---
    try:
        require_env("SEARX_HOST")
        from langchain_community.utilities import SearxSearchWrapper
        from langchain_core.tools import Tool

        wrapper = SearxSearchWrapper(searx_host=require_env("SEARX_HOST"))
        tools.append(Tool(name="searx_search", description="Search a self-hosted SearXNG instance.", func=wrapper.run))
    except Exception:
        pass

    # --- Brave Search ---
    try:
        require_env("BRAVE_API_KEY")
        from langchain_community.tools import BraveSearch

        tools.append(BraveSearch.from_api_key(api_key=require_env("BRAVE_API_KEY"), name="brave_search"))
    except Exception:
        pass

    # --- Wikipedia (no key needed) ---
    try:
        from langchain_community.tools import WikipediaQueryRun
        from langchain_community.utilities import WikipediaAPIWrapper

        tools.append(WikipediaQueryRun(api_wrapper=WikipediaAPIWrapper(), name="wikipedia"))
    except ImportError:
        pass

    # --- Arxiv (no key needed) ---
    try:
        from langchain_community.tools import ArxivQueryRun
        from langchain_community.utilities import ArxivAPIWrapper

        tools.append(ArxivQueryRun(api_wrapper=ArxivAPIWrapper(), name="arxiv_search"))
    except ImportError:
        pass

    # --- PubMed (no key needed) ---
    try:
        from langchain_community.tools import PubmedQueryRun
        from langchain_community.utilities import PubMedAPIWrapper

        tools.append(PubmedQueryRun(api_wrapper=PubMedAPIWrapper(), name="pubmed_search"))
    except ImportError:
        pass

    # --- Generic web page fetch + parse (scrape a known URL) ---
    try:
        from langchain_community.document_loaders import WebBaseLoader
        from langchain_core.tools import tool

        @tool("fetch_web_page")
        def fetch_web_page(url: str) -> str:
            """Fetch a web page by URL and return its cleaned text content."""
            docs = WebBaseLoader(url).load()
            return "\n\n".join(d.page_content for d in docs)[:20000]

        tools.append(fetch_web_page)
    except ImportError:
        pass

    return tools
