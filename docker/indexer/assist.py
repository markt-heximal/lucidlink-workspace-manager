"""Writing assist for the document editor, on the engine's own LLM.

Replaces the web app's `docs-ai` edge function, which sent document text to a
hosted AI gateway. Same actions and prompts; the text now goes only to the
LLM this engine is configured with (LLM_BASE_URL).
"""
from __future__ import annotations

import html
import re
from typing import Literal

from .llm import LLM

Action = Literal["summarize", "rewrite", "shorten", "lengthen", "continue", "generate", "custom"]

SYSTEM = (
    "You are a writing assistant embedded in a rich-text document editor. "
    "Return ONLY the requested HTML fragment with no markdown fences, no commentary, "
    "no surrounding <html>/<body> tags. Use semantic tags like <p>, <ul>, <ol>, <li>, "
    "<h2>, <h3>, <strong>, <em>, <blockquote>. Keep voice and language matching the input."
)


def build_prompt(action: Action, text: str, instruction: str = "") -> str:
    if action == "summarize":
        return f"Summarize the following content into a concise bulleted list (3-6 bullets):\n\n{text}"
    if action == "rewrite":
        return ("Rewrite the following content to improve clarity, flow, and grammar. "
                f"Preserve meaning and roughly the same length:\n\n{text}")
    if action == "shorten":
        return f"Make the following content significantly shorter while keeping the key points:\n\n{text}"
    if action == "lengthen":
        return f"Expand the following content with more detail, examples, and depth:\n\n{text}"
    if action == "continue":
        return ("Continue writing naturally from where this content ends. Match the existing tone and "
                f"style. Output only the new continuation, not the original text:\n\n{text}")
    if action == "generate":
        return f"Write a new document section based on this prompt:\n\n{instruction or text}"
    return f"{instruction or 'Edit the content as appropriate.'}\n\nContent:\n{text}"


def to_html(content: str) -> str:
    """Strip code fences; wrap plain text in paragraphs (escaped)."""
    content = re.sub(r"^```(?:html)?\s*", "", content.strip(), flags=re.I)
    content = re.sub(r"\s*```$", "", content).strip()
    if re.search(r"<[a-z][\s\S]*>", content, flags=re.I):
        return content
    paras = [p for p in re.split(r"\n{2,}", content) if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br/>')}</p>" for p in paras)


def assist(llm: LLM, action: Action, text: str, instruction: str = "") -> dict:
    out = llm.chat(SYSTEM, build_prompt(action, text, instruction), temperature=0.4)
    return {"html": to_html(out), "action": action}
