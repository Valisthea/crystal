"""Go regex fallback, and the vocabulary the tree-sitter Go front-end shares.

Go has no contracts, so the front-end has to decide what a contract, an entry
point and a state variable *are* before any detector can be true on Go code.
The decision (argued in full in `go_ts.py`, which is the primary front-end):

    type Service struct { repo Repo; mu sync.Mutex; n int }  -> Contract(kind="struct")
        field `repo`, `mu`, `n`                                -> StateVar (visibility "field")
        func (s *Service) Run(ctx, x)   (exported)             -> Function (visibility "public")
        func (s *Service) helper(x)     (unexported)           -> Function (visibility "internal")
    package foo { var registry = ...; func Helper() }          -> Contract(kind="package")
        var registry                                           -> StateVar (visibility "package")
    type Repo interface { Save(ctx, q) error }                 -> Contract(kind="interface")
    func(w http.ResponseWriter, r *http.Request)               -> Function(kind="handler",
                                                                   visibility "external")

This module is the fallback used when `tree-sitter-go` is not installed or
tree-sitter is disabled. It is a structural reader over comment-stripped
source, not a parser: it recovers declarations, the statement skeleton (`if`,
`for`, `switch`, `return`, calls, assignments) and the receiver-field state
model, which is enough for the detectors to run. What it does *not* recover is
listed in `GO_REGEX_LIMITATIONS` and surfaced by `crystal doctor`, because a
detector that stays quiet for lack of type information looks exactly like a
clean result.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .. import ir as I
from ..models import GO, Contract, ContractType, Function, Parameter, StateVar
from .base import ParseResult, identifiers_in, matching_brace, split_arguments
from .base import strip_comments

PARSER_NAME = "regex"

# --------------------------------------------------------------------------
# Shared vocabulary (imported by go_ts)
# --------------------------------------------------------------------------

# Predeclared functions. They run no user code and reach no service state.
GO_BUILTIN_FUNCS = frozenset({
    "append", "cap", "clear", "close", "complex", "copy", "delete", "imag",
    "len", "make", "max", "min", "new", "panic", "print", "println", "real",
    "recover",
})

# Predeclared types: `uint64(x)` is a conversion spelled like a call.
GO_BUILTIN_TYPES = frozenset({
    "any", "bool", "byte", "complex64", "complex128", "error", "float32",
    "float64", "int", "int8", "int16", "int32", "int64", "rune", "string",
    "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
})

# Packages whose functions are data plumbing — formatting, encoding,
# arithmetic, logging, time, hashing. They run none of the analysed service's
# code and reach none of its state, so a call into them is `builtin`, the same
# way the Rust front-end files `encode`/`saturating_add` under conversions and
# read-only calls. Matched on the import's last path segment (or its alias).
PURE_PACKAGES = frozenset({
    "atomic", "base64", "big", "binary", "bytes", "cmp", "context", "crypto",
    "ecdsa", "errors", "filepath", "fmt", "hex", "hexutil", "json", "log",
    "logrus", "maps", "math", "os", "path", "rand", "reflect", "regexp",
    "sha256", "sha3", "slices", "sort", "strconv", "strings", "time",
    "unicode", "url", "utf8", "xml", "zap", "zerolog",
})

# Value accessors and formatters by Go convention (`Stringer`, `error`,
# `big.Int` readers, `time.Time` comparisons). Called with at most one
# argument on a value, they read it and run nothing else — the Go spelling
# of the Rust front-end's read-only calls.
STRINGER_METHODS = frozenset({
    "String", "Error", "Hex", "Bytes", "Int64", "Uint64", "Float64", "Cmp",
    "Sign", "Text", "Len", "Unix", "UnixNano", "UnixMilli", "Before", "After",
    "Equal", "IsZero", "Format", "BitLen", "IsInt64", "IsUint64",
})

# `net/http` helpers that write the response. The response is the handler's
# return value in every sense Crystal cares about: it is neither state nor a
# transfer of control, so it is `builtin` like a call on the writer itself.
HTTP_RESPONSE_FUNCS = frozenset({
    "Error", "Redirect", "NotFound", "ServeFile", "ServeContent", "SetCookie",
    "StatusText", "MaxBytesReader", "NotFoundHandler", "StripPrefix",
})

# Mutex operations. A lock guards *code*, not a nameable set of fields, so
# Crystal records the operation (note "lock"/"unlock") without inventing a
# mapping from the mutex to the state it protects.
LOCK_METHODS = frozenset({
    "Lock", "Unlock", "RLock", "RUnlock", "TryLock", "TryRLock",
})
LOCK_NOTES = {"Lock": "lock", "RLock": "lock", "TryLock": "lock",
              "TryRLock": "lock", "Unlock": "unlock", "RUnlock": "unlock"}

# Calls that end the goroutine or the process: the Go spelling of `revert`.
TERMINATING_SELECTORS = frozenset({
    ("log", "Fatal"), ("log", "Fatalf"), ("log", "Fatalln"), ("log", "Panic"),
    ("log", "Panicf"), ("log", "Panicln"), ("os", "Exit"),
})

# Decoders that fill a value through a pointer argument (`Decode(&req)`).
# A local passed by address to one of these is *derived from the request*;
# the front-end lowers that into an assignment so the symbolic engine sees the
# derivation (the pointer write itself is invisible to it).
DECODE_CALLS = frozenset({
    "Decode", "Unmarshal", "DecodeRequest", "DecodeJSON", "DecodeBody",
    "ReadJSON", "ReadBody", "Bind", "BindJSON", "BindQuery", "BindUri",
    "ShouldBind", "ShouldBindJSON", "ShouldBindQuery", "ShouldBindUri",
    "ParseRequest", "ParseBody", "NewDecoder",
})

# Parameter types that carry the untrusted request, across the common
# routers; a function taking one of these with a response writer is a
# handler whatever it is called.
REQUEST_TYPES = ("*http.Request", "http.Request", "*gin.Context", "gin.Context",
                 "echo.Context", "*fiber.Ctx", "fiber.Ctx", "*fasthttp.RequestCtx")
RESPONSE_TYPES = ("http.ResponseWriter",)
# Result types of a handler *factory*: `func NewX(uc) http.HandlerFunc` whose
# body returns the closure that actually serves the request.
HANDLER_RESULT_TYPES = ("http.HandlerFunc", "http.Handler", "gin.HandlerFunc",
                        "echo.HandlerFunc", "fiber.Handler")
CONTEXT_TYPES = ("context.Context",)

GO_REGEX_LIMITATIONS = (
    "types are not resolved, so a struct is not linked to the interfaces it "
    "satisfies and a call through an interface-typed field is not attributed "
    "to its implementation",
    "package-level declarations are grouped per file, not per package, so a "
    "helper defined in another file of the same package is not resolved",
    "the statement skeleton is recovered by a line/brace scanner: a "
    "multi-line expression or an unusual layout can split a statement",
    "closures are followed only in the handler-factory shape "
    "(`return func(w http.ResponseWriter, r *http.Request) {..}`)",
    "`switch`/`select` cases are lowered as a chain of conditions on the "
    "case text; type switches are not distinguished",
    "generic type parameters are stripped rather than modelled",
    "purity is not propagated through calls, so a function that only calls "
    "value builders is still `stateful` and its call sites are compared as "
    "state transitions",
)


def typed_receiver(identifier: str, declared: str) -> str:
    """Spell a parameter receiver with its declared type: `Ledger(repo)`.

    Two handlers each calling `useCase.Run(ctx, id)` call two different
    objects of two different types; the receiver text is what groups sibling
    call sites, so it must say which. Go's own conversion syntax is the
    spelling — the Solidity front-end records `IBridge(bridge)` the same way.
    """
    base = (declared or "").strip().lstrip("*")
    if re.fullmatch(r"(?:[A-Za-z_]\w*\.)?[A-Z]\w*(?:\[[^\]]*\])?", base):
        return f"{base}({identifier})"
    return identifier


def is_exported(name: str) -> bool:
    return bool(name) and name[0].isupper()


def is_test_file(path) -> bool:
    return str(path).lower().endswith("_test.go")


def package_label_for_path(path, package_name: str) -> str:
    """A readable package identity when no catalog disambiguates it.

    `main` says nothing on its own, so the directory is used: `cmd/withdraw`
    is `withdraw_main`.
    """
    if package_name == "main":
        parent = Path(path).parent.name
        return f"{parent}_main" if parent else "main"
    return package_name or Path(path).parent.name or "package"


@lru_cache(maxsize=256)
def module_path_for(directory: str) -> str:
    """The `module` line of the nearest go.mod, or "" when there is none."""
    current = Path(directory)
    for candidate in [current, *current.parents]:
        manifest = candidate / "go.mod"
        try:
            if manifest.is_file():
                text = manifest.read_text(encoding="utf-8", errors="ignore")
                match = re.search(r"(?m)^module\s+(\S+)", text)
                return match.group(1) if match else ""
        except OSError:
            return ""
    return ""


def canonical_receiver(text: str, receiver: str | None) -> str:
    """Spell the method receiver `self` so the language-neutral engine reads
    `useCase.repo` as the field `repo`, the way it already reads `self.x`."""
    if not receiver or not text:
        return text
    return re.sub(rf"(?<![\w.]){re.escape(receiver)}\.", "self.", text)


def blank_raw_strings(text: str) -> str:
    """Blank backtick strings (struct tags, templates) keeping line structure.

    `strip_comments` knows `"` and `'`; a raw string can hold `//`, braces
    and quotes, any of which would derail the brace scanner.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "`":
            end = text.find("`", index + 1)
            end = length if end < 0 else end + 1
            out.append("`" + "".join(c if c == "\n" else " " for c in text[index + 1:end - 1]) + "`")
            index = end
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_params(text: str) -> list[Parameter]:
    """`ctx context.Context, a, b string, opts ...Opt` -> named parameters.

    Go lets several names share one type; the type is carried back over the
    names that precede it.
    """
    chunks = split_arguments(text or "")
    parsed: list[tuple[str, str]] = []
    for chunk in chunks:
        tokens = chunk.strip().split(None, 1)
        if not tokens:
            continue
        if len(tokens) == 1:
            parsed.append((tokens[0], ""))
        else:
            parsed.append((tokens[0], " ".join(tokens[1].split())))
    # A chunk with no type borrows the next declared type (`a, b string`).
    out: list[Parameter] = []
    pending: list[str] = []
    for name, type_name in parsed:
        if not type_name:
            pending.append(name)
            continue
        for earlier in pending:
            out.append(Parameter(earlier, type_name))
        pending = []
        out.append(Parameter(name, type_name))
    for name in pending:
        # Unnamed parameters (an interface method `Save(context.Context) error`).
        out.append(Parameter("", name))
    return out


