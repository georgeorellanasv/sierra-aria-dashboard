"""
Convert Sierra's Lexical editor state JSON to Markdown.

Lexical (Meta's editor framework) stores content as a tree of typed nodes.
Sierra extends it with agent-builder specific types like `basic-block`,
`condition-block`, `tool-reference`, `observation`, etc.

For analysis we need:
  - Each top-level block as a separate markdown chunk
  - A human-readable label (the block's header)
  - The full body as markdown so Sonnet can read it
"""
from __future__ import annotations

from typing import Any


# Block-type containers — each becomes its own top-level chunk.
BLOCK_TYPES: set[str] = {
    "basic-block",
    "condition-block",
    "journey-block",
    "tools-block",
    "agent-monitors-block",
    "lite-block",
}

# Nodes that contain the visible name of a block or sub-header.
LABEL_TYPES: set[str] = {
    "basic-block-header-label",
    "condition-header-label",
    "journey-header-value",
    "tools-header-label",
    "base-block-header-label",
    "journey-field-label",
    "condition-label",
}

# Decorative nodes to drop silently.
IGNORE_TYPES: set[str] = {
    "block-header-decorator",
    "disable-block",
    "basic-block-header-icon",
    "condition-header-icon",
    "journey-header-icon",
    "tools-header-icon",
    "base-block-header-icon",
    "add-block",
    "add-tool",
    "add-agent-monitor-action",
    "scoped-end-add-block",
    "collapsed-preview",
    "rootStore",
}


def _node_text(node: dict) -> str:
    """Recursively flatten all `text` nodes in a subtree into a single string."""
    if not isinstance(node, dict):
        return ""
    parts: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if n.get("type") == "text" and isinstance(n.get("text"), str):
                parts.append(n["text"])
            for v in n.get("children", []) or []:
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return "".join(parts).strip()


def _block_label(block_node: dict) -> str:
    """Find the first label-type node inside `block_node` and return its text."""
    if not isinstance(block_node, dict):
        return ""

    def walk(n: Any) -> str | None:
        if isinstance(n, dict):
            if n.get("type") in LABEL_TYPES:
                t = _node_text(n)
                if t:
                    return t
            for v in n.get("children", []) or []:
                r = walk(v)
                if r:
                    return r
        elif isinstance(n, list):
            for v in n:
                r = walk(v)
                if r:
                    return r
        return None

    return walk(block_node) or ""


def _convert(node: Any, indent: int = 0) -> str:
    if isinstance(node, list):
        return "".join(_convert(c, indent) for c in node)
    if not isinstance(node, dict):
        return ""

    t = node.get("type")
    children = node.get("children") or []

    # Text leaf: preserve text with basic formatting flags if any.
    if t == "text":
        text = node.get("text", "") or ""
        fmt = node.get("format") or 0
        # Lexical bitflags: 1=bold, 2=italic, 4=strike, 8=underline, 16=code
        if fmt & 16: text = f"`{text}`"
        if fmt & 1:  text = f"**{text}**"
        if fmt & 2:  text = f"*{text}*"
        return text

    if t in IGNORE_TYPES:
        return ""

    # Label node -> bold, no trailing newline (caller decides context).
    if t in LABEL_TYPES:
        inner = "".join(_convert(c, indent) for c in children).strip()
        return f"**{inner}** " if inner else ""

    # Top-level block headers — labels already return bold; we wrap block itself
    # with a level-3 heading assembled in `split_blocks`, not here.
    if t in ("basic-block-header", "condition-header", "journey-header",
             "tools-header", "base-block-header"):
        inner = "".join(_convert(c, indent) for c in children).strip()
        return f"\n### {inner}\n\n" if inner else ""

    # Lists
    if t == "list":
        items: list[str] = []
        for c in children:
            item_md = "".join(_convert(cc, indent + 1) for cc in (c.get("children") or []))
            items.append(item_md.strip())
        list_type = node.get("listType")
        prefix = "1." if list_type == "number" else "-"
        lines = []
        for i, it in enumerate(items):
            marker = f"{i+1}." if list_type == "number" else prefix
            lines.append("  " * indent + f"{marker} {it}")
        return "\n" + "\n".join(lines) + "\n"

    if t == "listitem":
        return "".join(_convert(c, indent) for c in children)

    # Domain-specific inline nodes — encode their semantics as tagged text.
    if t == "tool-reference":
        name = node.get("toolName") or node.get("name") or _node_text(node) or "?"
        return f"`tool:{name}`"

    if t == "observation":
        return f"[observation: {_node_text(node)}]"

    if t == "condition-criteria":
        return f"[criteria: {_node_text(node)}]"

    if t == "journey-field":
        # Contains a label and a value as children.
        inner = "".join(_convert(c, indent) for c in children).strip()
        return f"\n- {inner}\n" if inner else ""

    if t == "journey-field-value":
        return _node_text(node) + " "

    if t == "block-tag":
        return f"[tag: {_node_text(node)}]"

    # Paragraph / default container
    if t == "paragraph":
        inner = "".join(_convert(c, indent) for c in children)
        return inner + "\n\n"

    # Unknown container: pass children through.
    return "".join(_convert(c, indent) for c in children)


def block_to_markdown(block_node: dict) -> str:
    """Convert a single top-level block into a markdown string."""
    body = _convert(block_node).strip()
    # Collapse runs of blank lines.
    while "\n\n\n" in body:
        body = body.replace("\n\n\n", "\n\n")
    return body


def split_blocks(editor_state: dict) -> list[dict]:
    """
    Given the raw editorStateJson (already parsed as dict),
    return [{uuid, type, name, markdown}, ...] one per top-level block.
    """
    root = editor_state.get("root") or {}
    out: list[dict] = []
    for idx, child in enumerate(root.get("children") or []):
        t = child.get("type") or ""
        if t in IGNORE_TYPES:
            continue
        uuid = (child.get("$") or {}).get("uuid") or ""
        name = _block_label(child)
        md = block_to_markdown(child)
        out.append({"idx": idx, "uuid": uuid, "type": t, "name": name, "markdown": md})
    return out


def full_markdown(editor_state: dict, *, header_prefix: str = "## ") -> str:
    """Concatenate all top-level blocks into a single markdown document."""
    chunks: list[str] = []
    for b in split_blocks(editor_state):
        title = b["name"] or b["type"]
        chunks.append(f"{header_prefix}{title}\n\n{b['markdown']}")
    return "\n\n---\n\n".join(chunks)


def find_all_named_blocks(editor_state: dict) -> list[dict]:
    """
    Walk the whole tree and return every block-type node that has a non-empty
    label. Each result has keys: {uuid, type, name, depth, markdown}.

    This is the flat list we actually want for analysis — Sierra's UI shows
    nested basic-blocks and journey-blocks as separate journeys in the sidebar
    (e.g. 'Check Order Status' lives inside the 'Intents Where User Needs to
    Authenticate' journey-block).
    """
    out: list[dict] = []

    def walk(n: dict, depth: int) -> None:
        if not isinstance(n, dict):
            return
        t = n.get("type")
        if t in BLOCK_TYPES:
            name = _block_label(n)
            if name:
                uuid = (n.get("$") or {}).get("uuid") or ""
                out.append({
                    "uuid":     uuid,
                    "type":     t,
                    "name":     name,
                    "depth":    depth,
                    "markdown": block_to_markdown(n),
                })
        for c in n.get("children") or []:
            walk(c, depth + 1)

    walk((editor_state or {}).get("root") or {}, 0)
    return out
