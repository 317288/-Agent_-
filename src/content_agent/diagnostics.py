"""Public-action diagnostics; never log provider reasoning fields or credentials."""
import json
import re


PRIVATE_KEYS = {"reasoning", "reasoning_content", "analysis", "chain_of_thought", "thinking", "authorization", "api_key", "password", "secret"}


def public_value(value, secrets=()):
    if isinstance(value, dict):
        return {k: ("[redacted]" if k.lower() in PRIVATE_KEYS else public_value(v, secrets)) for k, v in value.items()}
    if isinstance(value, list):
        return [public_value(v, secrets) for v in value]
    if isinstance(value, str):
        value = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "[redacted reasoning]", value, flags=re.S | re.I)
        if re.search(r"<think(?:ing)?>", value, flags=re.I):
            value = re.sub(r"<think(?:ing)?>.*", "[redacted reasoning]", value, flags=re.S | re.I)
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted key]")
        return value
    return value


def public_content(text, secrets=()):
    """Return final message.content, sanitizing explicit private fields if mis-emitted."""
    if not isinstance(text, str):
        return {"content": None, "redacted": False, "note": "最终动作内容不是字符串"}
    try:
        value = json.loads(text)
        safe = public_value(value, secrets)
        if safe != value:
            return {"content": json.dumps(safe, ensure_ascii=False), "redacted": True}
    except (ValueError, TypeError):
        # Malformed private JSON cannot safely be retained verbatim.
        if re.search(r'"(?:reasoning_content|reasoning|analysis|chain_of_thought|thinking|authorization|api_key|password|secret)"\s*:', text, re.I):
            return {"content": "[malformed content with private field withheld]", "redacted": True}
    safe = public_value(text, secrets)
    return {"content": safe, "redacted": safe != text}