def classify_shape(params: list[Parameter], result_text: str) -> str:
    """`handler`, `handler-factory` or `` for an ordinary function.

    A handler has *exactly* the router's signature — `(http.ResponseWriter,
    *http.Request)`, or the framework context alone. A helper that takes the
    pair plus something else (`DecodeRequest(w, req, body)`) is called by
    handlers, not served by the router.
    """
    types = [" ".join(p.type_name.split()) for p in params]
    # In this order: `http.HandlerFunc` is `(ResponseWriter, *Request)`, and a
    # method taking `(r, w)` is a helper that handlers call, not one the
    # router serves.
    if len(types) == 2 and types[0] in RESPONSE_TYPES and types[1] in REQUEST_TYPES:
        return "handler"
    if len(types) == 1 and types[0] in REQUEST_TYPES and \
            types[0].startswith(("*gin.", "gin.", "echo.", "*fiber.", "fiber.")):
        return "handler"
    cleaned = " ".join((result_text or "").split())
    if cleaned in HANDLER_RESULT_TYPES:
        return "handler-factory"
    return ""


def user_inputs_for(params: list[Parameter], shape: str, visibility: str) -> list[str]:
    """Parameters an untrusted caller chooses.

    The request object is chosen by whoever sent it; the response writer and
    the context are supplied by the framework, the way `origin` is by the
    dispatch layer in Substrate.
    """
    if shape != "handler" and visibility not in {"public", "external"}:
        return []
    out: list[str] = []
    for parameter in params:
        type_name = " ".join(parameter.type_name.split())
        if not parameter.name or parameter.name == "_":
            continue
        if type_name in RESPONSE_TYPES or type_name in CONTEXT_TYPES:
            continue
        out.append(parameter.name)
    return out


