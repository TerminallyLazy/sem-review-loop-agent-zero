from __future__ import annotations

import re
import unicodedata


class SanitizationError(ValueError):
    """Raised when agent-supplied review prose is not safe plain text."""


MARKUP_PATTERNS = (
    re.compile(r"```|`[^`]*`"),
    re.compile(r"^\s*#{1,6}\s", re.MULTILINE),
    re.compile(r"\[[^\]]+\]\([^)]+\)"),
    re.compile(r"<[^>]+>"),
)
_CREDENTIAL_LABEL = (
    r"(?:api[\s_-]*key|(?:access|refresh|session|id)[\s_-]*token|token|"
    r"auth(?:orization)?|credentials?|client[\s_-]*secret|secret|"
    r"password|passwd|private[\s_-]*key|"
    r"aws[\s_-]*secret[\s_-]*access[\s_-]*key)"
)
CREDENTIAL_PATTERN = re.compile(
    rf"(?i)\b{_CREDENTIAL_LABEL}\s*(?::|=)"
)
CREDENTIAL_IS_PATTERN = re.compile(
    rf"(?i)\b{_CREDENTIAL_LABEL}\s+\bis\b\s+"
    r"(?!(?:absent|available|configured|disabled|enabled|expired|invalid|"
    r"missing|optional|present|redacted|required|revoked|scoped|"
    r"unavailable|unset|valid)\b)\S+"
)
TOKEN_PATTERN = re.compile(
    r"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\bsk-[A-Za-z0-9_-]{8,}|"
    r"\bghp_[A-Za-z0-9]{8,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{8,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{8,}|"
    r"\bAKIA[A-Z0-9]{8,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
URL_USERINFO_PATTERN = re.compile(r"(?i)https?://[^\s/@]+:[^\s/@]+@")
INSTRUCTION_PATTERN = re.compile(
    r"(?i)(?:\b(?:ignore|disregard|forget)\s+"
    r"(?:(?:all|the|any|everything)\s+){0,2}"
    r"(?:above|before|earlier|previous|prior|instructions?|messages?|rules?|"
    r"context)\b|"
    r"\b(?:override|bypass|disable|evade)\s+"
    r"(?:(?:all|the|any)\s+)?"
    r"(?:instructions?|rules?|polic(?:y|ies)|safety|guardrails?|checks?|"
    r"controls?)\b|"
    r"\b(?:publish|share|send|upload|transmit|leak|expose|extract|export|"
    r"exfiltrate|reveal)\s+"
    r"(?:(?:all|the|any)\s+)?"
    r"(?:sensitive|secret|private|confidential|credentials?|tokens?|keys?|"
    r"passwords?|data|information)\b|"
    r"system prompt|developer message|"
    r"execute this command|run this command|"
    r"follow these instructions|reveal (?:the )?(?:prompt|secret)|"
    r"act as (?:an? )?|"
    r"^\s*(?:please\s+)?(?:obey|follow|execute|run|delete|remove|"
    r"reveal|send|upload|exfiltrate)\b)"
)
IMPERATIVE_VERBS = frozenset(
    {
        "apply",
        "bypass",
        "write",
        "open",
        "change",
        "fix",
        "copy",
        "move",
        "read",
        "store",
        "create",
        "edit",
        "modify",
        "replace",
        "delete",
        "remove",
        "execute",
        "run",
        "follow",
        "obey",
        "reveal",
        "send",
        "upload",
        "download",
        "disregard",
        "expose",
        "export",
        "exfiltrate",
        "extract",
        "forget",
        "ignore",
        "leak",
        "override",
        "publish",
        "share",
        "stage",
        "commit",
        "push",
        "transmit",
        "invoke",
        "call",
        "use",
    }
)
DECLARATIVE_PREDICATES = frozenset(
    {
        "appeared",
        "appears",
        "are",
        "became",
        "becomes",
        "had",
        "has",
        "have",
        "is",
        "remain",
        "remained",
        "remains",
        "seem",
        "seemed",
        "seems",
        "stay",
        "stayed",
        "stays",
        "was",
        "were",
    }
)
NON_SUBJECT_WORDS = frozenset(
    {
        "a",
        "all",
        "an",
        "current",
        "final",
        "failing",
        "its",
        "my",
        "our",
        "selected",
        "suggested",
        "that",
        "the",
        "these",
        "this",
        "those",
        "updated",
        "your",
    }
)
WORD_PATTERN = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*")
UNICODE_WORD_PATTERN = re.compile(r"[^\W\d_]+", re.UNICODE)
CLAUSE_BOUNDARY_PATTERN = re.compile(r"(?<=[.!?;:])\s*")
DIRECTED_INSTRUCTION_PATTERN = re.compile(
    r"(?i)(?:"
    r"(?:^|[.!?;:]\s*)\s*(?:agent|assistant)\s*[:,]\s*[A-Za-z]|"
    r"\b(?:could|would|can|will)\s+you\s+(?:please\s+)?[A-Za-z]+\b|"
    r"\b(?:you|(?:the\s+)?agent|assistant)\s+"
    r"(?:must|should|shall|will|need(?:s)?\s+to|"
    r"have\s+to|has\s+to|is\s+to|are\s+to)\b)"
)
IMPERATIVE_PATTERN = re.compile(
    r"(?i)(?:"
    r"^\s*(?:please|kindly)\b|"
    r"\b(?:you|(?:the\s+)?agent|assistant)\s+"
    r"(?:must|should|shall|will|need(?:s)?\s+to|have\s+to)\b|"
    r"^\s*(?:apply|write|open|change|fix|copy|move|read|store|"
    r"create|edit|modify|replace|delete|remove|execute|run|follow|obey|"
    r"reveal|send|upload|download|bypass|disregard|expose|export|"
    r"exfiltrate|extract|forget|ignore|leak|override|publish|share|stage|"
    r"commit|push|transmit|invoke|call|use)\s+"
    r"(?:the|this|that|these|those|a|an|all|my|your|our|its|"
    r"current|selected|suggested|updated|final|failing)\b)"
)
CALL_SHAPE_PATTERN = re.compile(
    r"(?:"
    r"\b[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[^\]\n]*\])*\(|"
    r"[\])]\s*\()"
)
SOURCE_PATTERN = re.compile(
    r"(?i)(^\s*(?:async\s+)?(?:def|class|function|import|from|"
    r"const|let|var|package|namespace|using)\s+|"
    r"\b[A-Za-z_][A-Za-z0-9_.]*\s*=\s*[^\s=]|"
    r"=>|::=|;\s*$|\{\s*['\"A-Za-z_]|"
    r"\b(?:return|throw|raise|await)\b[^.]*[();{}]|"
    r"^\s*[A-Za-z_][A-Za-z0-9_.]*"
    r"(?:\s*\[[^\]\n]+\])*\s*\([^;\n]*\)\s*;?\s*$)"
)
DIFF_PATTERN = re.compile(
    r"(?i)(?:^|\s)(?:diff --git|index [0-9a-f]+\.\.[0-9a-f]+|"
    r"@@\s*-\d|---\s+(?:a/|/)|\+\+\+\s+(?:b/|/))|"
    r"^\s*[+-]\s*(?:def|class|function|return|import|from|const|let|var)\b"
)
PATH_PATTERN = re.compile(
    r"(?i)(?:"
    r"(?<![A-Za-z0-9_])/(?:[^\s/]+(?:/[^\s/]*)*)|"
    r"(?<![A-Za-z0-9_])~[\\/][^\s]+|"
    r"(?<![A-Za-z0-9_])\\\\[^\\/\s]+[\\/][^\s]+|"
    r"\b[A-Za-z]:[\\/]|"
    r"(?:^|[\\/])\.\.(?:[\\/]|$)|"
    r"(?:^|\s)(?:\./)?[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+|"
    r"\b[A-Za-z0-9_-]+\.(?:py|pyi|js|jsx|ts|tsx|swift|rs|go|java|"
    r"c|cc|cpp|h|hpp|rb|php|cs|kt|kts|scala|sh|bash|zsh|yaml|yml|json)\b|"
    r"https?://)"
)


def _is_safe_verb_initial_declarative(clause: str) -> bool:
    """Allow an unambiguous noun phrase such as `Read access remains scoped`."""

    words = [word.casefold() for word in WORD_PATTERN.findall(clause)]
    if len(words) < 3 or words[0] not in IMPERATIVE_VERBS:
        return False
    for predicate_index in (1, 2, 3):
        if (
            predicate_index >= len(words) - 1
            or words[predicate_index] not in DECLARATIVE_PREDICATES
        ):
            continue
        if predicate_index == 1:
            return True
        subject = words[1:predicate_index]
        if subject and not any(word in NON_SUBJECT_WORDS for word in subject):
            return True
    return False


def _contains_instruction_like_text(value: str) -> bool:
    if DIRECTED_INSTRUCTION_PATTERN.search(value):
        return True
    for clause in CLAUSE_BOUNDARY_PATTERN.split(value):
        match = WORD_PATTERN.search(clause.lstrip(" \"'([{"))
        if match is None or match.group(0).casefold() not in IMPERATIVE_VERBS:
            continue
        if not _is_safe_verb_initial_declarative(clause):
            return True
    return False


def _contains_mixed_latin_cyrillic_word(value: str) -> bool:
    for word in UNICODE_WORD_PATTERN.findall(value):
        has_latin = False
        has_cyrillic = False
        for character in word:
            name = unicodedata.name(character, "")
            has_latin = has_latin or "LATIN" in name
            has_cyrillic = has_cyrillic or "CYRILLIC" in name
            if has_latin and has_cyrillic:
                return True
    return False


def sanitize_plain_line(
    value: object,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    """Normalize a bounded prose line and reject executable or sensitive text."""

    if not isinstance(value, str):
        raise SanitizationError("value must be text")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 0:
        raise SanitizationError("maximum must be a non-negative integer")
    preflight_limit = max(maximum * 4, 64)
    if len(value) > preflight_limit:
        raise SanitizationError(f"value exceeds {maximum} bytes")
    if any(
        ord(character) < 32
        or ord(character) == 127
        or unicodedata.category(character) in {"Cf", "Cs"}
        for character in value
    ):
        raise SanitizationError("control characters are not allowed")

    normalized = " ".join(value.strip().split())
    analysis = unicodedata.normalize("NFKC", normalized)
    if not normalized and not allow_empty:
        raise SanitizationError("value may not be empty")
    try:
        encoded_length = len(normalized.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise SanitizationError("value must be valid UTF-8 text") from exc
    if encoded_length > maximum:
        raise SanitizationError(f"value exceeds {maximum} bytes")
    if _contains_mixed_latin_cyrillic_word(analysis):
        raise SanitizationError("mixed-script text is not allowed")
    if any(pattern.search(analysis) for pattern in MARKUP_PATTERNS):
        raise SanitizationError("Markdown or HTML is not allowed")
    if (
        CREDENTIAL_PATTERN.search(analysis)
        or CREDENTIAL_IS_PATTERN.search(analysis)
        or TOKEN_PATTERN.search(analysis)
        or URL_USERINFO_PATTERN.search(analysis)
    ):
        raise SanitizationError("credential-shaped text is not allowed")
    if (
        INSTRUCTION_PATTERN.search(analysis)
        or IMPERATIVE_PATTERN.search(analysis)
        or _contains_instruction_like_text(analysis)
    ):
        raise SanitizationError("instruction-like text is not allowed")
    if DIFF_PATTERN.search(analysis):
        raise SanitizationError("raw diff text is not allowed")
    if PATH_PATTERN.search(analysis):
        raise SanitizationError("filesystem paths or URLs are not allowed")
    if CALL_SHAPE_PATTERN.search(analysis) or SOURCE_PATTERN.search(analysis):
        raise SanitizationError("raw source code is not allowed")
    return normalized
