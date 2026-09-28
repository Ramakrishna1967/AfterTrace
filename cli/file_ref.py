"""@ file references: fuzzy search under the project root, capped + redacted inject.

Mirrors opencode docs: `@` does a fuzzy file search in the working directory
and the matched file's content is added to the conversation.
"""

from __future__ import annotations

import os
import re

MAX_BYTES = 8192
MAX_MATCHES = 8
SKIP_DIRS = {".git", ".data", "__pycache__", "node_modules", ".venv", ".pytest_cache"}

_SECRET_RES = [
    # NOTE: value class excludes JSON delimiters (" ' } , ;) so redacting a
    # serialized JSON string cannot eat its closing quote/brace and invalidate
    # the document. Structured exports must still redact *before* json.dumps
    # (see sessions_ops._redact_value); this bound is defense-in-depth for
    # display paths that redact rendered text.
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*[^\s\"'},;]+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)(secret|token|password)\s*[:=]\s*['\"]?[^\s\"'},;]+"),
    re.compile(r"eyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}"),
    re.compile(r"hsk_[A-Za-z0-9_]+"),
]


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def redact(text: str) -> str:
    out = text
    for pat in _SECRET_RES:
        out = pat.sub("[redacted]", out)
    return out


def _inside_root(path: str, root: str) -> bool:
    return os.path.commonpath([os.path.abspath(path), root]) == root


def resolve_ref(fragment: str, root: str | None = None) -> list[str]:
    """Return repo-relative file paths matching fragment. Guards traversal."""
    root = root or project_root()
    frag = fragment.strip().strip("\"'")
    if not frag or frag.startswith("-"):
        return []
    # Exact relative path first (still must stay inside root).
    cand = os.path.abspath(os.path.join(root, frag))
    if _inside_root(cand, root) and os.path.isfile(cand):
        return [os.path.relpath(cand, root).replace(os.sep, "/")]
    # Fuzzy: basename substring match, skipping tool dirs.
    needle = os.path.basename(frag).lower()
    if not needle:
        return []
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
            if needle in fn.lower() or needle in rel.lower():
                hits.append(rel)
                if len(hits) >= MAX_MATCHES:
                    return sorted(hits)
    return sorted(hits)


def read_ref(relpath: str, root: str | None = None) -> tuple[str, str]:
    """Return (display_text, note). Binary skipped, content capped + redacted."""
    root = root or project_root()
    abs_path = os.path.abspath(os.path.join(root, relpath))
    if not _inside_root(abs_path, root):
        return "", "refused: outside project root"
    if not os.path.isfile(abs_path):
        return "", "not a file"
    try:
        with open(abs_path, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
    except OSError as e:
        return "", f"unreadable: {e}"
    if b"\x00" in raw:
        return "", "binary file, skipped"
    truncated = len(raw) > MAX_BYTES
    text = redact(raw[:MAX_BYTES].decode("utf-8", errors="replace"))
    note = " (truncated at 8 KiB)" if truncated else ""
    return text, note


def extract_refs(text: str) -> list[str]:
    """Find @fragments in input text (stops at whitespace)."""
    return re.findall(r"@([A-Za-z0-9_.\-/\\]+)", text)