# --------------------------------------------------------------------------
# Regex fallback
# --------------------------------------------------------------------------

PACKAGE_RE = re.compile(r"(?m)^package\s+([A-Za-z_]\w*)")
IMPORT_BLOCK_RE = re.compile(r"(?m)^import\s*\(")
IMPORT_LINE_RE = re.compile(r"(?m)^import\s+(?:([A-Za-z_.]\w*)\s+)?\"([^\"]+)\"")
IMPORT_SPEC_RE = re.compile(r"(?:([A-Za-z_.]\w*)\s+)?\"([^\"]+)\"")
FUNC_RE = re.compile(
    r"(?m)^func\s+(?:\(\s*([A-Za-z_]\w*)?\s*\*?\s*([A-Za-z_]\w*)(?:\[[^\]]*\])?\s*\)\s*)?"
    r"([A-Za-z_]\w*)(?:\[[^\]]*\])?\s*\("
)
STRUCT_RE = re.compile(r"(?m)^type\s+([A-Za-z_]\w*)(?:\[[^\]]*\])?\s+struct\s*\{")
INTERFACE_RE = re.compile(r"(?m)^type\s+([A-Za-z_]\w*)(?:\[[^\]]*\])?\s+interface\s*\{")
VAR_LINE_RE = re.compile(r"(?m)^(var|const)\s+([A-Za-z_]\w*)\s*([^=\n]*?)\s*(?:=\s*(.*))?$")
VAR_BLOCK_RE = re.compile(r"(?m)^(var|const)\s*\(")
FIELD_RE = re.compile(r"^([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s+(.+?)\s*$")
CALLEE_RE = re.compile(r"([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\s*(?:\[[^\]]*\])?\s*\(")
CHAIN_RE = re.compile(r"\s*(?:\[[^\]]*\]\s*)*\.\s*([A-Za-z_]\w*)\s*\(")
NOT_CALLEES = {"if", "for", "switch", "select", "return", "func", "go", "defer",
               "range", "case", "chan", "map", "struct", "interface", "else"}
CLOSURE_RE = re.compile(r"^return\s+func\s*\(([^)]*)\)\s*(\([^)]*\)|[^{]*?)\s*\{", re.S)
COMPOUND_OPS = ("+=", "-=", "*=", "/=", "%=", "|=", "&=", "^=", "<<=", ">>=", "&^=")


