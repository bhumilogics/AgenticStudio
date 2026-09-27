"""
Multimodal tools.

pip installs:
    pip install openai                                      # DallEAPIWrapper (OPENAI_API_KEY)
    pip install langchain-community azure-ai-formrecognizer azure-cognitiveservices-speech  # Azure
"""

from __future__ import annotations

from ._env import require_env


def get_tools() -> list:
    tools = []

    # --- DALL-E image generation ---
    try:
        require_env("OPENAI_API_KEY")
        from langchain_community.utilities.dalle_image_generator import DallEAPIWrapper
        from langchain_core.tools import tool

        dalle = DallEAPIWrapper()

        @tool("generate_image")
        def generate_image(prompt: str) -> str:
            """Generate an image from a text prompt and return the image URL."""
            return dalle.run(prompt)

        tools.append(generate_image)
    except Exception:
        pass

    # --- Azure Cognitive Services (OCR, image analysis, form recognizer, speech) ---
    try:
        require_env("AZURE_COGS_KEY")
        require_env("AZURE_COGS_ENDPOINT")
        from langchain_community.agent_toolkits import AzureCognitiveServicesToolkit

        toolkit = AzureCognitiveServicesToolkit()
        tools.extend(toolkit.get_tools())
    except Exception:
        pass

    return tools
