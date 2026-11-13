"""Composite-key extraction and component-wise binary encoding.

A join key has one or more components, each configured as ``string`` or
``integer``.  A key object is an ordered tuple of ``(type_tag, value)`` pairs.

Encoding rules (applied per component -- the composite key is *never*
flattened into a delimiter-joined string, so business identifiers that
themselves contain delimiters cannot cross keys):

* integer components: tag ``I`` followed by the canonical decimal form of the
  arbitrary-precision JSON integer (``int`` keeps the full digit precision);
* string components: tag ``S`` followed by the UTF-8 byte length as 8-byte
  big endian and then the raw UTF-8 bytes.  Equality is byte-for-byte on the
  original text: no trimming, no case folding.

Distinct component values always have distinct encodings (the length prefix
makes prefix collisions impossible), and the byte ordering of the encoding
matches the ordering of the component tuple, so lexicographic ordering of the
encoded composite keys is a valid total order for external sorting.

``None`` stands for a key that is missing or null on at least one component.
Such keys never match -- including when both sides are empty -- and are kept
out of the join comparison entirely.
"""

TAG_INTEGER = 0x01
TAG_STRING = 0x02
TAG_NULL = 0x00

TYPE_INTEGER = "integer"
TYPE_STRING = "string"


def validate_config(config):
    """Validate a join configuration; return the normalized component list."""
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    left_keys = config.get("left_keys")
    right_keys = config.get("right_keys")
    types = config.get("key_types")
    if not isinstance(left_keys, list) or not left_keys:
        raise ValueError("config.left_keys must be a non-empty list of field names")
    if not isinstance(right_keys, list) or not right_keys:
        raise ValueError("config.right_keys must be a non-empty list of field names")
    if not isinstance(types, list) or not types:
        raise ValueError("config.key_types must be a non-empty list")
    if not (len(left_keys) == len(right_keys) == len(types)):
        raise ValueError(
            "left_keys, right_keys and key_types must have the same length"
        )
    components = []
    for i, (lf, rf, tp) in enumerate(zip(left_keys, right_keys, types)):
        if not isinstance(lf, str) or not isinstance(rf, str):
            raise ValueError(f"component {i}: key field names must be strings")
        if tp not in (TYPE_INTEGER, TYPE_STRING):
            raise ValueError(
                f"component {i}: type must be 'string' or 'integer', got {tp!r}"
            )
        components.append((lf, rf, tp))
    return components


def _encode_component(type_name, value):
    if type_name == TYPE_INTEGER:
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        return bytes([TAG_INTEGER]) + str(value).encode("ascii")
    # string: bool / int / float / list / dict / None are all wrong
    if not isinstance(value, str):
        return None
    raw = value.encode("utf-8")
    return bytes([TAG_STRING]) + len(raw).to_bytes(8, "big") + raw


def extract_key_for_row(obj, components, side, row):
    """Build the encoded composite key for one parsed JSON object.

    Returns ``None`` when any component field is missing or null (no match,
    not an error).  Raises ``KeyError_`` for present, non-null values whose
    JSON type disagrees with the configured component type; the error carries
    the 1-based physical input row.
    """
    from .errors import KeyError_

    field_idx = 0 if side == "left" else 1
    parts = []
    for component in components:
        field = component[field_idx]
        type_name = component[2]
        if field not in obj:
            return None
        value = obj[field]
        if value is None:
            return None
        encoded = _encode_component(type_name, value)
        if encoded is None:
            raise KeyError_(side, row, field, type_name, value)
        parts.append(encoded)
    return b"".join(parts)