def _matching_paren(text: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


def _top_level_brace(text: str, start: int, end: int) -> int:
    """First `{` between start and end outside parentheses/brackets, or -1."""
    depth = 0
    for index in range(start, end):
        char = text[index]
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "{" and depth == 0:
            return index
    return -1


def _split_top(text: str, separator: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == separator and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def go_statement_ranges(scan: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split a block into statements using Go's semicolon-insertion rule.

    A newline ends a statement when the last token before it is an identifier,
    a literal, `)`, `]`, `}`, `++`, `--` or a keyword such as `return`; inside
    parentheses, brackets or braces nothing ends. `else` on the same line as
    the closing brace continues the `if`.
    """
    ranges: list[tuple[int, int]] = []
    depth = 0
    brace = 0
    index = start
    statement_start = start
    last_token = ""
    # `if x := f(); x > 1 {` and `for i := 0; i < n; i++ {`: a semicolon in a
    # control header separates its clauses, so it must not close the
    # statement before the header's brace has been seen.
    header_open = False

    def close(at: int) -> None:
        nonlocal statement_start, last_token, header_open
        if scan[statement_start:at].strip():
            ranges.append((statement_start, at))
        statement_start = at
        last_token = ""
        header_open = False

    def in_control_header() -> bool:
        return header_open and bool(
            re.match(r"\s*(if|for|switch|select)\b", scan[statement_start:index]))

    while index < end:
        char = scan[index]
        if char in "\"'`":
            quote = char
            index += 1
            while index < end and scan[index] != quote:
                index += 2 if scan[index] == "\\" and quote != "`" else 1
            index += 1
            last_token = "literal"
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
            last_token = ")"
        elif char == "{":
            if brace == 0:
                header_open = False
            brace += 1
        elif char == "}":
            brace -= 1
            last_token = "}"
        elif char == ";" and depth == 0 and brace == 0:
            if in_control_header():
                index += 1
                continue
            close(index)
            statement_start = index + 1
            index += 1
            continue
        elif char == "\n":
            if depth == 0 and brace == 0 and last_token in {"ident", "literal", ")", "}", "++", "--"}:
                close(index)
            index += 1
            continue
        if char.isalnum() or char == "_":
            word = re.match(r"[A-Za-z0-9_.]+", scan[index:end])
            token = word.group(0) if word else char
            if not scan[statement_start:index].strip() and token in {"if", "for", "switch", "select"}:
                header_open = True
            index += len(token)
            last_token = "ident"
            continue
        if scan.startswith("++", index) or scan.startswith("--", index):
            last_token = "++"
            index += 2
            continue
        if not char.isspace():
            last_token = "op" if char not in ")]}" else last_token
        index += 1
    close(end)
    return ranges


def split_assignment(raw: str) -> tuple[str, str | None, str]:
    depth = 0
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if char in "\"'`":
            quote = char
            index += 1
            while index < length and raw[index] != quote:
                index += 2 if raw[index] == "\\" and quote != "`" else 1
            index += 1
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0:
            if raw.startswith(":=", index):
                return raw[:index].strip(), ":=", raw[index + 2:].strip()
            for operator in COMPOUND_OPS:
                if raw.startswith(operator, index):
                    return raw[:index].strip(), operator, raw[index + len(operator):].strip()
            if char == "=" and not raw.startswith("==", index):
                if index and raw[index - 1] in "!<>=":
                    index += 1
                    continue
                return raw[:index].strip(), "=", raw[index + 1:].strip()
        index += 1
    return raw, None, ""


def classify_call_text(text: str, imports: dict[str, str], module: str,
                       receiver_name: str | None = None,
                       params: dict[str, str] | None = None) -> I.IRCall | None:
    """The outermost call in `text`, classified with text-level knowledge only."""
    params = params or {}
    for match in CALLEE_RE.finditer(text):
        callee = re.sub(r"\s+", "", match.group(1))
        head = callee.split(".")[0]
        if head in NOT_CALLEES or callee in NOT_CALLEES:
            continue
        opening = match.end() - 1
        close = _matching_paren(text, opening)
        receiver, name = callee.rsplit(".", 1) if "." in callee else (None, callee)
        chain_start = match.start(1)
        while True:
            chain = CHAIN_RE.match(text, close)
            if not chain:
                break
            receiver = " ".join(text[chain_start:close].split())
            name = chain.group(1)
            opening = chain.end() - 1
            close = _matching_paren(text, opening)
        arguments = tuple(split_arguments(text[opening + 1:close - 1]))
        return _classify(name, receiver, arguments, imports, module, receiver_name,
                         text, params)
    return None


def _classify(name: str, receiver: str | None, arguments, imports, module,
              receiver_name, text, params) -> I.IRCall:
    kind = I.EXTERNAL_CALL
    note_receiver = receiver
    if receiver is None:
        if name in GO_BUILTIN_FUNCS or name in GO_BUILTIN_TYPES:
            kind = I.BUILTIN_CALL
        else:
            kind = I.INTERNAL_CALL
    else:
        base = receiver.split(".")[0].split("[")[0].split("(")[0].strip("&*")
        if receiver_name and base == receiver_name and "." not in receiver.strip("&*"):
            # `useCase.helper(x)`: a method on the receiver, i.e. `self.helper`.
            kind = I.INTERNAL_CALL
            note_receiver = None
        elif receiver_name and base == receiver_name:
            note_receiver = receiver.strip("&*").split(".", 1)[1]
            kind = I.BUILTIN_CALL if name in LOCK_METHODS else I.EXTERNAL_CALL
        elif base == "self":
            stripped = receiver.strip("&*").split(".", 1)
            if len(stripped) == 1:
                kind = I.INTERNAL_CALL
                note_receiver = None
            else:
                note_receiver = stripped[1]
                kind = I.BUILTIN_CALL if name in LOCK_METHODS else I.EXTERNAL_CALL
        elif base in imports and base not in params:
            path = imports[base]
            segment = path.rsplit("/", 1)[-1]
            if module and path.startswith(module):
                kind = I.INTERNAL_CALL
                note_receiver = segment
            elif base in PURE_PACKAGES or segment in PURE_PACKAGES:
                kind = I.BUILTIN_CALL
            elif segment == "http" and name in HTTP_RESPONSE_FUNCS:
                kind = I.BUILTIN_CALL
            else:
                kind = I.EXTERNAL_CALL
        elif name in LOCK_METHODS:
            kind = I.BUILTIN_CALL
        elif base in params:
            declared = params[base]
            if declared in RESPONSE_TYPES or declared in REQUEST_TYPES or declared in CONTEXT_TYPES:
                kind = I.BUILTIN_CALL
            elif name in STRINGER_METHODS and len(arguments) <= 1:
                kind = I.BUILTIN_CALL
            elif receiver == base:
                note_receiver = typed_receiver(base, declared)
        elif name in STRINGER_METHODS and len(arguments) <= 1:
            kind = I.BUILTIN_CALL
    return I.IRCall(name, kind, 0, " ".join(text.split())[:400], note_receiver,
                    tuple(I.IRExpr("expression", a[:400], _base_of(a),
                                   identifiers=identifiers_in(a))
                          for a in arguments),
                    False)


def _base_of(text: str) -> str | None:
    cleaned = (text or "").strip().lstrip("&*")
    if cleaned.startswith("self."):
        cleaned = cleaned[5:]
    match = re.match(r"[A-Za-z_]\w*", cleaned)
    return match.group(0) if match else None


class _BodyScanner:
    """Lower a comment-stripped Go function body into the statement IR."""

    def __init__(self, raw: str, scan: str, line_base: int, fields: set[str],
                 package_vars: set[str], imports: dict[str, str], module: str,
                 receiver: str | None, request_params: set[str],
                 params: dict[str, str] | None = None):
        self.raw = raw
        self.scan = scan
        self.line_base = line_base
        self.fields = fields
        self.package_vars = package_vars
        self.imports = imports
        self.module = module
        self.receiver = receiver
        self.request_params = request_params
        self.params = params or {}
        self.unsupported: list[str] = []

    # -- helpers -----------------------------------------------------------
    def line(self, offset: int) -> int:
        return self.line_base + self.scan.count("\n", 0, offset)

    def canon(self, text: str) -> str:
        return canonical_receiver(" ".join(text.split()), self.receiver)[:400]

    def state_in(self, text: str) -> tuple[str, ...]:
        canon = canonical_receiver(text, self.receiver)
        found = {m for m in re.findall(r"(?<![\w.])self\.([A-Za-z_]\w*)", canon)
                 if m in self.fields}
        found |= {ident for ident in identifiers_in(re.sub(r"self\.[A-Za-z_]\w*", " ", canon))
                  if ident in self.package_vars}
        return tuple(sorted(found))

    def expr(self, text: str, line: int) -> I.IRExpr:
        canon = self.canon(text)
        return I.IRExpr("expression", canon, _base_of(canon),
                        identifiers=identifiers_in(canon), line=line)

    def write_base(self, target: str) -> str | None:
        canon = canonical_receiver(target.strip(), self.receiver).lstrip("*&")
        if canon.startswith("self."):
            base = _base_of(canon)
            return base if base in self.fields else None
        base = _base_of(canon)
        return base if base in self.package_vars else None

    def call(self, text: str, line: int) -> I.IRCall | None:
        call = classify_call_text(text, self.imports, self.module, self.receiver, self.params)
        if call is None:
            return None
        return I.IRCall(call.callee, call.kind, line, self.canon(text), call.receiver,
                        tuple(I.IRExpr(a.kind, self.canon(a.text), _base_of(self.canon(a.text)),
                                       identifiers=identifiers_in(self.canon(a.text)), line=line)
                              for a in call.arguments),
                        call.value_attached)

    # -- statements --------------------------------------------------------
    def block(self, start: int, end: int) -> tuple[I.IRStmt, ...]:
        out: list[I.IRStmt] = []
        for s_start, s_end in go_statement_ranges(self.scan, start, end):
            out.extend(self.statement(s_start, s_end))
        return tuple(out)

    def statement(self, start: int, end: int) -> list[I.IRStmt]:
        while start < end and self.scan[start].isspace():
            start += 1
        while end > start and self.scan[end - 1].isspace():
            end -= 1
        if start >= end:
            return []
        chunk = self.scan[start:end]
        line = self.line(start)
        head = re.match(r"[A-Za-z_]\w*", chunk)
        keyword = head.group(0) if head else ""
        # A label: `loop: for {`
        label = re.match(r"[A-Za-z_]\w*\s*:\s*(?!=)", chunk)
        if label and keyword not in {"case", "default"}:
            return self.statement(start + label.end(), end)
        if keyword == "if":
            return self._if(start, end)
        if keyword == "for":
            return self._for(start, end)
        if keyword in {"switch", "select"}:
            return self._switch(start, end, keyword)
        if chunk.startswith("{"):
            close = min(matching_brace(self.scan, start), end)
            return [I.IRStmt(I.BLOCK, line, "block", body=self.block(start + 1, close - 1))]
        if keyword in {"break", "continue", "goto", "fallthrough"}:
            return []
        raw = self.raw[start:end]
        text = self.canon(raw)
        if keyword == "return":
            value = raw[6:].strip()
            return [I.IRStmt(I.RETURN, line, text,
                             value=self.expr(value, line) if value else None,
                             call=self.call(value, line) if "(" in value else None,
                             reads=self.state_in(value))]
        if keyword in {"go", "defer"}:
            inner = raw[len(keyword):].strip()
            call = self.call(inner, line)
            return [I.IRStmt(I.CALL, line, text, call=call, reads=self.state_in(inner),
                             note=keyword)]
        if keyword == "panic":
            return [I.IRStmt(I.REVERT, line, text, reads=self.state_in(raw), note="panic")]
        if keyword in {"var", "const"}:
            body = raw[len(keyword):].strip()
            target, operator, value = split_assignment(body)
            names = target.split()[0] if target else ""
            return [I.IRStmt(I.VAR_DECL, line, text, target=self.expr(names, line),
                             operator="=" if operator else None,
                             value=self.expr(value, line) if value else None,
                             call=self.call(value, line) if "(" in value else None,
                             reads=self.state_in(value))]
        if keyword == "type":
            return []
        target, operator, value = split_assignment(raw)
        if operator:
            return self._assignment(target, operator, value, line, text)
        update = re.match(r"^(.*?)(\+\+|--)$", raw.strip())
        if update:
            target = update.group(1).strip()
            base = self.write_base(target)
            return [I.IRStmt(I.ASSIGN, line, text, target=self.expr(target, line),
                             operator="+=" if update.group(2) == "++" else "-=",
                             value=I.IRExpr("literal", "1", None, line=line),
                             writes=(base,) if base else (), reads=self.state_in(target))]
        if "<-" in raw and "(" not in raw.split("<-")[0]:
            return [I.IRStmt(I.UNKNOWN, line, text, reads=self.state_in(raw), note="send")]
        if "(" in raw:
            call = self.call(raw, line)
            if call is not None:
                if call.callee in LOCK_METHODS:
                    call = I.IRCall(call.callee, I.BUILTIN_CALL, line, call.text,
                                    call.receiver, call.arguments, False)
                    return [I.IRStmt(I.CALL, line, text, call=call,
                                     note=LOCK_NOTES.get(call.callee))]
                if (call.receiver or "").split(".")[0] in {"log", "os"} and \
                        (call.receiver, call.callee) in TERMINATING_SELECTORS:
                    return [I.IRStmt(I.REVERT, line, text, reads=self.state_in(raw),
                                     note=f"{call.receiver}.{call.callee}")]
                statements = [I.IRStmt(I.CALL, line, text, call=call, reads=self.state_in(raw))]
                statements.extend(self._decoded_locals(call, line))
                return statements
        return [I.IRStmt(I.UNKNOWN, line, text, reads=self.state_in(raw))]

    def _decoded_locals(self, call: I.IRCall, line: int) -> list[I.IRStmt]:
        """`Decode(&x)` with a request in the argument list: `x` is request-derived."""
        if call.callee not in DECODE_CALLS or not self.request_params:
            return []
        sources = [a.text for a in call.arguments
                   if _base_of(a.text) in self.request_params]
        if not sources and call.callee not in {"Decode", "Unmarshal"}:
            return []
        source = sources[0] if sources else next(iter(sorted(self.request_params)))
        out: list[I.IRStmt] = []
        for argument in call.arguments:
            if argument.text.startswith("&"):
                local = argument.text[1:].strip()
                if re.fullmatch(r"[A-Za-z_]\w*", local):
                    out.append(I.IRStmt(
                        I.ASSIGN, line, f"{local} = {source}",
                        target=I.IRExpr("identifier", local, local, identifiers=(local,), line=line),
                        operator="=", value=self.expr(source, line),
                        note=f"decoded-from-request:{call.callee}",
                    ))
        return out

    def _assignment(self, target, operator, value, line, text) -> list[I.IRStmt]:
        targets = [t.strip() for t in _split_top(target, ",")]
        writes = tuple(b for b in (self.write_base(t) for t in targets) if b)
        reads = set(self.state_in(value))
        if operator not in {"=", ":="}:
            reads |= set(self.state_in(target))
        call = self.call(value, line) if "(" in value else None
        declared = operator == ":="
        statement = I.IRStmt(
            I.VAR_DECL if declared else I.ASSIGN, line, text,
            target=self.expr(target, line),
            operator="=" if declared else operator,
            value=self.expr(value, line), call=call,
            writes=writes, reads=tuple(sorted(reads)),
        )
        out = [statement]
        if call is not None:
            out.extend(self._decoded_locals(call, line))
        derived = self._request_derived(targets, value, line)
        if derived is not None:
            out.append(derived)
        return out

    def _request_derived(self, targets, value, line) -> I.IRStmt | None:
        """`id := mux.Vars(r)["id"]`: the local is chosen by whoever sent the request."""
        if not self.request_params or "(" not in value:
            return None
        mentioned = [name for name in self.request_params
                     if re.search(rf"(?<![\w.]){re.escape(name)}\b", value)]
        if not mentioned:
            return None
        local = targets[0] if targets else ""
        if not re.fullmatch(r"[A-Za-z_]\w*", local) or local == "_":
            return None
        return I.IRStmt(
            I.ASSIGN, line, f"{local} = {mentioned[0]}",
            target=I.IRExpr("identifier", local, local, identifiers=(local,), line=line),
            operator="=", value=self.expr(mentioned[0], line),
            note=f"request-derived: {' '.join(value.split())[:120]}",
        )

    def _header_and_body(self, start: int, end: int, keyword: str):
        brace = _top_level_brace(self.scan, start + len(keyword), end)
        if brace < 0:
            return None
        close = min(matching_brace(self.scan, brace), end)
        header = self.raw[start + len(keyword):brace].strip()
        return header, brace, close

    def _if(self, start: int, end: int) -> list[I.IRStmt]:
        parsed = self._header_and_body(start, end, "if")
        if parsed is None:
            return [I.IRStmt(I.UNKNOWN, self.line(start), self.canon(self.raw[start:end]))]
        header, brace, close = parsed
        line = self.line(start)
        out: list[I.IRStmt] = []
        parts = _split_top(header, ";")
        condition = parts[-1].strip()
        for initializer in parts[:-1]:
            out.extend(self.statement_text(initializer.strip(), line))
        body = self.block(brace + 1, close - 1)
        orelse: tuple[I.IRStmt, ...] = ()
        tail = self.scan[close:end]
        else_match = re.match(r"\s*else\b", tail)
        if else_match:
            rest_start = close + else_match.end()
            rest = self.scan[rest_start:end].lstrip()
            if rest.startswith("if"):
                offset = rest_start + (len(self.scan[rest_start:end]) - len(rest))
                orelse = tuple(self._if(offset, end))
            else:
                bopen = self.scan.find("{", rest_start, end)
                if bopen >= 0:
                    bclose = min(matching_brace(self.scan, bopen), end)
                    orelse = self.block(bopen + 1, bclose - 1)
        out.append(I.IRStmt(I.IF, line, self.canon("if " + header),
                            condition=self.expr(condition, line), body=body, orelse=orelse,
                            reads=self.state_in(condition)))
        return out

    def statement_text(self, text: str, line: int) -> list[I.IRStmt]:
        """Lower a statement given as text (an `if` initializer)."""
        scanner = _BodyScanner(text, text, line, self.fields, self.package_vars,
                               self.imports, self.module, self.receiver, self.request_params,
                               self.params)
        return list(scanner.block(0, len(text)))

    def _for(self, start: int, end: int) -> list[I.IRStmt]:
        parsed = self._header_and_body(start, end, "for")
        if parsed is None:
            return [I.IRStmt(I.UNKNOWN, self.line(start), self.canon(self.raw[start:end]))]
        header, brace, close = parsed
        line = self.line(start)
        bound = re.search(r"<\s*(\d+)", header)
        return [I.IRStmt(I.LOOP, line, self.canon("for " + header),
                         condition=self.expr(header, line), body=self.block(brace + 1, close - 1),
                         reads=self.state_in(header), loop_bound=int(bound.group(1)) if bound else None,
                         note="range" if re.search(r"\brange\b", header) else "for")]

    def _switch(self, start: int, end: int, keyword: str) -> list[I.IRStmt]:
        parsed = self._header_and_body(start, end, keyword)
        if parsed is None:
            return [I.IRStmt(I.UNKNOWN, self.line(start), self.canon(self.raw[start:end]))]
        header, brace, close = parsed
        line = self.line(start)
        out: list[I.IRStmt] = []
        parts = _split_top(header, ";") if header else [""]
        subject = parts[-1].strip()
        for initializer in parts[:-1]:
            out.extend(self.statement_text(initializer.strip(), line))
        cases = self._cases(brace + 1, close - 1)
        if keyword == "select":
            # A `select` case is a message arriving, not a condition the code
            # decides: each arm is lowered as a block, never as a guard.
            for case_text, case_line, c_start, c_end in cases:
                body = self.block(c_start, c_end)
                binding = re.match(r"([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*:?=\s*<-", case_text)
                if binding:
                    body = (I.IRStmt(I.VAR_DECL, case_line, self.canon(case_text),
                                     target=self.expr(binding.group(1), case_line),
                                     operator="=", value=self.expr(case_text.split("<-", 1)[1], case_line),
                                     reads=self.state_in(case_text)),) + body
                out.append(I.IRStmt(I.BLOCK, case_line, self.canon(f"select case {case_text}"),
                                    body=body, reads=self.state_in(case_text), note="select-case"))
            return out
        chain: tuple[I.IRStmt, ...] = ()
        for case_text, case_line, c_start, c_end in reversed(cases):
            body = self.block(c_start, c_end)
            if case_text == "default":
                chain = body
                continue
            condition = case_text
            if subject and keyword == "switch":
                condition = " || ".join(f"{subject} == {v.strip()}" for v in _split_top(case_text, ","))
            chain = (I.IRStmt(I.IF, case_line, self.canon(f"case {case_text}"),
                              condition=self.expr(condition, case_line), body=body, orelse=chain,
                              reads=self.state_in(condition), note=keyword),)
        out.extend(chain)
        return out

    def _cases(self, start: int, end: int):
        """(case text, line, body start, body end) for each case clause."""
        found = []
        depth = 0
        index = start
        while index < end:
            char = self.scan[index]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif depth == 0:
                match = re.match(r"(case\b[^\n]*?|default)\s*:(?!=)", self.scan[index:end])
                if match and (index == start or self.scan[index - 1] in "\n\t "):
                    found.append([match.group(1), self.line(index), index + match.end(), end])
                    index += match.end()
                    continue
            index += 1
        for position in range(len(found) - 1):
            found[position][3] = found[position + 1][2] - len(found[position + 1][0]) - 1
            # Back up to the start of the following `case` keyword.
            back = self.scan.rfind("\n", found[position][2], found[position + 1][2])
            found[position][3] = back if back > 0 else found[position][3]
        return [(text[4:].strip() if text.startswith("case") else "default", line, s, e)
                for text, line, s, e in found]


def _imports(scan: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in IMPORT_LINE_RE.finditer(scan):
        alias, path = match.group(1), match.group(2)
        out[alias or path.rsplit("/", 1)[-1]] = path
    for match in IMPORT_BLOCK_RE.finditer(scan):
        close = scan.find(")", match.end())
        for spec in IMPORT_SPEC_RE.finditer(scan[match.end():close]):
            alias, path = spec.group(1), spec.group(2)
            out[alias or path.rsplit("/", 1)[-1]] = path
    return out


def _fields(text: str) -> list[tuple[str, str, bool]]:
    """(name, type, embedded) for each struct field line."""
    out: list[tuple[str, str, bool]] = []
    for line in text.splitlines():
        stripped = line.strip().rstrip(";").strip()
        if not stripped or stripped.startswith("//"):
            continue
        match = FIELD_RE.match(stripped)
        if match and not re.match(r"^[A-Za-z_]\w*(\.\w+)?$", stripped):
            names = [n.strip() for n in match.group(1).split(",")]
            type_text = match.group(2).strip()
            for name in names:
                out.append((name, type_text, False))
        else:
            embedded = stripped.lstrip("*")
            out.append((embedded.rsplit(".", 1)[-1], embedded, True))
    return out


def _result_text(scan: str, after_params: int, brace: int) -> str:
    return " ".join(scan[after_params:brace].split())


def parse_text(text: str, path: str) -> list[Contract]:
    raw = blank_raw_strings(text)
    scan = strip_comments(raw)
    path = str(path)
    package_match = PACKAGE_RE.search(scan)
    package_name = package_match.group(1) if package_match else Path(path).parent.name
    label = package_label_for_path(path, package_name)
    test_file = is_test_file(path)
    if test_file and not label.endswith("_test"):
        label = f"{label}_test"
    imports = _imports(scan)
    module = module_path_for(str(Path(path).parent))

    package = Contract(label, path, 1, kind="package", language=GO, parser=PARSER_NAME,
                       end_line=scan.count("\n") + 1, module=module or label, is_test=test_file)
    package.imports = sorted(imports.values())
    contracts: dict[str, Contract] = {}
    struct_fields: dict[str, set[str]] = {}

    for match in STRUCT_RE.finditer(scan):
        brace = scan.find("{", match.start())
        close = matching_brace(scan, brace)
        name = match.group(1)
        line = scan.count("\n", 0, match.start()) + 1
        fields = _fields(raw[brace + 1:close - 1])
        contract = Contract(name, path, line, kind="struct", language=GO, parser=PARSER_NAME,
                            end_line=scan.count("\n", 0, close) + 1, module=module or label,
                            is_test=test_file)
        for field_name, type_text, embedded in fields:
            if embedded:
                contract.bases.append(field_name)
            contract.state_vars.append(StateVar(
                name, field_name, type_text, "field", line, language=GO,
                key_types=_map_keys(type_text), value_type=_map_value(type_text),
            ))
        struct_fields[name] = {v.name for v in contract.state_vars}
        contracts[name] = contract

    for match in INTERFACE_RE.finditer(scan):
        brace = scan.find("{", match.start())
        close = matching_brace(scan, brace)
        name = match.group(1)
        line = scan.count("\n", 0, match.start()) + 1
        contract = Contract(name, path, line, kind="interface", language=GO, parser=PARSER_NAME,
                            end_line=scan.count("\n", 0, close) + 1, module=module or label,
                            is_test=test_file)
        for member in raw[brace + 1:close - 1].splitlines():
            member = member.strip()
            head = re.match(r"([A-Za-z_]\w*)\s*\((.*?)\)\s*(.*)$", member)
            if not head:
                continue
            function = Function(name, head.group(1), "public", "stateful", [], set(), set(),
                                [], line + 1 + raw[brace:close].count("\n", 0, raw[brace:close].find(member)), "")
            function.params = parse_params(head.group(2))
            function.returns = [Parameter("", head.group(3).strip("() "))] if head.group(3).strip() else []
            function.kind = "method"
            function.language = GO
            function.parser = PARSER_NAME
            function.path = path
            function.is_test = test_file
            contract.functions.append(function)
        contracts[name] = contract

    package_vars: set[str] = set()

    def package_variable(name: str, type_text: str, value: str, line: int, constant: bool) -> None:
        package_vars.add(name)
        type_text = declared_type(type_text, value)
        package.state_vars.append(StateVar(
            label, name, type_text, "package", line, constant=constant, language=GO,
            key_types=_map_keys(type_text), value_type=_map_value(type_text),
            initial_value=(value or "").strip()[:200],
        ))

    for match in VAR_LINE_RE.finditer(scan):
        package_variable(match.group(2), match.group(3) or "", match.group(4) or "",
                         scan.count("\n", 0, match.start()) + 1, match.group(1) == "const")
    for match in VAR_BLOCK_RE.finditer(scan):
        close = scan.find(")", match.end())
        offset = match.end()
        for spec in scan[match.end():close].split("\n"):
            stripped = spec.strip()
            head = re.match(r"([A-Za-z_]\w*)\s*([^=]*?)\s*(?:=\s*(.*))?$", stripped)
            if head and head.group(1) not in {"var", "const"}:
                package_variable(head.group(1), head.group(2) or "", head.group(3) or "",
                                 scan.count("\n", 0, offset) + 1, match.group(1) == "const")
            offset += len(spec) + 1

    for match in FUNC_RE.finditer(scan):
        receiver_name, receiver_type, name = match.group(1), match.group(2), match.group(3)
        popen = match.end() - 1
        pclose = _matching_paren(scan, popen)
        brace = _top_level_brace(scan, pclose, len(scan))
        if brace < 0:
            continue
        close = matching_brace(scan, brace)
        line = scan.count("\n", 0, match.start()) + 1
        params = parse_params(raw[popen + 1:pclose - 1])
        result_text = _result_text(scan, pclose, brace)
        shape = classify_shape(params, result_text)
        body_start = brace + 1
        body_end = close - 1
        request_facing = params
        if shape == "handler-factory":
            # `return func(w http.ResponseWriter, r *http.Request) {..}`: the
            # closure is the handler; its body is lowered as this function's.
            closure = CLOSURE_RE.search(scan[body_start:body_end].strip())
            if closure:
                offset = body_start + scan[body_start:body_end].find("return")
                closure_brace = _top_level_brace(scan, offset + 6, body_end)
                closure_params = parse_params(closure.group(1))
                if closure_brace >= 0 and classify_shape(closure_params, "") == "handler":
                    closure_close = matching_brace(scan, closure_brace)
                    params = params + closure_params
                    request_facing = closure_params
                    shape = "handler"
                    body_start, body_end = closure_brace + 1, closure_close - 1

        owner = contracts.get(receiver_type) if receiver_type else None
        if receiver_type and owner is None:
            owner = Contract(receiver_type, path, line, kind="struct", language=GO,
                             parser=PARSER_NAME, module=module or label, is_test=test_file)
            contracts[receiver_type] = owner
        target = owner or package
        fields = struct_fields.get(receiver_type, set()) if receiver_type else set()
        request_params = {p.name for p in params if " ".join(p.type_name.split()) in REQUEST_TYPES}
        param_types = {p.name: " ".join(p.type_name.split()) for p in params if p.name}

        # Statement lines come from the scan offset, which counts from the top
        # of the file, so a base of 1 makes them absolute.
        scanner = _BodyScanner(raw, scan, 1, fields, package_vars, imports, module,
                               receiver_name, request_params, param_types)
        statements = scanner.block(body_start, body_end)
        writes: set[str] = set()
        calls: list[str] = []
        external = False
        non_builtin = False
        for statement in statements:
            for sub in statement.walk():
                writes |= set(sub.writes)
                if sub.call is not None:
                    if sub.call.kind in I.EXTERNAL_CALL_KINDS:
                        external = True
                    if sub.call.kind != I.BUILTIN_CALL:
                        non_builtin = True
                    if sub.call.callee:
                        calls.append(sub.call.callee)
        body_text = raw[body_start:body_end]
        reads = set()
        for statement in statements:
            for sub in statement.walk():
                reads |= set(sub.reads)
        reads |= writes

        if shape == "handler":
            visibility, kind = "external", "handler"
        elif name in {"init", "main"} and not receiver_type:
            visibility, kind = "internal", name
        else:
            visibility = "public" if is_exported(name) else "internal"
            kind = "method" if receiver_type else "function"

        function = Function(
            target.name, name, visibility,
            "view" if not writes and not non_builtin else "stateful",
            [], reads, writes,
            (["<low-level-call>"] if external else []) + sorted(set(calls))[:32],
            line, body_text,
        )
        function.params = params
        function.returns = [Parameter("", result_text)] if result_text else []
        function.kind = kind
        function.language = GO
        function.parser = PARSER_NAME
        function.path = path
        function.end_line = scan.count("\n", 0, close) + 1
        function.is_test = test_file
        function.user_inputs = user_inputs_for(request_facing, shape, visibility)
        function.ir = I.IRFunctionBody(statements=statements, source=body_text,
                                       unsupported=tuple(scanner.unsupported))
        target.functions.append(function)

    for name, contract in list(contracts.items()):
        if contract.kind == "struct" and not contract.functions:
            package.types.append(ContractType(
                name, "struct", [f"{v.name}: {v.type_name}" for v in contract.state_vars],
                contract.line,
            ))
            del contracts[name]

    out = [package] if (package.functions or package.state_vars or package.types) else []
    out.extend(contracts.values())
    return out


def declared_type(type_text: str, initial_value: str) -> str:
    """`var x = map[string]bool{}` declares a map without spelling the type:
    the literal (or the `make`) it is initialised with carries it."""
    if (type_text or "").strip():
        return type_text.strip()
    match = re.match(r"^(?:make\(|new\()?\s*((?:map\[[^\]]+\]|\[\]|\[\d+\])\s*[\w.*\[\]]+)",
                     (initial_value or "").strip())
    return match.group(1).replace(" ", "") if match else ""


def _map_keys(type_text: str) -> list[str]:
    match = re.match(r"map\[(.+?)\]", type_text or "")
    return [match.group(1)] if match else []


def _map_value(type_text: str) -> str:
    match = re.match(r"map\[(?:[^\[\]]|\[[^\]]*\])+\](.+)$", type_text or "")
    return match.group(1).strip() if match else ""


def parse_file(path, catalog=None) -> list[Contract]:
    return parse_text(Path(path).read_text(encoding="utf-8", errors="ignore"), str(path))


def parse_sources(paths) -> list[Contract]:
    out: list[Contract] = []
    for path in paths:
        if Path(path).suffix.lower() == ".go":
            out.extend(parse_file(path))
    return out


def parse(paths) -> ParseResult:
    return ParseResult(parse_sources(paths), PARSER_NAME, GO)


def available() -> bool:
    return True


def status() -> str:
    return "regex fallback (reduced fidelity, see GO_REGEX_LIMITATIONS)"
