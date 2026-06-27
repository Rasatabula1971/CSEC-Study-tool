# PHASE: dual
"""
backend/ollama_client.py
========================
Thin httpx wrapper around the local Ollama HTTP API. This is the ONLY way the
system talks to a model — we never import the Ollama Python SDK (CLAUDE.md).

One model (MODEL_CHAT) serves every role (Archivist / Tutor / Examiner); roles
differ by system prompt only. Embeddings use MODEL_EMBED and evict immediately
(keep_alive=0) so the 3B chat model keeps its RAM slot.

Env (from .env): OLLAMA_BASE, MODEL_CHAT, MODEL_EMBED.
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

OLLAMA = os.getenv("OLLAMA_BASE", "http://localhost:11434")
MODEL_CHAT = os.getenv("MODEL_CHAT", "llama3.2:3b")
MODEL_EMBED = os.getenv("MODEL_EMBED", "nomic-embed-text")


def ollama_embed(text: str) -> list[float]:
    """Embed one string. keep_alive=0 evicts the embedding model immediately."""
    r = httpx.post(
        f"{OLLAMA}/api/embeddings",
        json={"model": MODEL_EMBED, "prompt": text, "keep_alive": 0},
        timeout=300,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def ollama_chat(messages: list[dict], system: str, schema: dict | None = None) -> str:
    """Chat completion. `schema` (a JSON Schema dict) forces conforming JSON output.

    keep_alive="30m" holds the 3B chat model resident across a study session so
    the first Submit isn't paying a cold model-load tax. This is the opposite of
    ollama_embed (keep_alive=0): the embedding model still evicts immediately so
    the chat model keeps its RAM slot (CLAUDE.md v3.0 RAM budget).
    """
    payload = {
        "model": MODEL_CHAT,
        "messages": [{"role": "system", "content": system}] + messages,
        "stream": False,
        "keep_alive": "30m",
    }
    if schema:
        payload["format"] = schema
    r = httpx.post(f"{OLLAMA}/api/chat", json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"]


def ollama_health() -> bool:
    """True if the Ollama server answers /api/tags."""
    try:
        return httpx.get(f"{OLLAMA}/api/tags", timeout=3).status_code == 200
    except Exception:
        return False


def list_models() -> list[str]:
    """Return the names of all pulled models, e.g. ['llama3.2:3b', 'nomic-embed-text:latest'].

    Returns an empty list if the server is unreachable.
    """
    try:
        r = httpx.get(f"{OLLAMA}/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def _name_matches(required: str, pulled: list[str]) -> bool:
    """A required model is present if it matches a pulled name, ignoring the
    implicit ':latest' tag (Ollama lists 'nomic-embed-text' as 'nomic-embed-text:latest').
    """
    base = required.split(":")[0]
    for name in pulled:
        if name == required or name.split(":")[0] == base:
            return True
    return False


def verify_models() -> dict:
    """Check the server is up and the required chat+embed models are pulled.

    Returns {"healthy": bool, "pulled": [...], "missing": [...]}.
    """
    healthy = ollama_health()
    pulled = list_models() if healthy else []
    required = [MODEL_CHAT, MODEL_EMBED]
    missing = [m for m in required if not _name_matches(m, pulled)]
    return {"healthy": healthy, "pulled": pulled, "missing": missing}


if __name__ == "__main__":
    # Manual smoke test — run once Ollama is installed and models are pulled.
    info = verify_models()
    print(f"OLLAMA_BASE : {OLLAMA}")
    print(f"healthy     : {info['healthy']}")
    print(f"pulled      : {', '.join(info['pulled']) or '(none / server down)'}")
    print(f"missing     : {', '.join(info['missing']) or '(none)'}")
    if info["healthy"] and not info["missing"]:
        dim = len(ollama_embed("smoke test"))
        print(f"embed dim   : {dim}  (expected {os.getenv('EMBED_DIM', '768')})")
        print(ollama_chat([{"role": "user", "content": "Reply with the single word OK."}],
                          system="You are a terse assistant."))
