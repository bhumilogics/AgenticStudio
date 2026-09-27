"""
Cloud / Infra tools (incl. browser automation).

pip installs:
    pip install langchain-community boto3                         # AWSLambdaTool
    pip install langchain-community amadeus                       # AmadeusToolkit
    pip install langchain-community playwright && playwright install   # PlayWrightBrowserToolkit
    pip install langchain-community selenium                       # SeleniumBrowserToolkit (community, optional)
"""

from __future__ import annotations

from ._env import require_env


def get_tools() -> list:
    tools = []

    # --- AWS Lambda (invoke a function as a tool) ---
    try:
        require_env("AGENTICSTUDIO_LAMBDA_FUNCTION_NAME")
        from langchain_community.tools import AWSLambdaTool

        tools.append(
            AWSLambdaTool(
                function_name=require_env("AGENTICSTUDIO_LAMBDA_FUNCTION_NAME"),
                name="aws_lambda",
                description="Invoke the configured AWS Lambda function.",
            )
        )
    except Exception:
        pass

    # --- Amadeus (travel data: flights, hotels) ---
    try:
        require_env("AMADEUS_CLIENT_ID")
        require_env("AMADEUS_CLIENT_SECRET")
        from langchain_community.agent_toolkits import AmadeusToolkit

        toolkit = AmadeusToolkit()
        tools.extend(toolkit.get_tools())
    except Exception:
        pass

    # --- Playwright browser control (click, navigate, extract text) ---
    # Runs a real headless browser -- heavier than the other tools, so it's
    # opt-in via AGENTICSTUDIO_ENABLE_BROWSER=1 rather than auto-loaded.
    import os

    if os.environ.get("AGENTICSTUDIO_ENABLE_BROWSER") == "1":
        try:
            from langchain_community.agent_toolkits import PlayWrightBrowserToolkit
            from langchain_community.tools.playwright.utils import create_sync_playwright_browser

            browser = create_sync_playwright_browser()
            toolkit = PlayWrightBrowserToolkit.from_browser(sync_browser=browser)
            tools.extend(toolkit.get_tools())
        except ImportError:
            pass

    return tools
