"""
Retrieval / Memory-adjacent tools.

This is where the agent's own long-term memory (ChromaDB) gets exposed
back to itself as a callable tool, alongside generic vector-store-backed
QA/retriever wrappers for any other store you plug in later.

pip installs:
    pip install langchain-chroma chromadb
    pip install sentence-transformers      # local embeddings (no API key needed)
"""

from __future__ import annotations

import os


def _get_embeddings():
    """
    Local, free embeddings by default (sentence-transformers). Swap for
    OpenAIEmbeddings / VoyageAIEmbeddings etc. by setting
    AGENTICSTUDIO_EMBEDDINGS=openai and providing the matching API key.
    """
    backend = os.environ.get("AGENTICSTUDIO_EMBEDDINGS", "local")

    if backend == "local":
        from langchain_huggingface import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")

    if backend == "openai":
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings()

    if backend == "voyage":
        from langchain_voyageai import VoyageAIEmbeddings

        return VoyageAIEmbeddings(model="voyage-3")

    raise ValueError(f"unknown AGENTICSTUDIO_EMBEDDINGS backend: {backend}")


def get_tools() -> list:
    tools = []

    # --- Long-term semantic memory (ChromaDB), exposed as a retriever tool ---
    try:
        from langchain_chroma import Chroma
        from langchain.tools.retriever import create_retriever_tool

        persist_dir = os.environ.get("AGENTICSTUDIO_MEMORY_DIR", "./memory/chroma")
        os.makedirs(persist_dir, exist_ok=True)

        vectorstore = Chroma(
            collection_name="agenticstudio_memory",
            embedding_function=_get_embeddings(),
            persist_directory=persist_dir,
        )
        retriever = vectorstore.as_retriever(search_kwargs={"k": 5})

        tools.append(
            create_retriever_tool(
                retriever,
                name="query_long_term_memory",
                description=(
                    "Search the agent's long-term memory for facts, past decisions, "
                    "or context from previous runs relevant to the current task."
                ),
            )
        )
    except ImportError:
        pass

    return tools


def get_vectorstore():
    """
    Direct handle to the memory Chroma store (for writing new memories from
    the orchestrator/graph -- not a tool itself, so it's not in get_tools()).
    """
    from langchain_chroma import Chroma

    persist_dir = os.environ.get("AGENTICSTUDIO_MEMORY_DIR", "./memory/chroma")
    os.makedirs(persist_dir, exist_ok=True)
    return Chroma(
        collection_name="agenticstudio_memory",
        embedding_function=_get_embeddings(),
        persist_directory=persist_dir,
    )
