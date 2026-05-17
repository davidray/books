from __future__ import annotations

from copy import deepcopy


def decode_pointer(path: str) -> list[str]:
    if not path:
        return []
    if not path.startswith("/"):
        raise ValueError(f"Unsupported JSON pointer: {path}")
    return [segment.replace("~1", "/").replace("~0", "~") for segment in path[1:].split("/")]


def _container_and_key(document: object, path: str) -> tuple[object, str]:
    parts = decode_pointer(path)
    if not parts:
        raise ValueError("Cannot mutate the document root")

    current = document
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
            continue
        current = current[part]
    return current, parts[-1]


def _get(document: object, path: str) -> object:
    current = document
    for part in decode_pointer(path):
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def _set_value(container: object, key: str, value: object, *, insert: bool) -> None:
    if isinstance(container, list):
        if key == "-":
            container.append(value)
            return

        index = int(key)
        if insert:
            container.insert(index, value)
        else:
            container[index] = value
        return

    container[key] = value


def _remove_value(container: object, key: str) -> object:
    if isinstance(container, list):
        return container.pop(int(key))
    return container.pop(key)


def apply_text_delta(original: str, operations: list[dict[str, int | str]]) -> str:
    cursor = 0
    chunks: list[str] = []
    for operation in operations:
        if "retain" in operation:
            length = int(operation["retain"])
            chunks.append(original[cursor : cursor + length])
            cursor += length
        elif "insert" in operation:
            chunks.append(str(operation["insert"]))
        elif "delete" in operation:
            cursor += int(operation["delete"])
        else:
            raise ValueError(f"Unsupported text delta operation: {operation}")
    chunks.append(original[cursor:])
    return "".join(chunks)


def text_value_to_plain_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("ops"), list):
        pieces: list[str] = []
        for operation in value["ops"]:
            inserted = operation.get("insert")
            if isinstance(inserted, str):
                pieces.append(inserted)
        return "".join(pieces)
    return str(value)


def apply_patch(document: dict, operation: dict) -> None:
    op_name = operation["op"]

    if op_name == "@changeText":
        container, key = _container_and_key(document, operation["path"])
        existing = ""
        if isinstance(container, list):
            index = int(key)
            existing = text_value_to_plain_text(container[index]) if index < len(container) else ""
            container[index] = apply_text_delta(existing, operation["value"])
        else:
            existing = text_value_to_plain_text(container.get(key, ""))
            container[key] = apply_text_delta(existing, operation["value"])
        return

    if op_name == "move":
        from_container, from_key = _container_and_key(document, operation["from"])
        moved = _remove_value(from_container, from_key)
        target_container, target_key = _container_and_key(document, operation["path"])
        _set_value(target_container, target_key, moved, insert=True)
        return

    container, key = _container_and_key(document, operation["path"])

    if op_name == "add":
        _set_value(container, key, deepcopy(operation.get("value")), insert=isinstance(container, list))
        return

    if op_name == "replace":
        _set_value(container, key, deepcopy(operation.get("value")), insert=False)
        return

    if op_name == "remove":
        _remove_value(container, key)
        return

    raise ValueError(f"Unsupported patch operation: {op_name}")