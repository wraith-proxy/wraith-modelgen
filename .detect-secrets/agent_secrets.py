import re

from detect_secrets.plugins.base import RegexBasedDetector

# Literal values only: "$VAR" / "${VAR}" references are fine.
_VAL = r"""["']?(?![$<{\s"'])[^\s"'\\]{8,}"""


class AgentCommandSecretDetector(RegexBasedDetector):
    secret_type = "Secret typed into a command"  # pragma: allowlist secret
    denylist = (
        re.compile(r"(?i)\bPGPASSWORD=" + _VAL),
        re.compile(r"(?i)\bsshpass\s+-p\s*" + _VAL),
        re.compile(r"(?i)\bapi-key:\s*" + _VAL),
        re.compile(r"(?i)\bmysql\b[^\n]*\s-p" + r"(?![\s$])\S{4,}"),
        re.compile(r"(?i)\bauthorization:\s*bearer\s+" + _VAL),
    )
