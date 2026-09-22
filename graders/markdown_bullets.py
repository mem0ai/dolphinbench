"""Count Markdown bullets and visible, whitespace-separated words."""

from markdown_it import MarkdownIt


def validate_limits(limits: dict) -> None:
    allowed = {"max_items", "exact_items", "max_words", "only_bullets"}
    if not isinstance(limits, dict) or not limits or set(limits) - allowed:
        raise ValueError("bullet comparison needs supported limits")
    if "max_items" in limits and "exact_items" in limits:
        raise ValueError("use either max_items or exact_items")
    for key, value in limits.items():
        if key == "only_bullets":
            if type(value) is not bool:
                raise ValueError("only_bullets must be Boolean")
        elif type(value) is not int or value < 1:
            raise ValueError("bullet and word limits must be positive integers")


def matches_bullets(text: object, limits: dict) -> bool:
    if not isinstance(text, str):
        return False
    tokens = MarkdownIt("commonmark").parse(text)
    items: list[list[str]] = []
    lists: list[str] = []
    active: list[int | None] = []
    only = limits.get("only_bullets", False)
    for token in tokens:
        kind = token.type
        if kind in {"bullet_list_open", "ordered_list_open"}:
            lists.append(kind)
            if only and kind != "bullet_list_open":
                return False
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
        elif kind == "list_item_open":
            if lists[-1] == "bullet_list_open":
                active.append(len(items))
                items.append([])
            else:
                active.append(None)
        elif kind == "list_item_close":
            active.pop()
        elif kind == "inline":
            if active and active[-1] is not None:
                visible = []
                for child in token.children or []:
                    if child.type in {"text", "code_inline"}:
                        visible.append(child.content)
                    elif child.type in {"softbreak", "hardbreak"}:
                        visible.append(" ")
                    elif child.type in {"html_inline", "image"}:
                        return False
                items[active[-1]].append("".join(visible))
            elif only:
                return False
        elif only and kind not in {"paragraph_open", "paragraph_close"}:
            return False
    words = [" ".join(parts).split() for parts in items]
    if not words or any(not item for item in words):
        return False
    if "max_items" in limits and len(words) > limits["max_items"]:
        return False
    if "exact_items" in limits and len(words) != limits["exact_items"]:
        return False
    return "max_words" not in limits or all(len(item) <= limits["max_words"] for item in words)
