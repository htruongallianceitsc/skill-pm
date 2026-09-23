#!/usr/bin/env python3
"""Local-first project workspace CLI.

Standard-library only. Domain entities remain plain JSON. The sync command calls one
configurable Project Manager endpoint and keeps remote bookkeeping under .pm/.
"""
from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_ROOT = SKILL_ROOT / "assets" / "project-template"


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def canonical_json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def project_path(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Unsafe project-relative path: {relative}")
    root_resolved = root.resolve()
    candidate = (root / rel).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError(f"Path escapes project root: {relative}")
    return candidate


def require_project(root: Path) -> Dict[str, Any]:
    path = root / "project.json"
    if not path.exists():
        raise FileNotFoundError(f"Not a project workspace: {path} not found")
    return load_json(path)


def load_controls(root: Path) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    config = load_json(root / ".pm" / "config.json")
    registry = load_json(project_path(root, config["storage"]["entityRegistry"]))
    relation_map = load_json(project_path(root, config["storage"]["relationMap"]))
    sync_state = load_json(project_path(root, config["storage"]["syncState"]))
    return config, registry, relation_map, sync_state


def iter_entity_files(root: Path, registry: Dict[str, Any]) -> Iterable[Tuple[str, Path]]:
    for entity_type, spec in registry.get("types", {}).items():
        folder = project_path(root, spec["folder"])
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            yield entity_type, path


def content_file_entries(root: Path, entity: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for item in entity.get("content", []):
        if item.get("mode") != "file":
            continue
        rel = item.get("path")
        if not rel:
            continue
        path = project_path(root, rel)
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
            encoding = "utf-8"
            content_type = "text/plain; charset=utf-8"
        except UnicodeDecodeError:
            content = None
            encoding = "binary"
            content_type = "application/octet-stream"
        result.append({
            "path": rel,
            "hash": sha256_bytes(raw),
            "contentType": content_type,
            "encoding": encoding,
            "content": content,
        })
    return result


def payload_hash(root: Path, entity: Dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(canonical_json_bytes(entity))
    files = []
    for item in entity.get("content", []):
        if item.get("mode") == "file" and item.get("path"):
            files.append(item["path"])
    for rel in sorted(files):
        path = project_path(root, rel)
        h.update(b"\0FILE\0")
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
    return "sha256:" + h.hexdigest()


def entity_dest(root: Path, registry: Dict[str, Any], entity_type: str, uid_value: str) -> Path:
    spec = registry.get("types", {}).get(entity_type)
    if not spec:
        raise KeyError(f"Unknown entity type: {entity_type}")
    return project_path(root, spec["folder"]) / f"{uid_value}.json"


def collect_entities(root: Path, registry: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Path]]:
    entities: Dict[str, Dict[str, Any]] = {}
    paths: Dict[str, Path] = {}
    for expected_type, path in iter_entity_files(root, registry):
        entity = load_json(path)
        uid_value = str(entity.get("uid", ""))
        if uid_value and uid_value not in entities:
            entities[uid_value] = entity
            paths[uid_value] = path
        elif uid_value:
            # Keep first; validator reports duplicate.
            paths.setdefault(uid_value, path)
    return entities, paths


def build_manifest(root: Path) -> Dict[str, Any]:
    project = require_project(root)
    config, registry, _, _ = load_controls(root)
    entries = []
    by_uid: Dict[str, Any] = {}
    by_code: Dict[str, str] = {}
    by_id: Dict[str, str] = {}
    by_type: Dict[str, List[str]] = defaultdict(list)
    by_status: Dict[str, List[str]] = defaultdict(list)
    by_tag: Dict[str, List[str]] = defaultdict(list)
    outgoing: Dict[str, List[Any]] = defaultdict(list)
    incoming: Dict[str, List[Any]] = defaultdict(list)

    for expected_type, path in iter_entity_files(root, registry):
        entity = load_json(path)
        uid_value = str(entity.get("uid", ""))
        if not uid_value:
            continue
        rel_path = path.relative_to(root).as_posix()
        p_hash = payload_hash(root, entity)
        entry = {
            "uid": uid_value,
            "entityType": entity.get("entityType"),
            "path": rel_path,
            "id": entity.get("id"),
            "code": entity.get("code"),
            "localRef": entity.get("localRef"),
            "title": entity.get("title"),
            "status": entity.get("status"),
            "isDeleted": bool(entity.get("isDeleted", False)),
            "updatedAt": entity.get("updatedAt"),
            "payloadHash": p_hash,
        }
        entries.append(entry)
        by_uid[uid_value] = {k: v for k, v in entry.items() if k != "payloadHash"}
        if entity.get("code") is not None:
            by_code[str(entity["code"])] = uid_value
        if entity.get("id") is not None:
            by_id[str(entity["id"])] = uid_value
        by_type[str(entity.get("entityType") or expected_type)].append(uid_value)
        by_status[str(entity.get("status"))].append(uid_value)
        for tag in entity.get("tags", []):
            by_tag[str(tag)].append(uid_value)

        for rel in entity.get("relations", []):
            target = str(rel.get("targetUid", ""))
            if not target:
                continue
            edge = {
                "sourceUid": uid_value,
                "sourceType": entity.get("entityType"),
                "relation": rel.get("type"),
                "targetUid": target,
                "targetType": rel.get("targetType"),
                "metadata": rel.get("metadata", {}),
            }
            outgoing[uid_value].append(edge)
            incoming[target].append(edge)

    generated = now_utc()
    sorted_entries = sorted(entries, key=lambda x: (str(x.get("entityType")), str(x.get("uid"))))
    source_fingerprint = sha256_bytes(canonical_json_bytes([
        {"uid": item.get("uid"), "payloadHash": item.get("payloadHash")} for item in sorted(sorted_entries, key=lambda x: str(x.get("uid")))
    ]))
    manifest = {
        "schemaVersion": "1.1",
        "projectUid": project.get("uid"),
        "generatedAt": generated,
        "sourceFingerprint": source_fingerprint,
        "entities": sorted_entries,
    }
    save_json(project_path(root, config["storage"]["manifest"]), manifest)
    save_json(project_path(root, config["storage"]["entityIndex"]), {
        "schemaVersion": "1.1",
        "generatedAt": generated,
        "sourceFingerprint": source_fingerprint,
        "byUid": by_uid,
        "byId": by_id,
        "byCode": by_code,
        "byType": {k: sorted(v) for k, v in sorted(by_type.items())},
        "byStatus": {k: sorted(v) for k, v in sorted(by_status.items())},
        "byTag": {k: sorted(v) for k, v in sorted(by_tag.items())},
    })
    save_json(project_path(root, config["storage"]["relationIndex"]), {
        "schemaVersion": "1.1",
        "generatedAt": generated,
        "sourceFingerprint": source_fingerprint,
        "outgoing": dict(outgoing),
        "incoming": dict(incoming),
    })
    build_search_index(root, registry=registry, generated_at=generated, source_fingerprint=source_fingerprint)
    build_project_graph_html(root, registry=registry)
    return manifest



def load_local_engine(root: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if config is None:
        config, _, _, _ = load_controls(root)
    path = project_path(root, config["storage"].get("localEngine", ".pm/local-engine.json"))
    if not path.is_file():
        return {"indexing": {}, "query": {}, "doctor": {}}
    return load_json(path)


def flatten_scalar_fields(value: Any, prefix: str = "") -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            result.update(flatten_scalar_fields(child, child_prefix))
    elif isinstance(value, list):
        scalars = [x for x in value if not isinstance(x, (dict, list))]
        if scalars:
            result[prefix] = scalars
        for i, child in enumerate(value):
            if isinstance(child, (dict, list)):
                result.update(flatten_scalar_fields(child, f"{prefix}.{i}"))
    else:
        result[prefix] = value
    return result


def normalize_search_text(value: Any) -> str:
    return str(value if value is not None else "").casefold()


def tokenize_search_text(text: str, min_length: int = 2, stop_words: Optional[set] = None) -> List[str]:
    stop_words = stop_words or set()
    tokens = re.findall(r"[\w@./:#-]+", text.casefold(), flags=re.UNICODE)
    return [t for t in tokens if len(t) >= min_length and t not in stop_words]


def safe_entity_search_content(root: Path, entity: Dict[str, Any], settings: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    chunks: List[str] = []
    sources: List[Dict[str, Any]] = []
    max_file_chars = int(settings.get("maxFileChars", 200000))
    max_entity_chars = int(settings.get("maxEntityTextChars", 300000))
    if settings.get("includeInlineContent", True):
        for item in entity.get("content", []):
            if item.get("mode") == "inline" and isinstance(item.get("body"), str):
                body = item.get("body") or ""
                chunks.append(body)
                sources.append({"name": item.get("name"), "mode": "inline", "format": item.get("format"), "chars": len(body)})
    if settings.get("includeFileContent", True):
        for item in entity.get("content", []):
            if item.get("mode") != "file" or not item.get("path"):
                continue
            rel = str(item.get("path"))
            try:
                path = project_path(root, rel)
                if not path.is_file():
                    continue
                body = path.read_text(encoding="utf-8")
                body = body[:max_file_chars]
                chunks.append(body)
                sources.append({"name": item.get("name"), "mode": "file", "format": item.get("format"), "path": rel, "chars": len(body)})
            except (UnicodeDecodeError, OSError, ValueError):
                continue
    return "\n".join(chunks)[:max_entity_chars], sources


def build_search_index(root: Path, registry: Optional[Dict[str, Any]] = None, generated_at: Optional[str] = None, source_fingerprint: Optional[str] = None) -> Dict[str, Any]:
    config, loaded_registry, _, _ = load_controls(root)
    registry = registry or loaded_registry
    settings = (load_local_engine(root, config).get("indexing") or {})
    generated_at = generated_at or now_utc()
    entities, paths = collect_entities(root, registry)
    documents: Dict[str, Any] = {}
    inverted: Dict[str, set] = defaultdict(set)
    min_len = int(settings.get("tokenMinLength", 2))
    stop_words = {str(x).casefold() for x in (settings.get("stopWords") or [])}
    fp_items = []
    for uid_value, entity in sorted(entities.items()):
        try:
            p_hash = payload_hash(root, entity)
        except Exception:
            p_hash = sha256_bytes(canonical_json_bytes(entity))
        fp_items.append({"uid": uid_value, "payloadHash": p_hash})
        fields: Dict[str, Any] = {
            "uid": uid_value,
            "entityType": entity.get("entityType"),
            "id": entity.get("id"),
            "code": entity.get("code"),
            "localRef": entity.get("localRef"),
            "title": entity.get("title"),
            "status": entity.get("status"),
            "tags": entity.get("tags", []),
            "isDeleted": bool(entity.get("isDeleted")),
            "updatedAt": entity.get("updatedAt"),
        }
        if settings.get("includeData", True):
            fields.update({f"data.{k}": v for k, v in flatten_scalar_fields(entity.get("data", {})).items()})
        structured_text = json.dumps(fields, ensure_ascii=False, sort_keys=True)
        content_text, content_sources = safe_entity_search_content(root, entity, settings)
        search_text = (structured_text + "\n" + content_text).casefold()
        tokens = tokenize_search_text(search_text, min_len, stop_words)
        unique_tokens = sorted(set(tokens))
        for token in unique_tokens:
            inverted[token].add(uid_value)
        documents[uid_value] = {
            "uid": uid_value,
            "entityType": entity.get("entityType"),
            "id": entity.get("id"),
            "code": entity.get("code"),
            "localRef": entity.get("localRef"),
            "title": entity.get("title"),
            "status": entity.get("status"),
            "tags": entity.get("tags", []),
            "isDeleted": bool(entity.get("isDeleted")),
            "updatedAt": entity.get("updatedAt"),
            "path": paths[uid_value].relative_to(root).as_posix(),
            "payloadHash": p_hash,
            "fields": fields,
            "searchText": search_text,
            "tokens": unique_tokens,
            "contentSources": content_sources,
        }
    source_fingerprint = source_fingerprint or sha256_bytes(canonical_json_bytes(fp_items))
    doc = {
        "schemaVersion": "1.0",
        "generatedAt": generated_at,
        "sourceFingerprint": source_fingerprint,
        "documentCount": len(documents),
        "documents": documents,
        "inverted": {k: sorted(v) for k, v in sorted(inverted.items())},
    }
    path = project_path(root, config["storage"].get("searchIndex", ".pm/indexes/search-index.json"))
    save_json(path, doc)
    return doc


def workspace_fingerprint_safe(root: Path, registry: Optional[Dict[str, Any]] = None) -> str:
    if registry is None:
        _, registry, _, _ = load_controls(root)
    items = []
    for expected_type, path in iter_entity_files(root, registry):
        try:
            entity = load_json(path)
            try:
                digest = payload_hash(root, entity)
            except Exception:
                digest = sha256_bytes(canonical_json_bytes(entity))
            items.append({"uid": str(entity.get("uid") or path.name), "payloadHash": digest})
        except Exception:
            items.append({"uid": f"invalid:{expected_type}:{path.name}", "payloadHash": sha256_bytes(path.read_bytes())})
    return sha256_bytes(canonical_json_bytes(sorted(items, key=lambda x: x["uid"])))


def ensure_search_index(root: Path, rebuild_if_stale: bool = True) -> Dict[str, Any]:
    config, registry, _, _ = load_controls(root)
    path = project_path(root, config["storage"].get("searchIndex", ".pm/indexes/search-index.json"))
    current = workspace_fingerprint_safe(root, registry)
    if path.is_file():
        try:
            index = load_json(path)
            if index.get("sourceFingerprint") == current:
                return index
        except Exception:
            pass
    if not rebuild_if_stale:
        raise ValueError("Local search index is missing or stale; run reindex")
    build_manifest(root)
    return load_json(path)


def search_local(root: Path, text_query: str, entity_types: Optional[List[str]] = None, statuses: Optional[List[str]] = None, tags: Optional[List[str]] = None, include_deleted: bool = False, limit: int = 50, rebuild_if_stale: bool = True) -> Dict[str, Any]:
    config, _, _, _ = load_controls(root)
    settings = (load_local_engine(root, config).get("indexing") or {})
    index = ensure_search_index(root, rebuild_if_stale=rebuild_if_stale)
    min_len = int(settings.get("tokenMinLength", 2))
    stop_words = {str(x).casefold() for x in (settings.get("stopWords") or [])}
    normalized = normalize_search_text(text_query).strip()
    tokens = tokenize_search_text(normalized, min_len, stop_words)
    docs = index.get("documents", {})
    if tokens:
        sets = [set(index.get("inverted", {}).get(token, [])) for token in tokens]
        candidates = set.intersection(*sets) if sets else set()
    else:
        candidates = set(docs)
    types = set(entity_types or [])
    status_set = set(statuses or [])
    tag_set = set(tags or [])
    rows = []
    for uid_value in candidates:
        doc = docs.get(uid_value) or {}
        if doc.get("isDeleted") and not include_deleted:
            continue
        if types and doc.get("entityType") not in types:
            continue
        if status_set and doc.get("status") not in status_set:
            continue
        if tag_set and not tag_set.issubset(set(doc.get("tags") or [])):
            continue
        search_text = str(doc.get("searchText") or "")
        if normalized and normalized not in search_text and not all(t in set(doc.get("tokens") or []) for t in tokens):
            continue
        title = normalize_search_text(doc.get("title"))
        code = normalize_search_text(doc.get("code"))
        tags_text = " ".join(normalize_search_text(x) for x in (doc.get("tags") or []))
        score = 0
        for token in tokens:
            if token in code:
                score += 8
            if token in title:
                score += 5
            if token in tags_text:
                score += 3
            if token in search_text:
                score += 1
        pos = search_text.find(normalized) if normalized else -1
        snippet = ""
        if pos >= 0:
            start = max(0, pos - 100)
            snippet = search_text[start:pos + len(normalized) + 180].replace("\n", " ")
        rows.append({
            "uid": uid_value, "entityType": doc.get("entityType"), "id": doc.get("id"), "code": doc.get("code"),
            "localRef": doc.get("localRef"), "title": doc.get("title"), "status": doc.get("status"), "tags": doc.get("tags", []),
            "updatedAt": doc.get("updatedAt"), "path": doc.get("path"), "score": score, "snippet": snippet,
        })
    rows.sort(key=lambda x: (-int(x.get("score") or 0), str(x.get("entityType")), str(x.get("code") or x.get("localRef"))))
    return {"schemaVersion": "1.0", "query": text_query, "count": min(len(rows), limit), "totalMatched": len(rows), "results": rows[:limit], "indexGeneratedAt": index.get("generatedAt")}


QUERY_TOKEN_RE = re.compile(r"""\s*(>=|<=|!=|~=|\^=|\$=|=|>|<|\(|\)|\bAND\b|\bOR\b|\bNOT\b|\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s()=<>!~^$]+)""", re.IGNORECASE)


def query_tokens(expression: str) -> List[str]:
    tokens = [m.group(1) for m in QUERY_TOKEN_RE.finditer(expression or "")]
    consumed = "".join(m.group(0) for m in QUERY_TOKEN_RE.finditer(expression or "")).strip()
    if expression.strip() and not tokens:
        raise ValueError("Query expression could not be parsed")
    return tokens


def parse_query_value(token: str) -> Any:
    if len(token) >= 2 and token[0] == token[-1] and token[0] == '"':
        token = json.loads(token)
    elif len(token) >= 2 and token[0] == token[-1] and token[0] == "'":
        token = token[1:-1].replace("\\'", "'").replace("\\\\", "\\")
    low = token.casefold()
    if low == "null":
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(token)
    except ValueError:
        try:
            return float(token)
        except ValueError:
            return token


def entity_field_value(entity: Dict[str, Any], field: str) -> Any:
    aliases = {"type": "entityType", "tag": "tags"}
    field = aliases.get(field, field)
    if field.startswith("data."):
        return get_dotted(entity.get("data", {}), field[5:])
    if field.startswith("entity."):
        return get_dotted(entity, field[7:])
    return get_dotted(entity, field) if "." in field else entity.get(field)


def compare_query_value(actual: Any, op: str, expected: Any) -> bool:
    if isinstance(actual, list):
        if op == "=":
            return expected in actual
        if op == "!=":
            return expected not in actual
        if op == "~=":
            return any(str(expected).casefold() in str(x).casefold() for x in actual)
    if op in {"=", "!="}:
        equal = actual == expected or (actual is not None and expected is not None and str(actual).casefold() == str(expected).casefold())
        return equal if op == "=" else not equal
    if actual is None:
        return False
    a = actual
    b = expected
    if op in {">", ">=", "<", "<="}:
        try:
            af = float(a); bf = float(b)
            return {">": af > bf, ">=": af >= bf, "<": af < bf, "<=": af <= bf}[op]
        except (TypeError, ValueError):
            sa, sb = str(a), str(b)
            return {">": sa > sb, ">=": sa >= sb, "<": sa < sb, "<=": sa <= sb}[op]
    sa, sb = str(a).casefold(), str(b).casefold()
    if op == "~=":
        return sb in sa
    if op == "^=":
        return sa.startswith(sb)
    if op == "$=":
        return sa.endswith(sb)
    raise ValueError(f"Unsupported query operator: {op}")


class QueryExpressionParser:
    def __init__(self, tokens: List[str]):
        self.tokens = tokens
        self.pos = 0
    def peek(self) -> Optional[str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None
    def take(self) -> str:
        token = self.peek()
        if token is None:
            raise ValueError("Unexpected end of query expression")
        self.pos += 1
        return token
    def parse(self):
        if not self.tokens:
            return ("const", True)
        node = self.parse_or()
        if self.peek() is not None:
            raise ValueError(f"Unexpected query token: {self.peek()}")
        return node
    def parse_or(self):
        node = self.parse_and()
        while self.peek() and self.peek().upper() == "OR":
            self.take(); node = ("or", node, self.parse_and())
        return node
    def parse_and(self):
        node = self.parse_factor()
        while self.peek() and self.peek().upper() == "AND":
            self.take(); node = ("and", node, self.parse_factor())
        return node
    def parse_factor(self):
        tok = self.peek()
        if tok and tok.upper() == "NOT":
            self.take(); return ("not", self.parse_factor())
        if tok == "(":
            self.take(); node = self.parse_or()
            if self.take() != ")":
                raise ValueError("Expected closing parenthesis")
            return node
        return self.parse_comparison()
    def parse_comparison(self):
        field = self.take()
        op = self.take()
        if op not in {"=", "!=", "~=", "^=", "$=", ">", ">=", "<", "<="}:
            raise ValueError(f"Expected comparison operator after {field!r}, got {op!r}")
        value = parse_query_value(self.take())
        return ("cmp", field, op, value)


def eval_query_ast(node: Any, entity: Dict[str, Any]) -> bool:
    kind = node[0]
    if kind == "const": return bool(node[1])
    if kind == "and": return eval_query_ast(node[1], entity) and eval_query_ast(node[2], entity)
    if kind == "or": return eval_query_ast(node[1], entity) or eval_query_ast(node[2], entity)
    if kind == "not": return not eval_query_ast(node[1], entity)
    if kind == "cmp": return compare_query_value(entity_field_value(entity, node[1]), node[2], node[3])
    raise ValueError(f"Unknown query AST node: {kind}")


def parse_query_expression(expression: Optional[str]):
    return QueryExpressionParser(query_tokens(expression or "")).parse()


def graph_candidate_uids(root: Path, ref: str, direction: str = "both", relations: Optional[List[str]] = None, max_depth: int = 1) -> set:
    _, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    root_uid, _ = resolve_entity_ref(root, ref, entities, registry)
    outgoing, incoming = graph_edges(entities)
    relation_filter = set(relations or [])
    seen = {root_uid}
    frontier = {root_uid}
    for _ in range(max(0, int(max_depth))):
        nxt = set()
        for uid_value in frontier:
            if direction in {"outgoing", "both"}:
                for edge in outgoing.get(uid_value, []):
                    if relation_filter and edge.get("relation") not in relation_filter: continue
                    if edge.get("targetUid") in entities: nxt.add(str(edge.get("targetUid")))
            if direction in {"incoming", "both"}:
                for edge in incoming.get(uid_value, []):
                    if relation_filter and edge.get("relation") not in relation_filter: continue
                    if edge.get("sourceUid") in entities: nxt.add(str(edge.get("sourceUid")))
        nxt -= seen
        seen |= nxt
        frontier = nxt
        if not frontier: break
    seen.discard(root_uid)
    return seen


def query_entities(root: Path, expression: Optional[str] = None, include_deleted: bool = False, related_to: Optional[str] = None, direction: str = "both", relations: Optional[List[str]] = None, max_depth: int = 1, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    config, registry, _, _ = load_controls(root)
    entities, paths = collect_entities(root, registry)
    ast = parse_query_expression(expression)
    candidates = set(entities)
    if related_to:
        candidates &= graph_candidate_uids(root, related_to, direction=direction, relations=relations, max_depth=max_depth)
    rows = []
    for uid_value in candidates:
        entity = entities[uid_value]
        if entity.get("isDeleted") and not include_deleted:
            continue
        if not eval_query_ast(ast, entity):
            continue
        row = copy.deepcopy(entity)
        row["__path"] = paths[uid_value].relative_to(root).as_posix()
        rows.append(row)
    rows.sort(key=lambda x: (str(x.get("entityType")), str(x.get("code") or x.get("localRef"))))
    max_limit = int((load_local_engine(root, config).get("query") or {}).get("maxLimit", 1000))
    effective = min(int(limit or (load_local_engine(root, config).get("query") or {}).get("defaultLimit", 100)), max_limit)
    return rows[:effective]


def view_value(entity: Dict[str, Any], field: str) -> Any:
    if field == "path": return entity.get("__path")
    return entity_field_value(entity, field)


def load_views(root: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if config is None:
        config, _, _, _ = load_controls(root)
    path = project_path(root, config["storage"].get("views", ".pm/views.json"))
    return load_json(path) if path.is_file() else {"schemaVersion": "1.0", "views": {}}


def sort_view_rows(rows: List[Dict[str, Any]], specs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = list(rows)
    for spec in reversed(specs or []):
        field = str(spec.get("field") or "title")
        reverse = str(spec.get("direction") or "asc").casefold() == "desc"
        ordered_values = [str(x).casefold() for x in (spec.get("values") or [])]
        def sort_key(entity: Dict[str, Any]):
            value = view_value(entity, field)
            normalized = str(value or "").casefold()
            if ordered_values:
                try:
                    rank = ordered_values.index(normalized)
                except ValueError:
                    rank = len(ordered_values)
                return (value is None, rank, normalized)
            return (value is None, normalized)
        # Explicit values define their own order; direction only applies to natural sorts.
        result.sort(key=sort_key, reverse=(reverse and not ordered_values))
    return result


def run_saved_view(root: Path, view_id: str, limit: Optional[int] = None) -> Dict[str, Any]:
    config, _, _, _ = load_controls(root)
    doc = load_views(root, config)
    view = (doc.get("views") or {}).get(view_id)
    if not view:
        raise KeyError(f"Unknown saved view: {view_id}")
    rows = query_entities(root, expression=view.get("expression"), include_deleted=bool(view.get("includeDeleted", False)), related_to=view.get("relatedTo"), direction=view.get("direction", "both"), relations=view.get("relations"), max_depth=int(view.get("maxDepth", 1)), limit=limit or view.get("limit"))
    rows = sort_view_rows(rows, view.get("sort") or [])
    columns = view.get("columns") or ["code", "title", "status"]
    projected = [{field: view_value(row, field) for field in columns} | {"uid": row.get("uid")} for row in rows]
    group_by = view.get("groupBy")
    groups = None
    if group_by:
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in projected:
            grouped[str(row.get(group_by))].append(row)
        groups = dict(grouped)
    return {"schemaVersion": "1.0", "viewId": view_id, "title": view.get("title", view_id), "expression": view.get("expression"), "columns": columns, "groupBy": group_by, "count": len(projected), "rows": projected, "groups": groups}


def doctor_report(root: Path) -> Dict[str, Any]:
    config, registry, relation_map, _ = load_controls(root)
    findings: List[Dict[str, Any]] = []
    def add(kind: str, severity: str, message: str, fixable: bool = False, fix_level: Optional[str] = None, **extra: Any) -> None:
        findings.append({"kind": kind, "severity": severity, "message": message, "fixable": fixable, "fixLevel": fix_level, **extra})
    for err in validate_project(root):
        add("validation", "error", err, False)
    broken = broken_links_report(root)
    for item in broken.get("findings", []):
        fixable = item.get("kind") == "target-type-mismatch"
        add("broken-link", "error", item.get("message") or item.get("kind"), fixable, "semantic" if fixable else None, detail=item)
    current_fp = workspace_fingerprint_safe(root, registry)
    derived = [
        ("manifest", config["storage"]["manifest"]),
        ("entity-index", config["storage"]["entityIndex"]),
        ("relation-index", config["storage"]["relationIndex"]),
        ("search-index", config["storage"].get("searchIndex", ".pm/indexes/search-index.json")),
    ]
    for kind, rel in derived:
        path = project_path(root, rel)
        if not path.is_file():
            add("derived-missing", "warning", f"Derived file missing: {rel}", True, "safe", artifact=kind, path=rel)
            continue
        try:
            doc = load_json(path)
            if doc.get("sourceFingerprint") != current_fp:
                add("derived-stale", "warning", f"Derived file is stale: {rel}", True, "safe", artifact=kind, path=rel)
        except Exception as exc:
            add("derived-invalid", "warning", f"Derived file is invalid JSON: {rel}: {exc}", True, "safe", artifact=kind, path=rel)
    source_path = source_index_path(root, config)
    if not source_path.is_file():
        add("source-index-missing", "info", f"Source index not built yet: {source_path.relative_to(root).as_posix()}", True, "safe", artifact="source-index", path=source_path.relative_to(root).as_posix())
    else:
        try:
            source_doc = load_json(source_path)
            current_source_fp = source_fingerprint(root, source_doc.get("roots") or None)
            if source_doc.get("sourceFingerprint") != current_source_fp:
                add("source-index-stale", "warning", f"Source index is stale: {source_path.relative_to(root).as_posix()}", True, "safe", artifact="source-index", path=source_path.relative_to(root).as_posix())
        except Exception as exc:
            add("source-index-invalid", "warning", f"Source index is invalid JSON: {source_path.relative_to(root).as_posix()}: {exc}", True, "safe", artifact="source-index", path=source_path.relative_to(root).as_posix())
    dep_path = dependency_index_path(root, config)
    if not dep_path.is_file():
        add("dependency-index-missing", "info", f"Dependency index not built yet: {dep_path.relative_to(root).as_posix()}", True, "safe", artifact="dependency-index", path=dep_path.relative_to(root).as_posix())
    else:
        try:
            dep_doc = load_json(dep_path)
            source_doc = ensure_source_index(root, rebuild_if_stale=False)
            expected_dep_fp = sha256_bytes(canonical_json_bytes({"sourceFingerprint": source_doc.get("sourceFingerprint"), "dependency": _source_cfg_dependency(root, config)}))
            if dep_doc.get("dependencyFingerprint") != expected_dep_fp:
                add("dependency-index-stale", "warning", f"Dependency index is stale: {dep_path.relative_to(root).as_posix()}", True, "safe", artifact="dependency-index", path=dep_path.relative_to(root).as_posix())
        except Exception as exc:
            add("dependency-index-invalid", "warning", f"Dependency index is invalid/stale: {dep_path.relative_to(root).as_posix()}: {exc}", True, "safe", artifact="dependency-index", path=dep_path.relative_to(root).as_posix())
    views = load_views(root, config)
    for view_id, view in (views.get("views") or {}).items():
        try:
            parse_query_expression(view.get("expression") or "")
        except Exception as exc:
            add("view-invalid-query", "error", f"View {view_id}: {exc}", False, viewId=view_id)
    # Deterministic semantic repair candidates: targetType mismatch when actual mapping is allowed.
    entities, _ = collect_entities(root, registry)
    for source_uid, entity in entities.items():
        source_type = str(entity.get("entityType") or "")
        for rel in entity.get("relations", []):
            target = entities.get(str(rel.get("targetUid") or ""))
            if not target: continue
            actual = str(target.get("entityType") or "")
            declared = str(rel.get("targetType") or "")
            if declared != actual and any(matches_mapping(source_type, str(rel.get("type") or ""), actual, m) for m in relation_map.get("mappings", [])):
                add("relation-target-type-repair", "warning", f"Relation targetType can be repaired for {source_uid}: {declared!r} -> {actual!r}", True, "semantic", sourceUid=source_uid, targetUid=target.get("uid"), relation=rel.get("type"), actualTargetType=actual)
    summary: Dict[str, int] = defaultdict(int)
    for item in findings: summary[str(item.get("severity"))] += 1
    return {"schemaVersion": "1.0", "generatedAt": now_utc(), "workspaceFingerprint": current_fp, "summary": dict(summary), "findingCount": len(findings), "findings": findings}


def apply_doctor_semantic_fixes(root: Path) -> int:
    _, registry, relation_map, _ = load_controls(root)
    entities, paths = collect_entities(root, registry)
    changed = 0
    for source_uid, entity in entities.items():
        source_type = str(entity.get("entityType") or "")
        dirty = False
        for rel in entity.get("relations", []):
            target = entities.get(str(rel.get("targetUid") or ""))
            if not target: continue
            actual = str(target.get("entityType") or "")
            declared = str(rel.get("targetType") or "")
            if declared != actual and any(matches_mapping(source_type, str(rel.get("type") or ""), actual, m) for m in relation_map.get("mappings", [])):
                rel["targetType"] = actual
                dirty = True
        if dirty:
            entity["revision"] = int(entity.get("revision", 0)) + 1
            entity["updatedAt"] = now_utc()
            save_json(paths[source_uid], entity)
            changed += 1
    return changed


def matches_mapping(source_type: str, relation_type: str, target_type: str, mapping: Dict[str, Any]) -> bool:
    from_types = mapping.get("from", [])
    to_types = mapping.get("to", [])
    return (
        ("*" in from_types or source_type in from_types)
        and mapping.get("relation") == relation_type
        and ("*" in to_types or target_type in to_types)
    )


def validate_project(root: Path) -> List[str]:
    project = require_project(root)
    config, registry, relation_map, sync_state = load_controls(root)
    errors: List[str] = []
    seen_uid: Dict[str, Path] = {}
    seen_id: Dict[str, str] = {}
    seen_code: Dict[str, str] = {}
    records: List[Tuple[str, Dict[str, Any], Path]] = []

    required = ["schemaVersion", "entityType", "uid", "id", "code", "localRef", "title", "status", "isDeleted", "tags", "relations", "data", "content", "revision", "createdAt", "updatedAt"]

    for expected_type, path in iter_entity_files(root, registry):
        try:
            entity = load_json(path)
        except Exception as exc:
            errors.append(f"{path}: invalid JSON: {exc}")
            continue
        records.append((expected_type, entity, path))
        missing = [k for k in required if k not in entity]
        if missing:
            errors.append(f"{path}: missing fields: {', '.join(missing)}")
        uid_value = str(entity.get("uid", ""))
        if not uid_value:
            errors.append(f"{path}: uid is required")
            continue
        if uid_value in seen_uid:
            errors.append(f"Duplicate uid {uid_value}: {seen_uid[uid_value]} and {path}")
        else:
            seen_uid[uid_value] = path
        if entity.get("entityType") != expected_type:
            errors.append(f"{path}: entityType {entity.get('entityType')!r} does not match registry folder type {expected_type!r}")
        if not str(entity.get("localRef", "")).startswith(f"local:{expected_type}:"):
            errors.append(f"{path}: localRef must start with local:{expected_type}:")
        if not isinstance(entity.get("revision"), int) or entity.get("revision", 0) < 1:
            errors.append(f"{path}: revision must be an integer >= 1")

        for definition_error in validate_entity_definition(root, registry, expected_type, entity):
            errors.append(f"{path}: {definition_error}")

        id_value = entity.get("id")
        if id_value is not None:
            key = str(id_value)
            if key in seen_id and seen_id[key] != uid_value:
                errors.append(f"Duplicate id {key}: {seen_id[key]} and {uid_value}")
            seen_id[key] = uid_value
        code_value = entity.get("code")
        if code_value is not None:
            key = str(code_value)
            if key in seen_code and seen_code[key] != uid_value:
                errors.append(f"Duplicate code {key}: {seen_code[key]} and {uid_value}")
            seen_code[key] = uid_value

        locked = sync_state.get("entities", {}).get(uid_value, {}).get("serverIdentity")
        if locked:
            for field in config.get("identity", {}).get("immutableAfterServerAssign", ["id", "code"]):
                if entity.get(field) != locked.get(field):
                    errors.append(f"{path}: locked server field {field} changed from {locked.get(field)!r} to {entity.get(field)!r}")

        for item in entity.get("content", []):
            if item.get("mode") == "file":
                rel = item.get("path")
                if not rel:
                    errors.append(f"{path}: file content item {item.get('name')!r} has no path")
                    continue
                try:
                    content_path = project_path(root, rel)
                    if not content_path.is_file():
                        errors.append(f"{path}: referenced content file does not exist: {rel}")
                except Exception as exc:
                    errors.append(f"{path}: invalid content path {rel!r}: {exc}")

    entities, _ = collect_entities(root, registry)
    mappings = relation_map.get("mappings", [])
    incoming_by_mapping_target: Dict[Tuple[str, str], int] = defaultdict(int)

    for expected_type, entity, path in records:
        source_type = str(entity.get("entityType", expected_type))
        relation_matches: Dict[str, int] = defaultdict(int)
        for rel in entity.get("relations", []):
            relation_type = str(rel.get("type", ""))
            target_uid = str(rel.get("targetUid", ""))
            target_type = str(rel.get("targetType", ""))
            matching = [m for m in mappings if matches_mapping(source_type, relation_type, target_type, m)]
            if not matching:
                errors.append(f"{path}: relation {relation_type} from {source_type} to {target_type} is not allowed by relation-map.json")
                continue
            target = entities.get(target_uid)
            allow_unresolved = any(bool(m.get("allowUnresolved")) for m in matching)
            if target is None and not allow_unresolved:
                errors.append(f"{path}: relation target not found: {target_uid}")
            elif target is not None and target.get("entityType") != target_type:
                errors.append(f"{path}: targetType {target_type!r} does not match target entity type {target.get('entityType')!r} for {target_uid}")
            for m in matching:
                key = str(m.get("key"))
                relation_matches[key] += 1
                incoming_by_mapping_target[(key, target_uid)] += 1

        for m in mappings:
            from_types = m.get("from", [])
            if "*" not in from_types and source_type not in from_types:
                continue
            key = str(m.get("key"))
            count = relation_matches.get(key, 0)
            card = m.get("cardinality", "many-to-many")
            if m.get("required") and count == 0:
                errors.append(f"{path}: required relationship mapping {key} is missing")
            if card in {"many-to-one", "one-to-one"} and count > 1:
                errors.append(f"{path}: mapping {key} allows at most one target from the source side")

    for m in mappings:
        if m.get("cardinality") not in {"one-to-one", "one-to-many"}:
            continue
        key = str(m.get("key"))
        for (mapping_key, target_uid), count in incoming_by_mapping_target.items():
            if mapping_key == key and count > 1:
                errors.append(f"Target {target_uid}: mapping {key} allows at most one incoming source")

    project_locked = sync_state.get("projectServerIdentity")
    if project_locked:
        for field in config.get("identity", {}).get("immutableAfterServerAssign", ["id", "code"]):
            if project.get(field) != project_locked.get(field):
                errors.append(f"project.json: locked server field {field} changed from {project_locked.get(field)!r} to {project.get(field)!r}")

    # Governance planning records are separate from domain entities but are still part of a valid workspace.
    try:
        errors.extend(validate_workplans(root))
    except NameError:
        # validate_workplans is defined later in this single-file CLI; this only matters during unusual partial imports.
        pass

    return errors


def deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def get_dotted(data: Dict[str, Any], dotted: str) -> Any:
    value: Any = data
    for part in dotted.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def schema_type_matches(value: Any, expected: Any) -> bool:
    expected_types = expected if isinstance(expected, list) else [expected]
    for name in expected_types:
        if name == "null" and value is None:
            return True
        if name == "object" and isinstance(value, dict):
            return True
        if name == "array" and isinstance(value, list):
            return True
        if name == "string" and isinstance(value, str):
            return True
        if name == "boolean" and isinstance(value, bool):
            return True
        if name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
    return False


def validate_json_schema(value: Any, schema: Dict[str, Any], path: str = "data") -> List[str]:
    """Validate the small JSON-Schema subset used by bundled entity data schemas.

    The schemas remain standard JSON Schema so a web client can use a full validator.
    This built-in validator intentionally covers only the keywords used by the template,
    keeping the CLI dependency-free.
    """
    errors: List[str] = []
    expected = schema.get("type")
    if expected is not None and not schema_type_matches(value, expected):
        errors.append(f"{path}: expected type {expected!r}, got {type(value).__name__}")
        return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value {value!r} is not in allowed enum {schema['enum']!r}")
        return errors
    if value is None:
        return errors
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required field is missing")
        props = schema.get("properties", {})
        for key, child in value.items():
            if key in props:
                errors.extend(validate_json_schema(child, props[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: additional property is not allowed")
    elif isinstance(value, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < min_items:
            errors.append(f"{path}: requires at least {min_items} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, child in enumerate(value):
                errors.extend(validate_json_schema(child, item_schema, f"{path}[{idx}]"))
    elif isinstance(value, str):
        min_length = schema.get("minLength")
        if min_length is not None and len(value) < min_length:
            errors.append(f"{path}: requires at least {min_length} character(s)")
        pattern = schema.get("pattern")
        if pattern and re.search(pattern, value) is None:
            errors.append(f"{path}: does not match pattern {pattern!r}")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            errors.append(f"{path}: must be >= {minimum}")
        if maximum is not None and value > maximum:
            errors.append(f"{path}: must be <= {maximum}")
    return errors


def validate_entity_definition(root: Path, registry: Dict[str, Any], entity_type: str, entity: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    spec = registry.get("types", {}).get(entity_type) or {}
    lifecycle = spec.get("lifecycle") or {}
    statuses = lifecycle.get("statuses") or {}
    if statuses and entity.get("status") not in statuses:
        errors.append(f"status {entity.get('status')!r} is not defined for entity type {entity_type!r}")
    schema_path = spec.get("dataSchema")
    if schema_path:
        path = project_path(root, schema_path)
        if not path.is_file():
            errors.append(f"data schema not found: {schema_path}")
        else:
            try:
                schema = load_json(path)
                errors.extend(validate_json_schema(entity.get("data", {}), schema, "data"))
            except Exception as exc:
                errors.append(f"cannot load data schema {schema_path}: {exc}")
    return errors


def workspace_definition(root: Path, config: Dict[str, Any], registry: Dict[str, Any]) -> Dict[str, Any]:
    paths = [
        config["storage"]["entityRegistry"],
        config["storage"]["relationMap"],
        config["storage"].get("lookups", ".pm/lookups.json"),
        config["storage"].get("qualityRules", ".pm/quality-rules.json"),
        config["storage"].get("intelligence", ".pm/intelligence.json"),
        config["storage"].get("changeManagement", ".pm/change-management.json"),
        config["storage"].get("agentWorkflow", ".pm/agent-workflow.json"),
        config["storage"].get("localEngine", ".pm/local-engine.json"),
        config["storage"].get("views", ".pm/views.json"),
        config["storage"].get("sourceIntelligence", ".pm/source-intelligence.json"),
        config["storage"].get("documentationPolicy", ".pm/documentation-policy.json"),
        config["storage"].get("workPlanSchema", "schemas/core/workplan.schema.json"),
        "schemas/core/entity.schema.json",
    ]
    for spec in registry.get("types", {}).values():
        if spec.get("dataSchema"):
            paths.append(spec["dataSchema"])
    files = []
    for rel in sorted(set(paths)):
        path = project_path(root, rel)
        if path.is_file():
            files.append({"path": rel, "json": load_json(path)})
    digest = sha256_bytes(canonical_json_bytes(files))
    return {"payloadHash": digest, "files": files}


def allowed_definition_path(relative: str) -> bool:
    if relative in {".pm/entity-types.json", ".pm/relation-map.json", ".pm/lookups.json", ".pm/quality-rules.json", ".pm/intelligence.json", ".pm/change-management.json", ".pm/agent-workflow.json", ".pm/local-engine.json", ".pm/views.json", ".pm/source-intelligence.json", ".pm/documentation-policy.json"}:
        return True
    return relative in {"schemas/core/entity.schema.json", "schemas/core/workplan.schema.json"} or relative.startswith("schemas/entities/")


def apply_workspace_definition(root: Path, payload: Dict[str, Any]) -> None:
    for item in payload.get("files", []):
        rel = str(item.get("path", ""))
        if not allowed_definition_path(rel):
            raise ValueError(f"Remote workspace definition path is not allowed: {rel}")
        if "json" not in item:
            raise ValueError(f"Remote workspace definition has no json payload: {rel}")
        save_json(project_path(root, rel), item["json"])


def write_definition_conflict(root: Path, config: Dict[str, Any], sync_id: str, local_def: Dict[str, Any], remote_def: Dict[str, Any]) -> Path:
    folder = project_path(root, config["storage"]["conflictFolder"]) / "__workspace_definition__" / sync_id
    folder.mkdir(parents=True, exist_ok=True)
    save_json(folder / "local-definition.json", local_def)
    save_json(folder / "remote-definition.json", remote_def)
    return folder


def quality_context(root: Path) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
    config, registry, _, _ = load_controls(root)
    rules_path = project_path(root, config["storage"].get("qualityRules", ".pm/quality-rules.json"))
    rules_doc = load_json(rules_path) if rules_path.is_file() else {"rules": []}
    entities, _ = collect_entities(root, registry)
    outgoing: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    incoming: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for uid_value, entity in entities.items():
        if entity.get("isDeleted"):
            continue
        for rel in entity.get("relations", []):
            edge = {
                "sourceUid": uid_value,
                "sourceType": entity.get("entityType"),
                "targetUid": rel.get("targetUid"),
                "targetType": rel.get("targetType"),
                "relation": rel.get("type"),
            }
            outgoing[uid_value].append(edge)
            incoming[str(rel.get("targetUid", ""))].append(edge)
    return config, registry, rules_doc, entities, outgoing, incoming


def assess_relation_count_rule(entity: Dict[str, Any], uid_value: str, rule: Dict[str, Any], outgoing: Dict[str, List[Dict[str, Any]]], incoming: Dict[str, List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    if rule.get("kind") != "relation-count":
        return None
    target_types = set(rule.get("targetEntityTypes") or ["*"])
    if "*" not in target_types and entity.get("entityType") not in target_types:
        return None
    status_in = set(rule.get("statusIn") or [])
    if status_in and entity.get("status") not in status_in:
        return None
    peer_types = set(rule.get("peerEntityTypes") or ["*"])
    direction = rule.get("direction", "outgoing")
    relation = rule.get("relation")
    minimum = int(rule.get("min", 0))
    edges = outgoing.get(uid_value, []) if direction == "outgoing" else incoming.get(uid_value, [])
    matched = []
    for edge in edges:
        if relation and edge.get("relation") != relation:
            continue
        peer_type = edge.get("targetType") if direction == "outgoing" else edge.get("sourceType")
        if "*" not in peer_types and peer_type not in peer_types:
            continue
        matched.append(edge)
    observed = len(matched)
    return {
        "ruleId": rule.get("id"),
        "label": rule.get("label"),
        "category": rule.get("category", "quality"),
        "severity": rule.get("severity", "warning"),
        "uid": uid_value,
        "entityType": entity.get("entityType"),
        "code": entity.get("code"),
        "title": entity.get("title"),
        "observed": observed,
        "minimum": minimum,
        "passed": observed >= minimum,
        "matchedEdges": matched,
    }


def quality_report(root: Path) -> Dict[str, Any]:
    config, _, rules_doc, entities, outgoing, incoming = quality_context(root)
    rules = rules_doc.get("rules", [])
    findings: List[Dict[str, Any]] = []
    stats: Dict[str, Dict[str, Any]] = {}
    for rule in rules:
        rule_id = str(rule.get("id") or "unnamed")
        stats[rule_id] = {
            "ruleId": rule.get("id"),
            "label": rule.get("label"),
            "category": rule.get("category", "quality"),
            "severity": rule.get("severity", "warning"),
            "applicable": 0,
            "passed": 0,
            "failed": 0,
            "coveragePercent": None,
        }
        for uid_value, entity in entities.items():
            if entity.get("isDeleted"):
                continue
            assessment = assess_relation_count_rule(entity, uid_value, rule, outgoing, incoming)
            if assessment is None:
                continue
            stat = stats[rule_id]
            stat["applicable"] += 1
            if assessment["passed"]:
                stat["passed"] += 1
            else:
                stat["failed"] += 1
                findings.append({
                    k: v for k, v in assessment.items() if k not in {"passed", "matchedEdges"}
                } | {
                    "message": f"{rule.get('label')}: found {assessment['observed']}, expected at least {assessment['minimum']}"
                })
    for stat in stats.values():
        applicable = stat["applicable"]
        stat["coveragePercent"] = round((stat["passed"] / applicable) * 100, 2) if applicable else None
    summary: Dict[str, int] = defaultdict(int)
    for item in findings:
        summary[str(item.get("severity", "warning"))] += 1
    report = {
        "schemaVersion": "2.0",
        "generatedAt": now_utc(),
        "summary": dict(summary),
        "ruleStats": list(stats.values()),
        "findings": findings,
    }
    save_json(project_path(root, config["storage"].get("qualityReport", ".pm/reports/quality-report.json")), report)
    return report


def load_intelligence(root: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if config is None:
        config, _, _, _ = load_controls(root)
    path = project_path(root, config["storage"].get("intelligence", ".pm/intelligence.json"))
    if not path.is_file():
        return {"profiles": {}, "selector": {}, "orphanDetection": {}, "context": {}, "health": {}}
    return load_json(path)


def resolve_entity_ref(root: Path, ref: str, entities: Optional[Dict[str, Dict[str, Any]]] = None, registry: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
    config, loaded_registry, _, _ = load_controls(root)
    registry = registry or loaded_registry
    entities = entities or collect_entities(root, registry)[0]
    if ref in entities:
        return ref, entities[ref]
    candidates: List[Tuple[str, Dict[str, Any]]] = []
    for uid_value, entity in entities.items():
        for field in ("id", "code", "localRef"):
            value = entity.get(field)
            if value is not None and str(value) == ref:
                candidates.append((uid_value, entity))
                break
    if not candidates:
        intelligence = load_intelligence(root, config)
        selector = intelligence.get("selector", {})
        if selector.get("allowExactTitleFallback", True):
            case_insensitive = selector.get("caseInsensitive", True)
            wanted = ref.casefold() if case_insensitive else ref
            for uid_value, entity in entities.items():
                title = str(entity.get("title") or "")
                current = title.casefold() if case_insensitive else title
                if current == wanted:
                    candidates.append((uid_value, entity))
    unique = {uid: entity for uid, entity in candidates}
    if len(unique) == 1:
        uid_value = next(iter(unique))
        return uid_value, unique[uid_value]
    if len(unique) > 1:
        labels = [f"{e.get('entityType')}:{e.get('code') or uid[:8]} {e.get('title')}" for uid, e in unique.items()]
        raise ValueError(f"Ambiguous entity reference {ref!r}: " + "; ".join(labels))
    raise KeyError(f"Entity not found by uid/id/code/localRef/exact title: {ref}")


def entity_summary(entity: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "uid": entity.get("uid"),
        "id": entity.get("id"),
        "code": entity.get("code"),
        "localRef": entity.get("localRef"),
        "entityType": entity.get("entityType"),
        "title": entity.get("title"),
        "status": entity.get("status"),
        "tags": entity.get("tags", []),
        "revision": entity.get("revision"),
        "updatedAt": entity.get("updatedAt"),
        "data": entity.get("data", {}),
    }


def graph_edges(entities: Dict[str, Dict[str, Any]], include_deleted: bool = False) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
    outgoing: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    incoming: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for uid_value, entity in entities.items():
        if entity.get("isDeleted") and not include_deleted:
            continue
        for rel in entity.get("relations", []):
            target_uid = str(rel.get("targetUid") or "")
            if not target_uid:
                continue
            target = entities.get(target_uid)
            if target and target.get("isDeleted") and not include_deleted:
                continue
            edge = {
                "sourceUid": uid_value,
                "sourceType": entity.get("entityType"),
                "relation": rel.get("type"),
                "targetUid": target_uid,
                "targetType": rel.get("targetType") or (target or {}).get("entityType"),
                "metadata": rel.get("metadata", {}),
            }
            outgoing[uid_value].append(edge)
            incoming[target_uid].append(edge)
    return outgoing, incoming


def traverse_graph(root: Path, root_uid: str, profile_name: str, max_depth: Optional[int] = None, direction: Optional[str] = None, max_entities: Optional[int] = None) -> Dict[str, Any]:
    config, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    if root_uid not in entities:
        raise KeyError(f"Root entity not found: {root_uid}")
    intelligence = load_intelligence(root, config)
    profile = (intelligence.get("profiles") or {}).get(profile_name, {})
    depth_limit = int(max_depth if max_depth is not None else profile.get("maxDepth", 2))
    direction = direction or profile.get("direction", "both")
    if direction not in {"outgoing", "incoming", "both"}:
        raise ValueError(f"Unsupported graph direction: {direction}")
    entity_limit = int(max_entities if max_entities is not None else profile.get("maxEntities", 50))
    relation_filter = set(profile.get("relations") or ["*"])
    type_filter = set(profile.get("entityTypes") or ["*"])
    outgoing, incoming = graph_edges(entities)
    queue: List[Tuple[str, int, List[Dict[str, Any]]]] = [(root_uid, 0, [])]
    visited: Dict[str, Dict[str, Any]] = {root_uid: {"depth": 0, "path": []}}
    used_edges: List[Dict[str, Any]] = []
    edge_keys = set()
    cursor = 0
    while cursor < len(queue):
        current_uid, depth, path = queue[cursor]
        cursor += 1
        if depth >= depth_limit:
            continue
        options: List[Tuple[str, Dict[str, Any], str]] = []
        if direction in {"outgoing", "both"}:
            for edge in outgoing.get(current_uid, []):
                options.append((str(edge.get("targetUid")), edge, "outgoing"))
        if direction in {"incoming", "both"}:
            for edge in incoming.get(current_uid, []):
                options.append((str(edge.get("sourceUid")), edge, "incoming"))
        for next_uid, edge, traversal_direction in options:
            if "*" not in relation_filter and edge.get("relation") not in relation_filter:
                continue
            next_entity = entities.get(next_uid)
            if not next_entity or next_entity.get("isDeleted"):
                continue
            if "*" not in type_filter and next_entity.get("entityType") not in type_filter:
                continue
            edge_key = (edge.get("sourceUid"), edge.get("relation"), edge.get("targetUid"))
            if edge_key not in edge_keys:
                used_edges.append(edge)
                edge_keys.add(edge_key)
            if next_uid in visited:
                continue
            if len(visited) >= entity_limit:
                continue
            step = {
                "fromUid": current_uid,
                "relation": edge.get("relation"),
                "direction": traversal_direction,
                "toUid": next_uid,
            }
            next_path = path + [step]
            visited[next_uid] = {"depth": depth + 1, "path": next_path}
            queue.append((next_uid, depth + 1, next_path))
    nodes = []
    for uid_value, meta in sorted(visited.items(), key=lambda item: (item[1]["depth"], item[0])):
        item = entity_summary(entities[uid_value])
        item["depth"] = meta["depth"]
        item["pathFromRoot"] = meta["path"]
        nodes.append(item)
    return {
        "schemaVersion": "1.0",
        "generatedAt": now_utc(),
        "profile": profile_name,
        "rootUid": root_uid,
        "maxDepth": depth_limit,
        "direction": direction,
        "truncated": len(visited) >= entity_limit,
        "nodes": nodes,
        "edges": used_edges,
    }


def broken_links_report(root: Path) -> Dict[str, Any]:
    _, registry, relation_map, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    mappings = relation_map.get("mappings", [])
    findings: List[Dict[str, Any]] = []
    for uid_value, entity in entities.items():
        if entity.get("isDeleted"):
            continue
        source_type = str(entity.get("entityType") or "")
        for rel in entity.get("relations", []):
            target_uid = str(rel.get("targetUid") or "")
            relation_type = str(rel.get("type") or "")
            declared_target_type = str(rel.get("targetType") or "")
            target = entities.get(target_uid)
            if not target:
                findings.append({
                    "kind": "missing-target", "sourceUid": uid_value, "sourceType": source_type,
                    "relation": relation_type, "targetUid": target_uid, "targetType": declared_target_type,
                    "message": "Relation target does not exist locally"
                })
                continue
            actual_target_type = str(target.get("entityType") or "")
            if target.get("isDeleted"):
                findings.append({
                    "kind": "deleted-target", "sourceUid": uid_value, "sourceType": source_type,
                    "relation": relation_type, "targetUid": target_uid, "targetType": actual_target_type,
                    "message": "Relation points to a soft-deleted entity"
                })
            if declared_target_type and declared_target_type != actual_target_type:
                findings.append({
                    "kind": "target-type-mismatch", "sourceUid": uid_value, "sourceType": source_type,
                    "relation": relation_type, "targetUid": target_uid,
                    "declaredTargetType": declared_target_type, "actualTargetType": actual_target_type,
                    "message": "Relation targetType does not match the target entity"
                })
            if not any(matches_mapping(source_type, relation_type, actual_target_type, m) for m in mappings):
                findings.append({
                    "kind": "mapping-not-allowed", "sourceUid": uid_value, "sourceType": source_type,
                    "relation": relation_type, "targetUid": target_uid, "targetType": actual_target_type,
                    "message": "Relation is not allowed by .pm/relation-map.json"
                })
    return {"schemaVersion": "1.0", "generatedAt": now_utc(), "count": len(findings), "findings": findings}


def orphan_report(root: Path) -> Dict[str, Any]:
    config, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    intelligence = load_intelligence(root, config)
    settings = intelligence.get("orphanDetection", {})
    ignore_status = set(settings.get("ignoreStatuses") or [])
    ignore_types = set(settings.get("ignoreEntityTypes") or [])
    outgoing, incoming = graph_edges(entities)
    result = []
    for uid_value, entity in entities.items():
        if settings.get("ignoreDeleted", True) and entity.get("isDeleted"):
            continue
        if entity.get("status") in ignore_status or entity.get("entityType") in ignore_types:
            continue
        valid_out = [e for e in outgoing.get(uid_value, []) if e.get("targetUid") in entities]
        valid_in = [e for e in incoming.get(uid_value, []) if e.get("sourceUid") in entities]
        if not valid_out and not valid_in:
            result.append(entity_summary(entity))
    return {"schemaVersion": "1.0", "generatedAt": now_utc(), "count": len(result), "entities": result}


def context_record(root: Path, entity: Dict[str, Any], detail: str, max_inline_chars: int) -> Dict[str, Any]:
    if detail == "full":
        record = copy.deepcopy(entity)
    else:
        record = entity_summary(entity)
    refs = []
    for item in entity.get("content", []):
        ref = {k: item.get(k) for k in ("name", "format", "mode", "path") if item.get(k) is not None}
        if item.get("mode") == "inline" and isinstance(item.get("body"), str):
            body = item.get("body") or ""
            ref["body"] = body[:max_inline_chars]
            ref["truncated"] = len(body) > max_inline_chars
        elif item.get("mode") == "file" and item.get("path"):
            path = project_path(root, str(item.get("path")))
            ref["exists"] = path.is_file()
            if detail == "full" and path.is_file():
                try:
                    body = path.read_text(encoding="utf-8")
                    ref["body"] = body[:max_inline_chars]
                    ref["truncated"] = len(body) > max_inline_chars
                except UnicodeDecodeError:
                    ref["binary"] = True
        refs.append(ref)
    record["contentContext"] = refs
    return record


def context_pack(root: Path, ref: str, profile: str = "context", detail: Optional[str] = None, max_depth: Optional[int] = None, max_entities: Optional[int] = None) -> Dict[str, Any]:
    config, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    root_uid, root_entity = resolve_entity_ref(root, ref, entities, registry)
    intelligence = load_intelligence(root, config)
    context_settings = intelligence.get("context", {})
    detail = detail or context_settings.get("defaultDetail", "summary")
    if detail not in {"summary", "full"}:
        raise ValueError("Context detail must be summary or full")
    graph = traverse_graph(root, root_uid, profile, max_depth=max_depth, max_entities=max_entities)
    max_chars = int(context_settings.get("maxInlineContentChars", 12000))
    graph_uids = [node["uid"] for node in graph["nodes"]]
    records = [context_record(root, entities[uid], detail, max_chars) for uid in graph_uids]
    quality = quality_report(root)
    findings = []
    if context_settings.get("includeQualityFindings", True):
        selected = set(graph_uids)
        findings = [f for f in quality.get("findings", []) if f.get("uid") in selected]
    return {
        "schemaVersion": "1.0",
        "generatedAt": now_utc(),
        "root": entity_summary(root_entity),
        "profile": profile,
        "detail": detail,
        "graph": {k: graph[k] for k in ("maxDepth", "direction", "truncated", "edges")},
        "entities": records,
        "qualityFindings": findings,
    }


def impact_report(root: Path, ref: str, max_depth: Optional[int] = None, max_entities: Optional[int] = None) -> Dict[str, Any]:
    config, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    root_uid, root_entity = resolve_entity_ref(root, ref, entities, registry)
    graph = traverse_graph(root, root_uid, "impact", max_depth=max_depth, max_entities=max_entities)
    affected = [node for node in graph["nodes"] if node.get("uid") != root_uid]
    by_type: Dict[str, int] = defaultdict(int)
    by_depth: Dict[str, int] = defaultdict(int)
    for item in affected:
        by_type[str(item.get("entityType"))] += 1
        by_depth[str(item.get("depth"))] += 1
    return {
        "schemaVersion": "1.0", "generatedAt": now_utc(), "root": entity_summary(root_entity),
        "affectedCount": len(affected), "byType": dict(sorted(by_type.items())), "byDepth": dict(sorted(by_depth.items())),
        "affected": affected, "edges": graph["edges"], "truncated": graph["truncated"]
    }


def coverage_report(root: Path, ref: Optional[str] = None, category: Optional[str] = None) -> Dict[str, Any]:
    config, registry, rules_doc, entities, outgoing, incoming = quality_context(root)
    quality = quality_report(root)
    categories = {category} if category else {"test-coverage", "automation-coverage"}
    rule_ids = {str(r.get("id")) for r in rules_doc.get("rules", []) if r.get("category", "quality") in categories}
    stats = [s for s in quality.get("ruleStats", []) if str(s.get("ruleId")) in rule_ids]
    result: Dict[str, Any] = {
        "schemaVersion": "1.0", "generatedAt": now_utc(), "categories": sorted(categories), "ruleStats": stats,
        "findings": [f for f in quality.get("findings", []) if str(f.get("ruleId")) in rule_ids]
    }
    if ref:
        uid_value, entity = resolve_entity_ref(root, ref, entities, registry)
        assessments = []
        for rule in rules_doc.get("rules", []):
            if str(rule.get("id")) not in rule_ids:
                continue
            assessment = assess_relation_count_rule(entity, uid_value, rule, outgoing, incoming)
            if assessment is not None:
                assessments.append(assessment)
        result["root"] = entity_summary(entity)
        result["assessments"] = assessments
        result["findings"] = [f for f in result["findings"] if f.get("uid") == uid_value]
    return result


def count_conflicts(root: Path, config: Dict[str, Any]) -> int:
    folder = project_path(root, config["storage"].get("conflictFolder", ".pm/conflicts"))
    if not folder.exists():
        return 0
    return sum(1 for p in folder.rglob("conflict.json") if p.is_file()) + sum(1 for p in folder.rglob("remote-definition.json") if p.is_file())


def health_report(root: Path) -> Dict[str, Any]:
    config, registry, _, sync_state = load_controls(root)
    entities, _ = collect_entities(root, registry)
    validation_errors = validate_project(root)
    quality = quality_report(root)
    broken = broken_links_report(root)
    orphans = orphan_report(root)
    by_type: Dict[str, int] = defaultdict(int)
    by_status: Dict[str, int] = defaultdict(int)
    local_only = 0
    server_assigned = 0
    dirty = 0
    synced = sync_state.get("entities", {})
    for uid_value, entity in entities.items():
        if entity.get("isDeleted"):
            continue
        by_type[str(entity.get("entityType"))] += 1
        by_status[str(entity.get("status"))] += 1
        if entity.get("id") is None and entity.get("code") is None:
            local_only += 1
        else:
            server_assigned += 1
        state = synced.get(uid_value) or {}
        try:
            current_hash = payload_hash(root, entity)
        except FileNotFoundError:
            current_hash = None
        if current_hash != state.get("lastSyncedHash"):
            dirty += 1
    report = {
        "schemaVersion": "1.0", "generatedAt": now_utc(),
        "validation": {"errorCount": len(validation_errors), "errors": validation_errors},
        "quality": {"summary": quality.get("summary", {}), "ruleStats": quality.get("ruleStats", []), "findingCount": len(quality.get("findings", []))},
        "graph": {"brokenLinkCount": broken.get("count", 0), "orphanCount": orphans.get("count", 0)},
        "entities": {"activeCount": sum(by_type.values()), "byType": dict(sorted(by_type.items())), "byStatus": dict(sorted(by_status.items()))},
        "identity": {"localOnly": local_only, "serverAssigned": server_assigned},
        "sync": {"dirtyEntities": dirty, "lastSyncAt": sync_state.get("lastSyncAt"), "cursor": sync_state.get("cursor"), "conflictCount": count_conflicts(root, config)},
    }
    return report


def emit_json(data: Dict[str, Any], output: Optional[str] = None) -> None:
    if output:
        save_json(Path(output).resolve(), data)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_trace(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    config, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    uid_value, _ = resolve_entity_ref(root, args.ref, entities, registry)
    report = traverse_graph(root, uid_value, args.profile, max_depth=args.max_depth, direction=args.direction, max_entities=args.max_entities)
    emit_json(report, args.output)
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    report = context_pack(Path(args.project).resolve(), args.ref, args.profile, args.detail, args.max_depth, args.max_entities)
    emit_json(report, args.output)
    return 0


def cmd_impact(args: argparse.Namespace) -> int:
    report = impact_report(Path(args.project).resolve(), args.ref, args.max_depth, args.max_entities)
    emit_json(report, args.output)
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    report = coverage_report(Path(args.project).resolve(), args.ref, args.category)
    emit_json(report, args.output)
    return 0


def cmd_orphans(args: argparse.Namespace) -> int:
    report = orphan_report(Path(args.project).resolve())
    emit_json(report, args.output)
    return 1 if args.fail_if_found and report.get("count", 0) else 0


def cmd_broken_links(args: argparse.Namespace) -> int:
    report = broken_links_report(Path(args.project).resolve())
    emit_json(report, args.output)
    return 1 if args.fail_if_found and report.get("count", 0) else 0


def cmd_missing_tests(args: argparse.Namespace) -> int:
    report = coverage_report(Path(args.project).resolve(), args.ref, "test-coverage")
    emit_json(report, args.output)
    return 1 if args.fail_if_found and report.get("findings") else 0


def cmd_health(args: argparse.Namespace) -> int:
    report = health_report(Path(args.project).resolve())
    emit_json(report, args.output)
    if args.fail_on_validation and report["validation"]["errorCount"]:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Change management / audit / review / baseline
# ---------------------------------------------------------------------------

def load_change_policy(root: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if config is None:
        config, _, _, _ = load_controls(root)
    rel = config.get("storage", {}).get("changeManagement", ".pm/change-management.json")
    path = project_path(root, rel)
    if path.is_file():
        return load_json(path)
    return {
        "schemaVersion": "1.0",
        "audit": {"enabled": True, "captureReferencedTextFiles": True},
        "review": {"requireReasonForReject": True},
        "baseline": {"captureReferencedTextFiles": True},
    }


def change_paths(root: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Path]:
    if config is None:
        config, _, _, _ = load_controls(root)
    storage = config.get("storage", {})
    return {
        "changesets": project_path(root, storage.get("changeSetFolder", ".pm/changesets")),
        "baselines": project_path(root, storage.get("baselineFolder", ".pm/baselines")),
        "auditState": project_path(root, storage.get("auditState", ".pm/audit-state.json")),
    }


def entity_record(root: Path, entity: Dict[str, Any]) -> Dict[str, Any]:
    files = content_file_entries(root, entity)
    return {
        "payloadHash": payload_hash(root, entity),
        "entity": copy.deepcopy(entity),
        "files": files,
    }


def capture_workspace_records(root: Path) -> Dict[str, Dict[str, Any]]:
    _, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    return {uid_value: entity_record(root, entity) for uid_value, entity in entities.items()}


def record_hash(record: Optional[Dict[str, Any]]) -> Optional[str]:
    if not record:
        return None
    return record.get("payloadHash")


def actor_object(actor: Optional[str], source: str) -> Dict[str, Any]:
    value = actor or os.environ.get("PM_ACTOR") or ("sync:remote" if source == "sync" else "local-user")
    kind = "ai" if value.startswith("ai:") else ("system" if value.startswith("system:") or value.startswith("sync:") else "user")
    return {"id": value, "kind": kind}


def infer_operation(before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> str:
    if before is None and after is not None:
        return "create"
    if before is not None and after is None:
        return "purge"
    b = (before or {}).get("entity") or {}
    a = (after or {}).get("entity") or {}
    if not b.get("isDeleted") and a.get("isDeleted"):
        return "soft-delete"
    if b.get("isDeleted") and not a.get("isDeleted"):
        return "restore"
    if b.get("status") != a.get("status"):
        return "transition"
    if b.get("relations") != a.get("relations"):
        return "relation-update"
    return "update"


def make_changes(before: Dict[str, Dict[str, Any]], after: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    changes = []
    for uid_value in sorted(set(before) | set(after)):
        b = before.get(uid_value)
        a = after.get(uid_value)
        if record_hash(b) == record_hash(a):
            continue
        entity = ((a or b) or {}).get("entity") or {}
        changes.append({
            "uid": uid_value,
            "entityType": entity.get("entityType"),
            "operation": infer_operation(b, a),
            "beforeHash": record_hash(b),
            "afterHash": record_hash(a),
            "before": b,
            "after": a,
        })
    return changes


def new_change_set_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"CHG-{stamp}-{uuid.uuid4().hex[:8]}"


def write_change_set(
    root: Path,
    changes: List[Dict[str, Any]],
    *,
    status: str,
    source: str,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    related: Optional[List[str]] = None,
    change_set_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    allow_empty: bool = False,
) -> Optional[Dict[str, Any]]:
    if not changes and not allow_empty:
        return None
    config, _, _, _ = load_controls(root)
    policy = load_change_policy(root, config)
    if not policy.get("audit", {}).get("enabled", True) and status == "applied":
        return None
    paths = change_paths(root, config)
    paths["changesets"].mkdir(parents=True, exist_ok=True)
    change_set_id = change_set_id or new_change_set_id()
    doc = {
        "schemaVersion": "1.0",
        "changeSetId": change_set_id,
        "auditRevision": 1,
        "status": status,
        "source": source,
        "actor": actor_object(actor, source),
        "reason": reason or source,
        "related": related or [],
        "createdAt": now_utc(),
        "updatedAt": now_utc(),
        "changes": changes,
        "events": [
            {"type": "created", "at": now_utc(), "actor": actor_object(actor, source), "status": status}
        ],
    }
    if extra:
        doc.update(copy.deepcopy(extra))
    save_json(paths["changesets"] / f"{change_set_id}.json", doc)
    return doc


def load_change_sets(root: Path) -> List[Tuple[Path, Dict[str, Any]]]:
    config, _, _, _ = load_controls(root)
    folder = change_paths(root, config)["changesets"]
    if not folder.exists():
        return []
    result = []
    for path in sorted(folder.glob("*.json")):
        try:
            result.append((path, load_json(path)))
        except Exception:
            continue
    return result


def find_change_set(root: Path, change_set_id: str) -> Tuple[Path, Dict[str, Any]]:
    for path, doc in load_change_sets(root):
        if doc.get("changeSetId") == change_set_id:
            return path, doc
    raise KeyError(f"Change set not found: {change_set_id}")


def update_audit_state(root: Path, records: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    config, _, _, _ = load_controls(root)
    path = change_paths(root, config)["auditState"]
    records = records if records is not None else capture_workspace_records(root)
    save_json(path, {
        "schemaVersion": "1.0",
        "capturedAt": now_utc(),
        "entities": records,
    })


def load_audit_state(root: Path) -> Dict[str, Dict[str, Any]]:
    config, _, _, _ = load_controls(root)
    path = change_paths(root, config)["auditState"]
    if not path.is_file():
        return {}
    doc = load_json(path)
    return doc.get("entities", {}) if isinstance(doc, dict) else {}


def apply_entity_record(root: Path, uid_value: str, record: Optional[Dict[str, Any]], current: Optional[Dict[str, Any]] = None) -> None:
    config, registry, _, sync_state = load_controls(root)
    current_entity = (current or {}).get("entity") if current else None
    target_entity = (record or {}).get("entity") if record else None
    locked = (sync_state.get("entities", {}).get(uid_value) or {}).get("serverIdentity")
    if target_entity is None:
        if locked:
            raise ValueError(f"Cannot physically remove server-assigned entity {uid_value}; use soft delete or explicit server workflow")
        if current_entity:
            path = entity_dest(root, registry, current_entity["entityType"], uid_value)
            if path.exists():
                path.unlink()
    else:
        path = entity_dest(root, registry, target_entity["entityType"], uid_value)
        save_json(path, target_entity)
    current_files = {f.get("path"): f for f in (current or {}).get("files", []) if f.get("path")}
    target_files = {f.get("path"): f for f in (record or {}).get("files", []) if f.get("path")}
    for rel, f in target_files.items():
        target = project_path(root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if f.get("encoding") == "utf-8":
            target.write_text(f.get("content") or "", encoding="utf-8")
        elif f.get("content") is not None:
            raise ValueError(f"Binary audit restore is not supported: {rel}")
    for rel in sorted(set(current_files) - set(target_files)):
        if rel.startswith("files/"):
            target = project_path(root, rel)
            if target.is_file():
                target.unlink()


def json_diff(before: Any, after: Any, path: str = "$") -> List[Dict[str, Any]]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}"
            if key not in before:
                out.append({"path": child, "kind": "added", "before": None, "after": after[key]})
            elif key not in after:
                out.append({"path": child, "kind": "removed", "before": before[key], "after": None})
            else:
                out.extend(json_diff(before[key], after[key], child))
        return out
    if isinstance(before, list) and isinstance(after, list):
        return [{"path": path, "kind": "changed", "before": before, "after": after}]
    return [{"path": path, "kind": "changed", "before": before, "after": after}]


def merge_patch(target: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value) if isinstance(value, dict) else copy.deepcopy(value)
    return result


def validate_proposed_entity(root: Path, before_entity: Dict[str, Any], candidate: Dict[str, Any]) -> None:
    immutable = ["uid", "entityType", "localRef", "id", "code", "createdAt"]
    for field in immutable:
        if candidate.get(field) != before_entity.get(field):
            raise ValueError(f"Proposal cannot change immutable field {field}")
    _, registry, _, _ = load_controls(root)
    errors = validate_entity_definition(root, registry, candidate["entityType"], candidate)
    if errors:
        raise ValueError("Proposed entity is invalid:\n- " + "\n- ".join(errors))


def cmd_history(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    uid_value = None
    if args.ref:
        uid_value, _ = resolve_entity_ref(root, args.ref)
    result = []
    for _, doc in load_change_sets(root):
        touched = [c for c in doc.get("changes", []) if uid_value is None or c.get("uid") == uid_value]
        if not touched:
            continue
        result.append({
            "changeSetId": doc.get("changeSetId"),
            "status": doc.get("status"),
            "source": doc.get("source"),
            "actor": doc.get("actor"),
            "reason": doc.get("reason"),
            "createdAt": doc.get("createdAt"),
            "updatedAt": doc.get("updatedAt"),
            "changes": [{"uid": c.get("uid"), "entityType": c.get("entityType"), "operation": c.get("operation")} for c in touched],
        })
    result.sort(key=lambda x: str(x.get("createdAt") or ""), reverse=True)
    if args.limit:
        result = result[:args.limit]
    emit_json({"count": len(result), "history": result}, args.output)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    uid_value, entity = resolve_entity_ref(root, args.ref)
    current = entity_record(root, entity)
    before_record = None
    after_record = current
    source = "current-vs-previous"
    if args.baseline:
        baseline = load_baseline(root, args.baseline)
        before_record = (baseline.get("entities") or {}).get(uid_value)
        source = f"baseline:{baseline.get('name')}"
    elif args.change_set:
        _, cs = find_change_set(root, args.change_set)
        change = next((c for c in cs.get("changes", []) if c.get("uid") == uid_value), None)
        if not change:
            raise ValueError(f"Change set {args.change_set} does not touch {uid_value}")
        before_record = change.get("before")
        after_record = change.get("after")
        source = f"changeset:{args.change_set}"
    else:
        candidates = []
        for _, cs in load_change_sets(root):
            for c in cs.get("changes", []):
                if c.get("uid") == uid_value and cs.get("status") in {"applied", "reverted"}:
                    candidates.append((str(cs.get("createdAt") or ""), c, cs))
        if candidates:
            _, change, cs = sorted(candidates, key=lambda x: x[0])[-1]
            before_record = change.get("before")
            after_record = change.get("after")
            source = f"latest:{cs.get('changeSetId')}"
    before_entity = (before_record or {}).get("entity")
    after_entity = (after_record or {}).get("entity")
    file_before = {f.get("path"): f.get("content") for f in (before_record or {}).get("files", [])}
    file_after = {f.get("path"): f.get("content") for f in (after_record or {}).get("files", [])}
    report = {
        "uid": uid_value,
        "ref": args.ref,
        "source": source,
        "entityDiff": json_diff(before_entity, after_entity),
        "fileDiff": json_diff(file_before, file_after, "$.files"),
    }
    emit_json(report, args.output)
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    uid_value, entity = resolve_entity_ref(root, args.ref)
    patch = json.loads(args.patch_json)
    if not isinstance(patch, dict):
        raise ValueError("--patch-json must be a JSON object")
    candidate = merge_patch(entity, patch)
    candidate["revision"] = int(entity.get("revision", 0)) + 1
    candidate["updatedAt"] = now_utc()
    validate_proposed_entity(root, entity, candidate)
    before = entity_record(root, entity)
    after = {"payloadHash": None, "entity": candidate, "files": copy.deepcopy(before.get("files", []))}
    # Hash the proposed logical payload without writing it. Referenced files are unchanged.
    h = hashlib.sha256()
    h.update(canonical_json_bytes(candidate))
    for f in sorted(after["files"], key=lambda x: str(x.get("path"))):
        h.update(b"\0FILE\0")
        h.update(str(f.get("path") or "").encode("utf-8"))
        h.update(b"\0")
        h.update((f.get("content") or "").encode("utf-8"))
    after["payloadHash"] = "sha256:" + h.hexdigest()
    changes = make_changes({uid_value: before}, {uid_value: after})
    cs = write_change_set(root, changes, status="pending", source="proposal", actor=args.actor or "ai:agent", reason=args.reason, related=args.related)
    if not cs:
        raise RuntimeError("Proposal produced no changes")
    print(json.dumps({"changeSetId": cs["changeSetId"], "status": cs["status"], "uid": uid_value}, ensure_ascii=False))
    return 0


def cmd_reviews(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    result = []
    for _, doc in load_change_sets(root):
        if doc.get("status") != "pending":
            continue
        result.append({
            "changeSetId": doc.get("changeSetId"), "actor": doc.get("actor"), "reason": doc.get("reason"),
            "createdAt": doc.get("createdAt"), "changes": [{"uid": c.get("uid"), "entityType": c.get("entityType"), "operation": c.get("operation")} for c in doc.get("changes", [])]
        })
    result.sort(key=lambda x: str(x.get("createdAt") or ""))
    emit_json({"count": len(result), "reviews": result}, args.output)
    return 0


def cmd_review_show(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    _, doc = find_change_set(root, args.change_set)
    report = copy.deepcopy(doc)
    for change in report.get("changes", []):
        change["entityDiff"] = json_diff(((change.get("before") or {}).get("entity")), ((change.get("after") or {}).get("entity")))
    emit_json(report, args.output)
    return 0


def cmd_review_approve(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    path, doc = find_change_set(root, args.change_set)
    if doc.get("status") != "pending":
        raise ValueError(f"Change set is not pending: {doc.get('status')}")
    current_records = capture_workspace_records(root)
    for change in doc.get("changes", []):
        uid_value = str(change.get("uid"))
        if record_hash(current_records.get(uid_value)) != change.get("beforeHash"):
            raise ValueError(f"Entity {uid_value} changed since proposal; review again before approval")
    applied = []
    try:
        for change in doc.get("changes", []):
            uid_value = str(change.get("uid"))
            apply_entity_record(root, uid_value, change.get("after"), current_records.get(uid_value))
            applied.append(uid_value)
        errors = validate_project(root)
        if errors:
            raise ValueError("Approved proposal would make project invalid:\n- " + "\n- ".join(errors))
    except Exception:
        for uid_value in reversed(applied):
            change = next(c for c in doc.get("changes", []) if str(c.get("uid")) == uid_value)
            latest = capture_workspace_records(root).get(uid_value)
            apply_entity_record(root, uid_value, change.get("before"), latest)
        build_manifest(root)
        raise
    doc["status"] = "applied"
    doc["auditRevision"] = int(doc.get("auditRevision", 1)) + 1
    doc["updatedAt"] = now_utc()
    doc.setdefault("events", []).append({"type": "approved", "at": now_utc(), "actor": actor_object(args.actor, "review"), "reason": args.reason})
    save_json(path, doc)
    build_manifest(root)
    update_audit_state(root)
    print(json.dumps({"changeSetId": doc["changeSetId"], "status": doc["status"]}, ensure_ascii=False))
    return 0


def cmd_review_reject(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    path, doc = find_change_set(root, args.change_set)
    if doc.get("status") != "pending":
        raise ValueError(f"Change set is not pending: {doc.get('status')}")
    policy = load_change_policy(root)
    if policy.get("review", {}).get("requireReasonForReject", True) and not args.reason:
        raise ValueError("A rejection reason is required")
    doc["status"] = "rejected"
    doc["auditRevision"] = int(doc.get("auditRevision", 1)) + 1
    doc["updatedAt"] = now_utc()
    doc.setdefault("events", []).append({"type": "rejected", "at": now_utc(), "actor": actor_object(args.actor, "review"), "reason": args.reason})
    save_json(path, doc)
    print(json.dumps({"changeSetId": doc["changeSetId"], "status": doc["status"]}, ensure_ascii=False))
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    original_path, doc = find_change_set(root, args.change_set)
    if doc.get("status") not in {"applied", "reverted"}:
        raise ValueError(f"Only applied changes can be undone; status={doc.get('status')}")
    current_records = capture_workspace_records(root)
    for change in doc.get("changes", []):
        uid_value = str(change.get("uid"))
        current_hash = record_hash(current_records.get(uid_value))
        if not args.force and current_hash != change.get("afterHash"):
            raise ValueError(f"Entity {uid_value} changed after {args.change_set}; use --force only after reviewing the newer state")
    before_all = capture_workspace_records(root)
    applied = []
    try:
        for change in reversed(doc.get("changes", [])):
            uid_value = str(change.get("uid"))
            apply_entity_record(root, uid_value, change.get("before"), current_records.get(uid_value))
            applied.append(uid_value)
        errors = validate_project(root)
        if errors:
            raise ValueError("Undo would make project invalid:\n- " + "\n- ".join(errors))
    except Exception:
        latest = capture_workspace_records(root)
        for uid_value in reversed(applied):
            apply_entity_record(root, uid_value, before_all.get(uid_value), latest.get(uid_value))
        build_manifest(root)
        raise
    after_all = capture_workspace_records(root)
    reverse_changes = make_changes(before_all, after_all)
    undo_doc = write_change_set(root, reverse_changes, status="applied", source="undo", actor=args.actor, reason=args.reason or f"Undo {args.change_set}", related=[args.change_set])
    doc.setdefault("events", []).append({"type": "reverted", "at": now_utc(), "actor": actor_object(args.actor, "undo"), "revertedBy": (undo_doc or {}).get("changeSetId")})
    doc["auditRevision"] = int(doc.get("auditRevision", 1)) + 1
    doc["updatedAt"] = now_utc()
    save_json(original_path, doc)
    build_manifest(root)
    update_audit_state(root, after_all)
    print(json.dumps({"undone": args.change_set, "changeSetId": (undo_doc or {}).get("changeSetId")}, ensure_ascii=False))
    return 0


def cmd_audit_scan(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    current = capture_workspace_records(root)
    previous = load_audit_state(root)
    if args.initialize or not previous:
        update_audit_state(root, current)
        print(json.dumps({"initialized": True, "entities": len(current)}, ensure_ascii=False))
        return 0
    changes = make_changes(previous, current)
    cs = write_change_set(root, changes, status="applied", source="external-edit", actor=args.actor, reason=args.reason or "Detected direct/manual workspace edits")
    update_audit_state(root, current)
    print(json.dumps({"changeSetId": (cs or {}).get("changeSetId"), "changes": len(changes)}, ensure_ascii=False))
    return 0


def safe_baseline_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    if not value:
        raise ValueError("Baseline name must contain letters or numbers")
    return value


def baseline_path(root: Path, name: str) -> Path:
    config, _, _, _ = load_controls(root)
    return change_paths(root, config)["baselines"] / f"{safe_baseline_name(name)}.json"


def load_baseline(root: Path, name: str) -> Dict[str, Any]:
    path = baseline_path(root, name)
    if not path.is_file():
        raise KeyError(f"Baseline not found: {name}")
    return load_json(path)


def cmd_baseline_create(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    records = capture_workspace_records(root)
    path = baseline_path(root, args.name)
    if path.exists() and not args.force:
        raise ValueError(f"Baseline already exists: {args.name}. Use --force to replace it")
    project = require_project(root)
    doc = {
        "schemaVersion": "1.0",
        "baselineId": f"BASE-{uuid.uuid4().hex[:12]}",
        "name": args.name,
        "label": args.label or args.name,
        "release": args.release,
        "projectUid": project.get("uid"),
        "createdAt": now_utc(),
        "actor": actor_object(args.actor, "baseline"),
        "reason": args.reason or "Project baseline",
        "manifestHash": sha256_bytes(canonical_json_bytes({uid: rec.get("payloadHash") for uid, rec in sorted(records.items())})),
        "entities": records,
    }
    save_json(path, doc)
    print(json.dumps({"name": args.name, "baselineId": doc["baselineId"], "entities": len(records), "manifestHash": doc["manifestHash"]}, ensure_ascii=False))
    return 0


def cmd_baseline_list(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    config, _, _, _ = load_controls(root)
    folder = change_paths(root, config)["baselines"]
    result = []
    if folder.exists():
        for path in sorted(folder.glob("*.json")):
            doc = load_json(path)
            result.append({k: doc.get(k) for k in ("name", "baselineId", "label", "release", "createdAt", "manifestHash")})
    emit_json({"count": len(result), "baselines": result}, args.output)
    return 0


def baseline_records_or_current(root: Path, value: str) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    if value == "current":
        return "current", capture_workspace_records(root)
    doc = load_baseline(root, value)
    return str(doc.get("name") or value), doc.get("entities", {})


def cmd_baseline_compare(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    from_name, before = baseline_records_or_current(root, args.from_baseline)
    to_name, after = baseline_records_or_current(root, args.to_baseline)
    added, removed, modified = [], [], []
    for uid_value in sorted(set(before) | set(after)):
        b, a = before.get(uid_value), after.get(uid_value)
        if b is None:
            added.append(entity_summary((a or {}).get("entity") or {}))
        elif a is None:
            removed.append(entity_summary((b or {}).get("entity") or {}))
        elif record_hash(b) != record_hash(a):
            entity = (a or b).get("entity") or {}
            modified.append({
                **entity_summary(entity),
                "beforeHash": record_hash(b),
                "afterHash": record_hash(a),
                "entityDiff": json_diff((b or {}).get("entity"), (a or {}).get("entity")),
                "fileDiff": json_diff({f.get("path"): f.get("content") for f in (b or {}).get("files", [])}, {f.get("path"): f.get("content") for f in (a or {}).get("files", [])}, "$.files"),
            })
    report = {"from": from_name, "to": to_name, "summary": {"added": len(added), "removed": len(removed), "modified": len(modified)}, "added": added, "removed": removed, "modified": modified}
    emit_json(report, args.output)
    return 0


def bundle_change_files(root: Path) -> List[Dict[str, Any]]:
    config, _, _, _ = load_controls(root)
    paths = change_paths(root, config)
    result = []
    state = paths["auditState"]
    if state.is_file():
        result.append({"path": state.relative_to(root).as_posix(), "json": load_json(state)})
    for key in ("changesets", "baselines"):
        folder = paths[key]
        if folder.exists():
            for path in sorted(folder.glob("*.json")):
                result.append({"path": path.relative_to(root).as_posix(), "json": load_json(path)})
    return result


def restore_bundle_change_files(root: Path, items: List[Dict[str, Any]]) -> None:
    allowed_prefixes = (".pm/changesets/", ".pm/baselines/")
    for item in items:
        rel = str(item.get("path") or "")
        if rel != ".pm/audit-state.json" and not rel.startswith(allowed_prefixes):
            raise ValueError(f"Unsupported change-management bundle path: {rel}")
        save_json(project_path(root, rel), item.get("json"))


def bundle_workplan_files(root: Path) -> List[Dict[str, Any]]:
    result = []
    for path, doc in load_workplans(root):
        result.append({"path": path.relative_to(root).as_posix(), "json": doc})
    return result


def restore_bundle_workplan_files(root: Path, items: List[Dict[str, Any]]) -> None:
    for item in items:
        rel = str(item.get("path") or "")
        if not rel.startswith(".pm/workplans/PLAN-") or not rel.endswith(".json"):
            raise ValueError(f"Unsupported WorkPlan bundle path: {rel}")
        save_json(project_path(root, rel), item.get("json"))


def audit_mutation(root: Path, before: Dict[str, Dict[str, Any]], command: str, args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    after = capture_workspace_records(root)
    changes = make_changes(before, after)
    if not changes:
        update_audit_state(root, after)
        return None
    actor = getattr(args, "actor", None)
    reason = getattr(args, "reason", None) or command
    related = getattr(args, "related", None)
    source = f"cli:{command}"
    if command == "sync":
        source = "sync"
    doc = write_change_set(root, changes, status="applied", source=source, actor=actor, reason=reason, related=related)
    update_audit_state(root, after)
    return doc


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if root.exists() and any(root.iterdir()) and not args.force:
        raise RuntimeError(f"Target folder is not empty: {root}. Use --force to overwrite template files.")
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE_ROOT, root, dirs_exist_ok=True)
    project_uid = str(uuid.uuid4())
    client_uid = str(uuid.uuid4())
    created = now_utc()
    replacements = {
        "__PROJECT_UID__": project_uid,
        "__PROJECT_LOCAL_REF__": f"local:project:{project_uid[:8]}",
        "__PROJECT_NAME__": args.name,
        "__CLIENT_INSTANCE_ID__": client_uid,
        "__CREATED_AT__": created,
    }
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for old, new in replacements.items():
            text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")
    if args.timezone or args.language:
        project = load_json(root / "project.json")
        if args.timezone:
            project["settings"]["timezone"] = args.timezone
        if args.language:
            project["settings"]["defaultLanguage"] = args.language
        save_json(root / "project.json", project)
    build_manifest(root)
    print(f"Initialized project workspace: {root}")
    print(f"Project uid: {project_uid}")
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    require_project(root)
    _, registry, _, _ = load_controls(root)
    spec = registry.get("types", {}).get(args.type)
    if not spec:
        raise KeyError(f"Unknown entity type {args.type!r}. Configure it in .pm/entity-types.json first.")
    uid_value = str(uuid.uuid4())
    ts = now_utc()
    defaults = copy.deepcopy(spec.get("defaults", {}))
    entity = {
        "schemaVersion": "1.0",
        "entityType": args.type,
        "uid": uid_value,
        "id": None,
        "code": None,
        "localRef": f"local:{args.type}:{uid_value[:8]}",
        "title": args.title,
        "status": defaults.pop("status", "draft"),
        "isDeleted": False,
        "deletedAt": None,
        "tags": [],
        "relations": [],
        "data": {},
        "content": [],
        "revision": 1,
        "createdAt": ts,
        "updatedAt": ts,
    }
    entity = deep_merge(entity, defaults)
    if args.data_json:
        entity["data"] = deep_merge(entity.get("data", {}), json.loads(args.data_json))
    definition_errors = validate_entity_definition(root, registry, args.type, entity)
    if definition_errors:
        raise ValueError("Entity data is invalid:\n- " + "\n- ".join(definition_errors))
    path = entity_dest(root, registry, args.type, uid_value)
    save_json(path, entity)
    build_manifest(root)
    print(json.dumps({"uid": uid_value, "localRef": entity["localRef"], "path": path.relative_to(root).as_posix()}, ensure_ascii=False))
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    _, registry, relation_map, _ = load_controls(root)
    entities, paths = collect_entities(root, registry)
    source = entities.get(args.source_uid)
    target = entities.get(args.target_uid)
    if not source:
        raise KeyError(f"Source uid not found: {args.source_uid}")
    if not target:
        raise KeyError(f"Target uid not found: {args.target_uid}")
    mappings = [m for m in relation_map.get("mappings", []) if matches_mapping(source["entityType"], args.relation, target["entityType"], m)]
    if not mappings:
        raise ValueError(f"Relation not allowed: {source['entityType']} --{args.relation}--> {target['entityType']}")
    duplicate = any(r.get("type") == args.relation and r.get("targetUid") == args.target_uid for r in source.get("relations", []))
    if duplicate:
        print("Relation already exists")
        return 0
    for m in mappings:
        if m.get("cardinality") in {"many-to-one", "one-to-one"}:
            current = [r for r in source.get("relations", []) if matches_mapping(source["entityType"], r.get("type", ""), r.get("targetType", ""), m)]
            if current:
                raise ValueError(f"Mapping {m.get('key')} allows at most one target from the source side")
    source.setdefault("relations", []).append({
        "type": args.relation,
        "targetUid": target["uid"],
        "targetType": target["entityType"],
        "metadata": {},
    })
    source["revision"] = int(source.get("revision", 0)) + 1
    source["updatedAt"] = now_utc()
    save_json(paths[args.source_uid], source)
    errors = validate_project(root)
    if errors:
        # Roll back link if it violates reverse cardinality or another mapping constraint.
        source["relations"] = [r for r in source["relations"] if not (r.get("type") == args.relation and r.get("targetUid") == args.target_uid)]
        source["revision"] -= 1
        save_json(paths[args.source_uid], source)
        raise ValueError("Link would make project invalid:\n- " + "\n- ".join(errors))
    build_manifest(root)
    print(f"Linked {args.source_uid} --{args.relation}--> {args.target_uid}")
    return 0



def cmd_unlink(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    _, registry, _, _ = load_controls(root)
    entities, paths = collect_entities(root, registry)
    source = entities.get(args.source_uid)
    if not source:
        raise KeyError(f"Source uid not found: {args.source_uid}")
    original_relations = copy.deepcopy(source.get("relations", []))
    original_revision = int(source.get("revision", 0))
    original_updated_at = source.get("updatedAt")
    before = len(original_relations)
    source["relations"] = [r for r in original_relations if not (r.get("type") == args.relation and r.get("targetUid") == args.target_uid)]
    if len(source["relations"]) == before:
        print("Relation not found")
        return 0
    source["revision"] = original_revision + 1
    source["updatedAt"] = now_utc()
    save_json(paths[args.source_uid], source)
    errors = validate_project(root)
    if errors:
        source["relations"] = original_relations
        source["revision"] = original_revision
        source["updatedAt"] = original_updated_at
        save_json(paths[args.source_uid], source)
        raise ValueError("Unlink would make project invalid:\n- " + "\n- ".join(errors))
    build_manifest(root)
    print(f"Unlinked {args.source_uid} --{args.relation}--> {args.target_uid}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    _, registry, _, _ = load_controls(root)
    entities, paths = collect_entities(root, registry)
    entity = entities.get(args.uid)
    if not entity:
        raise KeyError(f"uid not found: {args.uid}")
    changed = False
    if args.title is not None and args.title != entity.get("title"):
        entity["title"] = args.title
        changed = True
    if args.data_json:
        patch = json.loads(args.data_json)
        if not isinstance(patch, dict):
            raise ValueError("--data-json must be a JSON object")
        entity["data"] = deep_merge(entity.get("data", {}), patch)
        changed = True
    if args.tags_json:
        tags = json.loads(args.tags_json)
        if not isinstance(tags, list) or not all(isinstance(x, str) for x in tags):
            raise ValueError("--tags-json must be a JSON array of strings")
        entity["tags"] = tags
        changed = True
    if not changed:
        print("No changes")
        return 0
    definition_errors = validate_entity_definition(root, registry, entity["entityType"], entity)
    if definition_errors:
        raise ValueError("Update is invalid:\n- " + "\n- ".join(definition_errors))
    entity["revision"] = int(entity.get("revision", 0)) + 1
    entity["updatedAt"] = now_utc()
    save_json(paths[args.uid], entity)
    build_manifest(root)
    print(f"Updated {args.uid}")
    return 0


def cmd_transition(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    _, registry, _, _ = load_controls(root)
    entities, paths = collect_entities(root, registry)
    entity = entities.get(args.uid)
    if not entity:
        raise KeyError(f"uid not found: {args.uid}")
    spec = registry.get("types", {}).get(entity["entityType"], {})
    lifecycle = spec.get("lifecycle") or {}
    statuses = lifecycle.get("statuses") or {}
    target = args.status
    current = str(entity.get("status"))
    if target not in statuses:
        raise ValueError(f"Status {target!r} is not configured for {entity['entityType']}")
    if target == current:
        print("Status unchanged")
        return 0
    allowed = (lifecycle.get("transitions") or {}).get(current, [])
    if not args.force and target not in allowed:
        raise ValueError(f"Transition {current!r} -> {target!r} is not allowed. Allowed: {allowed}")
    entity["status"] = target
    entity["revision"] = int(entity.get("revision", 0)) + 1
    entity["updatedAt"] = now_utc()
    save_json(paths[args.uid], entity)
    build_manifest(root)
    print(f"Transitioned {args.uid}: {current} -> {target}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    _, registry, _, _ = load_controls(root)
    entities, paths = collect_entities(root, registry)
    entity = entities.get(args.uid)
    if not entity:
        raise KeyError(f"uid not found: {args.uid}")
    if not entity.get("isDeleted"):
        print("Entity is not deleted")
        return 0
    entity["isDeleted"] = False
    entity["deletedAt"] = None
    entity["revision"] = int(entity.get("revision", 0)) + 1
    entity["updatedAt"] = now_utc()
    save_json(paths[args.uid], entity)
    build_manifest(root)
    print(f"Restored {args.uid}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    clauses = []
    if getattr(args, "expr", None): clauses.append(f"({args.expr})")
    if getattr(args, "type", None): clauses.append("(" + " OR ".join(f"type={json.dumps(x)}" for x in args.type) + ")")
    if getattr(args, "status", None): clauses.append("(" + " OR ".join(f"status={json.dumps(x)}" for x in args.status) + ")")
    if getattr(args, "tag", None): clauses.extend(f"tag={json.dumps(x)}" for x in args.tag)
    if getattr(args, "text", None):
        # Backward-compatible text filter uses indexed search first, then narrows query rows.
        search = search_local(root, args.text, entity_types=args.type, statuses=args.status, tags=args.tag, include_deleted=args.include_deleted, limit=int(getattr(args, "limit", 1000)), rebuild_if_stale=not getattr(args, "no_reindex", False))
        allowed = {r["uid"] for r in search.get("results", [])}
    else:
        allowed = None
    expression = " AND ".join(clauses)
    rows = query_entities(root, expression=expression, include_deleted=args.include_deleted, related_to=getattr(args, "related_to", None), direction=getattr(args, "direction", "both"), relations=getattr(args, "relation", None), max_depth=int(getattr(args, "max_depth", 1)), limit=int(getattr(args, "limit", 100)))
    if allowed is not None:
        rows = [x for x in rows if x.get("uid") in allowed]
    summaries = []
    for entity in rows:
        summaries.append({
            "uid": entity.get("uid"), "entityType": entity.get("entityType"), "id": entity.get("id"), "code": entity.get("code"),
            "localRef": entity.get("localRef"), "title": entity.get("title"), "status": entity.get("status"), "tags": entity.get("tags", []),
            "updatedAt": entity.get("updatedAt"), "path": entity.get("__path"),
        })
    output = json.dumps(summaries, indent=2, ensure_ascii=False)
    if getattr(args, "output", None): Path(args.output).write_text(output + "\n", encoding="utf-8")
    else: print(output)
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    manifest = build_manifest(root)
    config, _, _, _ = load_controls(root)
    search_path = project_path(root, config["storage"].get("searchIndex", ".pm/indexes/search-index.json"))
    index = load_json(search_path)
    source_index = build_source_index(root)
    dependency_index = build_dependency_index(root)
    print(json.dumps({"entities": len(manifest.get("entities", [])), "sourceFingerprint": manifest.get("sourceFingerprint"), "searchTokens": len(index.get("inverted", {})), "searchIndex": search_path.relative_to(root).as_posix(), "sourceFiles": source_index.get("fileCount", 0), "sourceIndex": source_index_path(root, config).relative_to(root).as_posix(), "dependencyEdges": dependency_index.get("edgeCount", 0), "dependencyIndex": dependency_index_path(root, config).relative_to(root).as_posix()}, ensure_ascii=False))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    report = search_local(root, args.text, entity_types=args.type, statuses=args.status, tags=args.tag, include_deleted=args.include_deleted, limit=args.limit, rebuild_if_stale=not args.no_reindex)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output: Path(args.output).write_text(text + "\n", encoding="utf-8")
    else: print(text)
    return 0


def cmd_view_list(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    views = load_views(root).get("views") or {}
    result = [{"viewId": key, "title": value.get("title", key), "description": value.get("description"), "expression": value.get("expression")} for key, value in sorted(views.items())]
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_view_show(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    views = load_views(root).get("views") or {}
    if args.view not in views: raise KeyError(f"Unknown saved view: {args.view}")
    print(json.dumps({"viewId": args.view, **views[args.view]}, indent=2, ensure_ascii=False))
    return 0


def cmd_view_run(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    result = run_saved_view(root, args.view, limit=args.limit)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output: Path(args.output).write_text(text + "\n", encoding="utf-8")
    else: print(text)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    before = capture_workspace_records(root) if getattr(args, "fix_level", "safe") == "semantic" else None
    applied = {"safe": 0, "semanticEntities": 0}
    if args.fix:
        # Safe repairs affect only folders and derived caches.
        config, registry, _, _ = load_controls(root)
        for spec in registry.get("types", {}).values():
            project_path(root, spec["folder"]).mkdir(parents=True, exist_ok=True)
        project_path(root, config["storage"].get("intelligenceReportFolder", ".pm/reports/intelligence")).mkdir(parents=True, exist_ok=True)
        project_path(root, config["storage"].get("searchIndex", ".pm/indexes/search-index.json")).parent.mkdir(parents=True, exist_ok=True)
        try:
            build_manifest(root)
            build_source_index(root)
            build_dependency_index(root)
            applied["safe"] += 1
        except Exception as exc:
            applied["safeError"] = str(exc)
        if args.fix_level == "semantic":
            applied["semanticEntities"] = apply_doctor_semantic_fixes(root)
            if applied["semanticEntities"]:
                build_manifest(root)
                after = capture_workspace_records(root)
                changes = make_changes(before or {}, after)
                if changes:
                    write_change_set(root, changes, status="applied", source="doctor", actor=args.actor, reason=args.reason or "Project doctor semantic repair", related=[])
                    update_audit_state(root, after)
    report = doctor_report(root)
    report["fixesApplied"] = applied
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output: Path(args.output).write_text(text + "\n", encoding="utf-8")
    else: print(text)
    return 1 if report.get("summary", {}).get("error", 0) else 0



def cmd_describe(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    config, registry, relation_map, _ = load_controls(root)
    lookups_path = project_path(root, config["storage"].get("lookups", ".pm/lookups.json"))
    lookups = load_json(lookups_path) if lookups_path.is_file() else {"lookups": {}}
    if not args.type:
        result = [{"entityType": key, "displayName": spec.get("displayName"), "category": spec.get("category"), "folder": spec.get("folder")} for key, spec in registry.get("types", {}).items()]
    else:
        spec = registry.get("types", {}).get(args.type)
        if not spec:
            raise KeyError(f"Unknown entity type: {args.type}")
        schema = load_json(project_path(root, spec["dataSchema"])) if spec.get("dataSchema") else None
        mappings = [m for m in relation_map.get("mappings", []) if "*" in m.get("from", []) or args.type in m.get("from", [])]
        result = {"entityType": args.type, "definition": spec, "dataSchema": schema, "relationMappings": mappings, "lookups": lookups.get("lookups", {})}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_quality(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    report = quality_report(root)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    severities = set(args.fail_on or [])
    return 1 if any(item.get("severity") in severities for item in report.get("findings", [])) else 0


def cmd_delete(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    _, registry, _, _ = load_controls(root)
    entities, paths = collect_entities(root, registry)
    entity = entities.get(args.uid)
    if not entity:
        raise KeyError(f"uid not found: {args.uid}")
    entity["isDeleted"] = True
    entity["deletedAt"] = now_utc()
    entity["revision"] = int(entity.get("revision", 0)) + 1
    entity["updatedAt"] = entity["deletedAt"]
    save_json(paths[args.uid], entity)
    build_manifest(root)
    print(f"Soft-deleted {args.uid}")
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    manifest = build_manifest(root)
    print(f"Manifest rebuilt: {len(manifest['entities'])} entities")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    errors = validate_project(root)
    if errors:
        print(f"Validation failed with {len(errors)} error(s):")
        for item in errors:
            print(f"- {item}")
        return 1
    print("Validation passed")
    return 0


def latest_conflict_token(root: Path, conflict_folder: str, uid_value: str) -> Optional[str]:
    base = project_path(root, conflict_folder) / uid_value
    if not base.exists():
        return None
    candidates = sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)
    for folder in candidates:
        path = folder / "conflict.json"
        if path.exists():
            data = load_json(path)
            token = data.get("conflictToken")
            if token:
                return str(token)
    return None


def write_conflict(root: Path, config: Dict[str, Any], sync_id: str, uid_value: str, conflict: Dict[str, Any], local_entity: Optional[Dict[str, Any]]) -> Path:
    folder = project_path(root, config["storage"]["conflictFolder"]) / uid_value / sync_id
    folder.mkdir(parents=True, exist_ok=True)
    save_json(folder / "conflict.json", conflict)
    if local_entity is not None:
        save_json(folder / "local.json", local_entity)
    remote_entity = conflict.get("remoteEntity") or conflict.get("entity")
    if remote_entity is not None:
        save_json(folder / "remote.json", remote_entity)
    if conflict.get("remoteFiles") is not None:
        save_json(folder / "remote-files.json", conflict.get("remoteFiles"))
    return folder


def apply_server_identity(entity: Dict[str, Any], server_id: Any, server_code: Any, locked: Optional[Dict[str, Any]]) -> None:
    if locked:
        if server_id is not None and locked.get("id") != server_id:
            raise ValueError(f"Server id changed after lock: {locked.get('id')!r} -> {server_id!r}")
        if server_code is not None and locked.get("code") != server_code:
            raise ValueError(f"Server code changed after lock: {locked.get('code')!r} -> {server_code!r}")
        entity["id"] = locked.get("id")
        entity["code"] = locked.get("code")
        return
    # First server mapping: server identity is authoritative even over legacy provisional values.
    if server_id is not None:
        entity["id"] = server_id
    if server_code is not None:
        entity["code"] = server_code


def make_change(root: Path, entity: Dict[str, Any], state: Optional[Dict[str, Any]], force_local: bool, conflict_folder: str) -> Dict[str, Any]:
    uid_value = str(entity["uid"])
    return {
        "operation": "delete" if entity.get("isDeleted") else "upsert",
        "entityType": entity["entityType"],
        "uid": uid_value,
        "baseRemoteVersion": (state or {}).get("remoteVersion"),
        "baseRemoteUpdatedAt": (state or {}).get("remoteUpdatedAt"),
        "payloadHash": payload_hash(root, entity),
        "conflictToken": latest_conflict_token(root, conflict_folder, uid_value) if force_local else None,
        "entity": entity,
        "files": content_file_entries(root, entity),
    }


def apply_remote_files(root: Path, files: List[Dict[str, Any]]) -> None:
    for item in files or []:
        rel = item.get("path")
        if not rel:
            continue
        path = project_path(root, rel)
        encoding = item.get("encoding", "utf-8")
        if encoding != "utf-8":
            raise ValueError(f"Binary remote file transfer is not supported by this CLI: {rel}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.get("content") or "", encoding="utf-8")



# ---------------------------------------------------------------------------
# Agent workflow / WorkPlan planning, review, execution
# ---------------------------------------------------------------------------

def load_agent_workflow(root: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if config is None:
        config, _, _, _ = load_controls(root)
    rel = config.get("storage", {}).get("agentWorkflow", ".pm/agent-workflow.json")
    path = project_path(root, rel)
    if path.is_file():
        return load_json(path)
    return {
        "schemaVersion": "1.0",
        "planning": {
            "requireApprovalBeforeExecute": True,
            "protectAgainstStaleContext": True,
            "maxSteps": 100,
            "allowedOperations": ["create", "update", "content-upsert", "link", "unlink", "transition", "delete", "restore"],
        },
        "execution": {"atomic": True, "validateAfterPlan": True, "runQualityAfterPlan": True, "blockAtQualitySeverity": "error"},
        "review": {"approvalInvalidatedByRefresh": True, "requireReasonForReject": True},
        "sync": {"enabled": True, "bidirectional": True},
    }


def workplan_folder(root: Path, config: Optional[Dict[str, Any]] = None) -> Path:
    if config is None:
        config, _, _, _ = load_controls(root)
    return project_path(root, config.get("storage", {}).get("workPlanFolder", ".pm/workplans"))


def safe_plan_id(value: str) -> str:
    if not re.fullmatch(r"PLAN-[A-Za-z0-9._-]+", value or ""):
        raise ValueError(f"Invalid planId: {value!r}")
    return value


def new_plan_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"PLAN-{stamp}-{uuid.uuid4().hex[:8]}"


def workplan_path(root: Path, plan_id: str, config: Optional[Dict[str, Any]] = None) -> Path:
    return workplan_folder(root, config) / f"{safe_plan_id(plan_id)}.json"


def load_workplans(root: Path) -> List[Tuple[Path, Dict[str, Any]]]:
    folder = workplan_folder(root)
    if not folder.exists():
        return []
    result: List[Tuple[Path, Dict[str, Any]]] = []
    for path in sorted(folder.glob("PLAN-*.json")):
        try:
            result.append((path, load_json(path)))
        except Exception:
            continue
    return result


def find_workplan(root: Path, plan_id: str) -> Tuple[Path, Dict[str, Any]]:
    path = workplan_path(root, plan_id)
    if not path.is_file():
        raise KeyError(f"WorkPlan not found: {plan_id}")
    return path, load_json(path)


def workplan_hash(doc: Dict[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(doc))


def symbolic_plan_ref(ref: Any) -> bool:
    return isinstance(ref, str) and (ref.startswith("$step:") or ref.startswith("$handle:"))


def plan_ref_creator(ref: str, steps: List[Dict[str, Any]]) -> Optional[str]:
    if ref.startswith("$step:"):
        return ref.split(":", 1)[1]
    if ref.startswith("$handle:"):
        handle = ref.split(":", 1)[1]
        for step in steps:
            if step.get("handle") == handle:
                return str(step.get("stepId"))
    return None


def ordered_plan_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id = {str(s.get("stepId")): s for s in steps}
    order = {str(s.get("stepId")): i for i, s in enumerate(steps)}
    deps: Dict[str, set] = {}
    for step in steps:
        sid = str(step.get("stepId"))
        current = set(str(x) for x in (step.get("dependsOn") or []))
        for key in ("targetRef", "sourceRef"):
            ref = step.get(key)
            if symbolic_plan_ref(ref):
                creator = plan_ref_creator(str(ref), steps)
                if creator:
                    current.add(creator)
        deps[sid] = current
    for sid, values in deps.items():
        if sid in values:
            raise ValueError(f"WorkPlan step {sid} depends on itself")
        unknown = sorted(x for x in values if x not in by_id)
        if unknown:
            raise ValueError(f"WorkPlan step {sid} has unknown dependencies: {unknown}")
    result: List[Dict[str, Any]] = []
    remaining = set(by_id)
    done = set()
    while remaining:
        ready = sorted((sid for sid in remaining if deps[sid].issubset(done)), key=lambda x: order[x])
        if not ready:
            raise ValueError("WorkPlan step dependency cycle detected")
        for sid in ready:
            result.append(by_id[sid])
            done.add(sid)
            remaining.remove(sid)
    return result


def validate_workplan_doc(root: Path, doc: Dict[str, Any], *, resolve_refs: bool = True) -> List[str]:
    errors: List[str] = []
    policy = load_agent_workflow(root)
    planning = policy.get("planning", {})
    _, registry, relation_map, _ = load_controls(root)
    known_types = set(registry.get("types", {}))
    relation_types = set((relation_map.get("relationTypes") or {}).keys())
    mappings = relation_map.get("mappings", [])
    required = ["schemaVersion", "planId", "title", "request", "status", "actor", "steps", "createdAt", "updatedAt"]
    for field in required:
        if field not in doc:
            errors.append(f"missing field {field}")
    if doc.get("schemaVersion") != "1.0":
        errors.append(f"unsupported schemaVersion {doc.get('schemaVersion')!r}")
    try:
        safe_plan_id(str(doc.get("planId") or ""))
    except Exception as exc:
        errors.append(str(exc))
    statuses = {"draft", "pending-review", "approved", "executing", "completed", "failed", "rejected", "cancelled"}
    if doc.get("status") not in statuses:
        errors.append(f"invalid status {doc.get('status')!r}")
    if not str(doc.get("title") or "").strip():
        errors.append("title is required")
    if not str(doc.get("request") or "").strip():
        errors.append("request is required")
    steps = doc.get("steps")
    if not isinstance(steps, list):
        errors.append("steps must be an array")
        return errors
    max_steps = int(planning.get("maxSteps", 100))
    if len(steps) > max_steps:
        errors.append(f"steps exceeds configured maxSteps={max_steps}")
    allowed = set(planning.get("allowedOperations") or [])
    ids: List[str] = []
    handles: List[str] = []
    by_step: Dict[str, Dict[str, Any]] = {}
    by_handle: Dict[str, Dict[str, Any]] = {}
    forbidden_update = {"uid", "id", "code", "localRef", "entityType", "revision", "createdAt", "updatedAt", "status", "isDeleted", "deletedAt"}
    forbidden_create = {"uid", "id", "code", "localRef", "entityType", "revision", "createdAt", "updatedAt", "relations", "isDeleted", "deletedAt"}

    for i, step in enumerate(steps):
        prefix = f"steps[{i}]"
        if not isinstance(step, dict):
            errors.append(f"{prefix} must be an object")
            continue
        sid = str(step.get("stepId") or "")
        if not sid:
            errors.append(f"{prefix}.stepId is required")
        elif sid in ids:
            errors.append(f"duplicate stepId {sid}")
        else:
            by_step[sid] = step
        ids.append(sid)
        op = str(step.get("operation") or "")
        if not op:
            errors.append(f"{prefix}.operation is required")
        elif allowed and op not in allowed:
            errors.append(f"{prefix}.operation {op!r} is not allowed")
        deps = step.get("dependsOn", [])
        if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
            errors.append(f"{prefix}.dependsOn must be an array of step ids")
        handle = step.get("handle")
        if handle:
            if op != "create":
                errors.append(f"{prefix}.handle is only valid for create steps")
            if str(handle) in handles:
                errors.append(f"duplicate create handle {handle!r}")
            else:
                by_handle[str(handle)] = step
            handles.append(str(handle))
        if op == "create":
            entity_type = str(step.get("entityType") or "")
            if not entity_type:
                errors.append(f"{prefix}.entityType is required for create")
            elif entity_type not in known_types:
                errors.append(f"{prefix}.entityType {entity_type!r} is not configured")
            payload = step.get("payload") or {}
            if not isinstance(payload, dict):
                errors.append(f"{prefix}.payload must be an object")
            else:
                bad = sorted(forbidden_create.intersection(payload))
                if bad:
                    errors.append(f"{prefix}.payload contains protected fields: {bad}")
                if not str(payload.get("title") or step.get("title") or "").strip():
                    errors.append(f"{prefix} create needs payload.title or step.title")
                if "tags" in payload and (not isinstance(payload.get("tags"), list) or not all(isinstance(x, str) for x in payload.get("tags", []))):
                    errors.append(f"{prefix}.payload.tags must be an array of strings")
                if "data" in payload and not isinstance(payload.get("data"), dict):
                    errors.append(f"{prefix}.payload.data must be an object")
        elif op == "update":
            if not step.get("targetRef"):
                errors.append(f"{prefix}.targetRef is required for update")
            patch = step.get("patch")
            if not isinstance(patch, dict):
                errors.append(f"{prefix}.patch must be an object for update")
            else:
                bad = sorted(forbidden_update.intersection(patch))
                if bad:
                    errors.append(f"{prefix}.patch contains protected/lifecycle fields: {bad}")
        elif op == "content-upsert":
            if not step.get("targetRef"):
                errors.append(f"{prefix}.targetRef is required for content-upsert")
            content = step.get("content")
            if not isinstance(content, dict):
                errors.append(f"{prefix}.content must be an object")
            else:
                if not content.get("name"):
                    errors.append(f"{prefix}.content.name is required")
                if content.get("mode", "inline") not in {"inline", "file"}:
                    errors.append(f"{prefix}.content.mode must be inline or file")
                if "body" not in content or not isinstance(content.get("body"), str):
                    errors.append(f"{prefix}.content.body must be a string")
                if content.get("filename") and Path(str(content.get("filename"))).name != str(content.get("filename")):
                    errors.append(f"{prefix}.content.filename must be a plain file name")
        elif op in {"link", "unlink"}:
            if not step.get("sourceRef") or not step.get("targetRef") or not step.get("relation"):
                errors.append(f"{prefix} {op} requires sourceRef, relation, and targetRef")
            elif relation_types and step.get("relation") not in relation_types:
                errors.append(f"{prefix}.relation {step.get('relation')!r} is not configured")
        elif op == "transition":
            if not step.get("targetRef") or not step.get("status"):
                errors.append(f"{prefix} transition requires targetRef and status")
        elif op in {"delete", "restore"}:
            if not step.get("targetRef"):
                errors.append(f"{prefix} {op} requires targetRef")

    def creator_for(ref: str) -> Optional[Dict[str, Any]]:
        if ref.startswith("$step:"):
            return by_step.get(ref.split(":", 1)[1])
        if ref.startswith("$handle:"):
            return by_handle.get(ref.split(":", 1)[1])
        return None

    def ref_type(ref: Any) -> Optional[str]:
        if not isinstance(ref, str) or not ref:
            return None
        if symbolic_plan_ref(ref):
            creator = creator_for(ref)
            if not creator:
                errors.append(f"symbolic reference {ref!r} does not point to a create step/handle")
                return None
            if creator.get("operation") != "create":
                errors.append(f"symbolic reference {ref!r} must point to a create step")
                return None
            return str(creator.get("entityType") or "") or None
        if not resolve_refs:
            return None
        try:
            _, entity = resolve_entity_ref(root, ref)
            return str(entity.get("entityType") or "") or None
        except Exception as exc:
            errors.append(f"reference {ref!r}: {exc}")
            return None

    # Resolve every literal/symbolic reference exactly once for early feedback.
    referenced: Dict[str, Optional[str]] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        for key in ("sourceRef", "targetRef"):
            ref = step.get(key)
            if isinstance(ref, str) and ref and ref not in referenced:
                referenced[ref] = ref_type(ref)
    for ref in doc.get("contextRefs", []) or []:
        if isinstance(ref, str) and ref and ref not in referenced:
            referenced[ref] = ref_type(ref)

    # Preflight relation mappings and lifecycle targets where types can be inferred.
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        prefix = f"steps[{i}]"
        op = step.get("operation")
        if op in {"link", "unlink"}:
            source_type = referenced.get(str(step.get("sourceRef")))
            target_type = referenced.get(str(step.get("targetRef")))
            relation = str(step.get("relation") or "")
            if source_type and target_type and relation:
                if not any(matches_mapping(source_type, relation, target_type, m) for m in mappings):
                    errors.append(f"{prefix}: relation not allowed: {source_type} --{relation}--> {target_type}")
        elif op == "transition":
            target_type = referenced.get(str(step.get("targetRef")))
            status = str(step.get("status") or "")
            if target_type and status:
                statuses = ((registry.get("types", {}).get(target_type, {}).get("lifecycle") or {}).get("statuses") or {})
                if status not in statuses:
                    errors.append(f"{prefix}: status {status!r} is not configured for {target_type}")

    try:
        ordered_plan_steps([s for s in steps if isinstance(s, dict)])
    except Exception as exc:
        errors.append(str(exc))
    return errors

def validate_workplan_storage_doc(doc: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    required = ["schemaVersion", "planId", "title", "request", "status", "actor", "steps", "createdAt", "updatedAt"]
    for field in required:
        if field not in doc:
            errors.append(f"missing field {field}")
    if doc.get("schemaVersion") != "1.0":
        errors.append(f"unsupported schemaVersion {doc.get('schemaVersion')!r}")
    try:
        safe_plan_id(str(doc.get("planId") or ""))
    except Exception as exc:
        errors.append(str(exc))
    statuses = {"draft", "pending-review", "approved", "executing", "completed", "failed", "rejected", "cancelled"}
    if doc.get("status") not in statuses:
        errors.append(f"invalid status {doc.get('status')!r}")
    if not isinstance(doc.get("steps"), list):
        errors.append("steps must be an array")
    if not isinstance(doc.get("events", []), list):
        errors.append("events must be an array")
    return errors


def validate_workplans(root: Path) -> List[str]:
    """Validate WorkPlan storage integrity without reinterpreting historical plans against today's registry/mappings."""
    errors: List[str] = []
    folder = workplan_folder(root)
    if not folder.exists():
        return errors
    seen = set()
    for path in sorted(folder.glob("PLAN-*.json")):
        try:
            doc = load_json(path)
        except Exception as exc:
            errors.append(f"{path}: invalid JSON: {exc}")
            continue
        pid = str(doc.get("planId") or "")
        if pid in seen:
            errors.append(f"Duplicate WorkPlan planId: {pid}")
        seen.add(pid)
        if path.stem != pid:
            errors.append(f"{path}: filename must match planId {pid!r}")
        for item in validate_workplan_storage_doc(doc):
            errors.append(f"{path}: {item}")
    return errors

def collect_plan_literal_refs(doc: Dict[str, Any]) -> List[str]:
    refs: List[str] = []
    for ref in doc.get("contextRefs", []) or []:
        if isinstance(ref, str) and ref and not symbolic_plan_ref(ref):
            refs.append(ref)
    for step in doc.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        for key in ("targetRef", "sourceRef"):
            ref = step.get(key)
            if isinstance(ref, str) and ref and not symbolic_plan_ref(ref):
                refs.append(ref)
    out: List[str] = []
    seen = set()
    for ref in refs:
        if ref not in seen:
            out.append(ref)
            seen.add(ref)
    return out


def capture_plan_base(root: Path, doc: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    records = capture_workspace_records(root)
    by_uid: Dict[str, Dict[str, Any]] = {}
    for ref in collect_plan_literal_refs(doc):
        uid_value, entity = resolve_entity_ref(root, ref)
        item = by_uid.setdefault(uid_value, {
            "refs": [],
            "uid": uid_value,
            "entityType": entity.get("entityType"),
            "code": entity.get("code"),
            "title": entity.get("title"),
            "payloadHash": record_hash(records.get(uid_value)),
        })
        item["refs"].append(ref)
    config, registry, _, _ = load_controls(root)
    definition_hash = workspace_definition(root, config, registry).get("payloadHash")
    return list(by_uid.values()), str(definition_hash or "")


def workplan_stale_findings(root: Path, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    config, registry, _, _ = load_controls(root)
    current_definition = workspace_definition(root, config, registry).get("payloadHash")
    base_definition = doc.get("baseWorkspaceDefinitionHash")
    if base_definition and current_definition != base_definition:
        findings.append({"kind": "workspace-definition-changed", "before": base_definition, "current": current_definition})
    records = capture_workspace_records(root)
    for base in doc.get("baseObjects", []) or []:
        uid_value = str(base.get("uid") or "")
        current = records.get(uid_value)
        if current is None:
            findings.append({"kind": "entity-missing", "uid": uid_value, "beforeHash": base.get("payloadHash")})
            continue
        current_hash = record_hash(current)
        if current_hash != base.get("payloadHash"):
            entity = current.get("entity") or {}
            findings.append({
                "kind": "entity-changed",
                "uid": uid_value,
                "entityType": entity.get("entityType"),
                "code": entity.get("code"),
                "title": entity.get("title"),
                "beforeHash": base.get("payloadHash"),
                "currentHash": current_hash,
            })
    return findings


def plan_event(doc: Dict[str, Any], event_type: str, actor: Optional[str], reason: Optional[str] = None, **extra: Any) -> None:
    event = {"type": event_type, "at": now_utc(), "actor": actor_object(actor, "workplan")}
    if reason is not None:
        event["reason"] = reason
    event.update(extra)
    doc.setdefault("events", []).append(event)
    doc["updatedAt"] = now_utc()


def load_plan_spec(args: argparse.Namespace) -> Dict[str, Any]:
    if getattr(args, "spec_file", None):
        value = load_json(Path(args.spec_file).resolve())
    elif getattr(args, "spec_json", None):
        value = json.loads(args.spec_json)
    else:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("WorkPlan spec must be a JSON object")
    return value


def cmd_plan_context(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    require_project(root)
    refs = args.ref or []
    contexts = []
    impacts = []
    for ref in refs:
        contexts.append(context_pack(root, ref, args.profile or "context", detail="summary", max_depth=args.max_depth, max_entities=args.max_entities))
        if args.include_impact:
            impacts.append(impact_report(root, ref, max_depth=args.impact_depth, max_entities=args.max_entities))
    config, registry, _, _ = load_controls(root)
    result = {
        "request": args.request,
        "project": {k: require_project(root).get(k) for k in ("uid", "id", "code", "name")},
        "entityTypes": [{"entityType": key, "displayName": spec.get("displayName")} for key, spec in registry.get("types", {}).items()],
        "contextRefs": refs,
        "contexts": contexts,
        "impacts": impacts,
        "workspaceDefinitionHash": workspace_definition(root, config, registry).get("payloadHash"),
    }
    emit_json(result, args.output)
    return 0


def cmd_plan_create(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    require_project(root)
    spec = load_plan_spec(args)
    plan_id = str(spec.get("planId") or new_plan_id())
    safe_plan_id(plan_id)
    path = workplan_path(root, plan_id)
    if path.exists():
        raise ValueError(f"WorkPlan already exists: {plan_id}")
    policy = load_agent_workflow(root)
    reason = args.reason if args.reason is not None else spec.get("reason")
    if policy.get("planning", {}).get("requireReason", True) and not reason:
        raise ValueError("WorkPlan reason is required")
    request_text = args.request or spec.get("request")
    if not request_text:
        raise ValueError("WorkPlan request is required")
    title = args.title or spec.get("title") or str(request_text)[:100]
    context_refs = list(spec.get("contextRefs") or []) + list(args.context_ref or [])
    related = list(spec.get("related") or []) + list(args.related or [])
    ts = now_utc()
    doc = {
        "schemaVersion": "1.0",
        "planId": plan_id,
        "title": title,
        "request": request_text,
        "status": "draft",
        "actor": actor_object(args.actor or spec.get("actorId") or "ai:agent", "workplan"),
        "reason": reason,
        "related": list(dict.fromkeys(str(x) for x in related)),
        "contextRefs": list(dict.fromkeys(str(x) for x in context_refs)),
        "baseObjects": [],
        "baseWorkspaceDefinitionHash": None,
        "assumptions": spec.get("assumptions") or [],
        "risks": spec.get("risks") or [],
        "acceptanceCriteria": spec.get("acceptanceCriteria") or [],
        "steps": spec.get("steps") or [],
        "execution": {"changeSetId": None, "startedAt": None, "completedAt": None, "stepResults": []},
        "events": [],
        "createdAt": ts,
        "updatedAt": ts,
    }
    plan_event(doc, "created", args.actor or "ai:agent", reason)
    errors = validate_workplan_doc(root, doc, resolve_refs=True)
    if errors:
        raise ValueError("WorkPlan is invalid:\n- " + "\n- ".join(errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, doc)
    print(json.dumps({"planId": plan_id, "status": doc["status"], "path": path.relative_to(root).as_posix()}, ensure_ascii=False))
    return 0


def cmd_plans(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    statuses = set(args.status or [])
    result = []
    for _, doc in load_workplans(root):
        if statuses and doc.get("status") not in statuses:
            continue
        result.append({
            "planId": doc.get("planId"), "title": doc.get("title"), "status": doc.get("status"),
            "actor": doc.get("actor"), "reason": doc.get("reason"), "steps": len(doc.get("steps") or []),
            "createdAt": doc.get("createdAt"), "updatedAt": doc.get("updatedAt"),
            "changeSetId": (doc.get("execution") or {}).get("changeSetId"),
        })
    result.sort(key=lambda x: str(x.get("updatedAt") or ""), reverse=True)
    emit_json({"count": len(result), "plans": result}, args.output)
    return 0


def cmd_plan_show(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    _, doc = find_workplan(root, args.plan)
    result = copy.deepcopy(doc)
    result["staleFindings"] = workplan_stale_findings(root, doc) if doc.get("baseObjects") or doc.get("baseWorkspaceDefinitionHash") else []
    emit_json(result, args.output)
    return 0


def cmd_plan_validate(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    _, doc = find_workplan(root, args.plan)
    errors = validate_workplan_doc(root, doc, resolve_refs=True)
    stale = workplan_stale_findings(root, doc) if doc.get("status") in {"pending-review", "approved", "executing"} else []
    report = {"planId": doc.get("planId"), "status": doc.get("status"), "valid": not errors, "errors": errors, "staleFindings": stale}
    emit_json(report, args.output)
    return 1 if errors or (args.fail_on_stale and stale) else 0


def cmd_plan_amend(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    path, doc = find_workplan(root, args.plan)
    if doc.get("status") not in {"draft", "failed"}:
        raise ValueError(f"Only draft/failed WorkPlans can be amended; status={doc.get('status')}. Use plan-refresh first when re-review is required.")
    spec = load_plan_spec(args)
    mutable = {"title", "request", "reason", "contextRefs", "related", "assumptions", "risks", "acceptanceCriteria", "steps"}
    unknown = sorted(set(spec) - mutable)
    if unknown:
        raise ValueError(f"Unsupported WorkPlan amend fields: {unknown}")
    for field in mutable:
        if field in spec:
            doc[field] = copy.deepcopy(spec[field])
    if "steps" in spec and doc.get("steps"):
        doc["requiresAuthoring"] = False
    doc["baseObjects"] = []
    doc["baseWorkspaceDefinitionHash"] = None
    doc["execution"] = {"changeSetId": None, "startedAt": None, "completedAt": None, "stepResults": []}
    errors = validate_workplan_doc(root, doc, resolve_refs=True)
    if errors:
        raise ValueError("Amended WorkPlan is invalid:\n- " + "\n- ".join(errors))
    plan_event(doc, "amended", args.actor, args.reason)
    save_json(path, doc)
    print(json.dumps({"planId": doc["planId"], "status": doc["status"], "steps": len(doc.get("steps") or [])}, ensure_ascii=False))
    return 0


def cmd_plan_submit(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    path, doc = find_workplan(root, args.plan)
    if doc.get("status") not in {"draft", "failed"}:
        raise ValueError(f"Only draft/failed plans can be submitted; status={doc.get('status')}")
    errors = validate_workplan_doc(root, doc, resolve_refs=True)
    if not doc.get("steps"):
        errors.append("steps must contain at least one executable step before submission")
    if doc.get("requiresAuthoring"):
        errors.append("generated change-analysis shell still requires authoring; use plan-amend with reviewed executable steps")
    if errors:
        raise ValueError("WorkPlan is invalid:\n- " + "\n- ".join(errors))
    policy = load_agent_workflow(root)
    if policy.get("planning", {}).get("requireAcceptanceCriteria", False) and not doc.get("acceptanceCriteria"):
        raise ValueError("WorkPlan acceptanceCriteria is required by policy")
    base, definition_hash = capture_plan_base(root, doc)
    doc["baseObjects"] = base
    doc["baseWorkspaceDefinitionHash"] = definition_hash
    doc["status"] = "pending-review"
    doc["execution"] = {"changeSetId": None, "startedAt": None, "completedAt": None, "stepResults": []}
    plan_event(doc, "submitted", args.actor, args.reason)
    save_json(path, doc)
    print(json.dumps({"planId": doc["planId"], "status": doc["status"], "baseObjects": len(base)}, ensure_ascii=False))
    return 0


def cmd_plan_refresh(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    path, doc = find_workplan(root, args.plan)
    if doc.get("status") in {"completed", "rejected", "cancelled", "executing"}:
        raise ValueError(f"Cannot refresh WorkPlan in status {doc.get('status')}")
    errors = validate_workplan_doc(root, doc, resolve_refs=True)
    if errors:
        raise ValueError("WorkPlan is invalid:\n- " + "\n- ".join(errors))
    base, definition_hash = capture_plan_base(root, doc)
    doc["baseObjects"] = base
    doc["baseWorkspaceDefinitionHash"] = definition_hash
    previous = doc.get("status")
    doc["status"] = "draft"
    doc["execution"] = {"changeSetId": None, "startedAt": None, "completedAt": None, "stepResults": []}
    plan_event(doc, "refreshed", args.actor, args.reason, previousStatus=previous)
    save_json(path, doc)
    print(json.dumps({"planId": doc["planId"], "status": doc["status"], "baseObjects": len(base)}, ensure_ascii=False))
    return 0


def cmd_plan_approve(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    path, doc = find_workplan(root, args.plan)
    if doc.get("status") != "pending-review":
        raise ValueError(f"WorkPlan is not pending-review: {doc.get('status')}")
    stale = workplan_stale_findings(root, doc)
    if stale:
        raise ValueError("WorkPlan context changed after submission; refresh and review again: " + json.dumps(stale, ensure_ascii=False))
    doc["status"] = "approved"
    plan_event(doc, "approved", args.actor, args.reason)
    save_json(path, doc)
    print(json.dumps({"planId": doc["planId"], "status": doc["status"]}, ensure_ascii=False))
    return 0


def cmd_plan_reject(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    path, doc = find_workplan(root, args.plan)
    if doc.get("status") != "pending-review":
        raise ValueError(f"WorkPlan is not pending-review: {doc.get('status')}")
    policy = load_agent_workflow(root)
    if policy.get("review", {}).get("requireReasonForReject", True) and not args.reason:
        raise ValueError("A rejection reason is required")
    doc["status"] = "rejected"
    plan_event(doc, "rejected", args.actor, args.reason)
    save_json(path, doc)
    print(json.dumps({"planId": doc["planId"], "status": doc["status"]}, ensure_ascii=False))
    return 0


def cmd_plan_cancel(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    path, doc = find_workplan(root, args.plan)
    if doc.get("status") in {"completed", "rejected", "cancelled", "executing"}:
        raise ValueError(f"Cannot cancel WorkPlan in status {doc.get('status')}")
    doc["status"] = "cancelled"
    plan_event(doc, "cancelled", args.actor, args.reason)
    save_json(path, doc)
    print(json.dumps({"planId": doc["planId"], "status": doc["status"]}, ensure_ascii=False))
    return 0


def resolve_plan_runtime_ref(root: Path, ref: str, outputs: Dict[str, Dict[str, str]]) -> Tuple[str, Dict[str, Any]]:
    if ref.startswith("$step:"):
        sid = ref.split(":", 1)[1]
        uid_value = outputs.get("steps", {}).get(sid)
        if not uid_value:
            raise ValueError(f"No entity output is available for step {sid}")
        return resolve_entity_ref(root, uid_value)
    if ref.startswith("$handle:"):
        handle = ref.split(":", 1)[1]
        uid_value = outputs.get("handles", {}).get(handle)
        if not uid_value:
            raise ValueError(f"No entity output is available for handle {handle}")
        return resolve_entity_ref(root, uid_value)
    return resolve_entity_ref(root, ref)


def plan_create_entity(root: Path, step: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    _, registry, _, _ = load_controls(root)
    entity_type = str(step.get("entityType") or "")
    spec = registry.get("types", {}).get(entity_type)
    if not spec:
        raise KeyError(f"Unknown entity type {entity_type!r}")
    payload = copy.deepcopy(step.get("payload") or {})
    forbidden = {"uid", "id", "code", "localRef", "entityType", "revision", "createdAt", "updatedAt", "relations", "isDeleted", "deletedAt"}
    bad = sorted(forbidden.intersection(payload))
    if bad:
        raise ValueError(f"Create payload cannot define protected fields: {bad}")
    uid_value = str(uuid.uuid4())
    ts = now_utc()
    defaults = copy.deepcopy(spec.get("defaults", {}))
    entity = {
        "schemaVersion": "1.0", "entityType": entity_type, "uid": uid_value, "id": None, "code": None,
        "localRef": f"local:{entity_type}:{uid_value[:8]}", "title": str(payload.pop("title", None) or step.get("title") or ""),
        "status": defaults.pop("status", "draft"), "isDeleted": False, "deletedAt": None, "tags": [], "relations": [],
        "data": {}, "content": [], "revision": 1, "createdAt": ts, "updatedAt": ts,
    }
    entity = deep_merge(entity, defaults)
    if "data" in payload:
        if not isinstance(payload["data"], dict):
            raise ValueError("create payload.data must be an object")
        entity["data"] = deep_merge(entity.get("data", {}), payload.pop("data"))
    if "tags" in payload:
        if not isinstance(payload["tags"], list) or not all(isinstance(x, str) for x in payload["tags"]):
            raise ValueError("create payload.tags must be an array of strings")
        entity["tags"] = payload.pop("tags")
    if "content" in payload:
        if not isinstance(payload["content"], list):
            raise ValueError("create payload.content must be an array")
        entity["content"] = payload.pop("content")
    if "status" in payload:
        entity["status"] = payload.pop("status")
    if payload:
        raise ValueError(f"Unsupported create payload fields: {sorted(payload)}")
    errors = validate_entity_definition(root, registry, entity_type, entity)
    if errors:
        raise ValueError("Created entity is invalid:\n- " + "\n- ".join(errors))
    save_json(entity_dest(root, registry, entity_type, uid_value), entity)
    return uid_value, entity


def plan_update_entity(root: Path, uid_value: str, entity: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    forbidden = {"uid", "id", "code", "localRef", "entityType", "revision", "createdAt", "updatedAt", "status", "isDeleted", "deletedAt"}
    bad = sorted(forbidden.intersection(patch))
    if bad:
        raise ValueError(f"Update patch cannot change protected/lifecycle fields: {bad}")
    candidate = merge_patch(entity, patch)
    candidate["revision"] = int(entity.get("revision", 0)) + 1
    candidate["updatedAt"] = now_utc()
    validate_proposed_entity(root, entity, candidate)
    _, registry, _, _ = load_controls(root)
    save_json(entity_dest(root, registry, entity["entityType"], uid_value), candidate)
    return candidate


def plan_content_upsert(root: Path, uid_value: str, entity: Dict[str, Any], content: Dict[str, Any]) -> Dict[str, Any]:
    name = str(content.get("name") or "").strip()
    if not name:
        raise ValueError("content-upsert content.name is required")
    body = content.get("body")
    if not isinstance(body, str):
        raise ValueError("content-upsert content.body must be a string")
    mode = str(content.get("mode") or "inline")
    fmt = str(content.get("format") or "text")
    current_items = copy.deepcopy(entity.get("content") or [])
    existing = next((x for x in current_items if x.get("name") == name), None)
    old_path = str(existing.get("path")) if existing and existing.get("mode") == "file" and existing.get("path") else None
    if mode == "inline":
        new_item = {"name": name, "format": fmt, "mode": "inline", "body": body}
    elif mode == "file":
        rel = content.get("path") or (existing or {}).get("path")
        prefix = f"files/{entity['entityType']}/{uid_value}/"
        if not rel:
            filename = str(content.get("filename") or f"{name}.txt")
            if Path(filename).name != filename or filename in {"", ".", ".."}:
                raise ValueError("content-upsert filename must be a plain file name")
            rel = prefix + filename
        rel = str(rel).replace("\\", "/")
        if not rel.startswith(prefix):
            raise ValueError(f"content-upsert file path must stay under {prefix}")
        target = project_path(root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        new_item = {"name": name, "format": fmt, "mode": "file", "path": rel}
    else:
        raise ValueError(f"Unsupported content mode: {mode}")
    replaced = False
    new_items = []
    for item in current_items:
        if item.get("name") == name:
            new_items.append(new_item)
            replaced = True
        else:
            new_items.append(item)
    if not replaced:
        new_items.append(new_item)
    entity = copy.deepcopy(entity)
    entity["content"] = new_items
    entity["revision"] = int(entity.get("revision", 0)) + 1
    entity["updatedAt"] = now_utc()
    _, registry, _, _ = load_controls(root)
    errors = validate_entity_definition(root, registry, entity["entityType"], entity)
    if errors:
        raise ValueError("content-upsert makes entity invalid:\n- " + "\n- ".join(errors))
    save_json(entity_dest(root, registry, entity["entityType"], uid_value), entity)
    new_path = new_item.get("path") if new_item.get("mode") == "file" else None
    if old_path and old_path != new_path and old_path.startswith("files/"):
        old = project_path(root, old_path)
        if old.is_file():
            old.unlink()
    return entity


def plan_link_entities(root: Path, source_uid: str, target_uid: str, relation: str, unlink: bool = False) -> None:
    _, registry, relation_map, _ = load_controls(root)
    entities, paths = collect_entities(root, registry)
    source = entities.get(source_uid)
    target = entities.get(target_uid)
    if not source or not target:
        raise KeyError("Plan link/unlink source or target entity not found")
    existing = source.get("relations", [])
    if unlink:
        new_rel = [r for r in existing if not (r.get("type") == relation and r.get("targetUid") == target_uid)]
        if len(new_rel) == len(existing):
            return
        source["relations"] = new_rel
    else:
        mappings = [m for m in relation_map.get("mappings", []) if matches_mapping(source["entityType"], relation, target["entityType"], m)]
        if not mappings:
            raise ValueError(f"Relation not allowed: {source['entityType']} --{relation}--> {target['entityType']}")
        if any(r.get("type") == relation and r.get("targetUid") == target_uid for r in existing):
            return
        for m in mappings:
            if m.get("cardinality") in {"many-to-one", "one-to-one"}:
                current = [r for r in existing if matches_mapping(source["entityType"], r.get("type", ""), r.get("targetType", ""), m)]
                if current:
                    raise ValueError(f"Mapping {m.get('key')} allows at most one target from the source side")
            if m.get("cardinality") in {"one-to-one", "one-to-many"}:
                for other_uid, other in entities.items():
                    if other_uid == source_uid:
                        continue
                    for r in other.get("relations", []):
                        if r.get("targetUid") == target_uid and matches_mapping(other["entityType"], r.get("type", ""), r.get("targetType", ""), m):
                            raise ValueError(f"Mapping {m.get('key')} allows at most one incoming source for target {target_uid}")
        source.setdefault("relations", []).append({"type": relation, "targetUid": target_uid, "targetType": target["entityType"], "metadata": {}})
    source["revision"] = int(source.get("revision", 0)) + 1
    source["updatedAt"] = now_utc()
    save_json(paths[source_uid], source)


def plan_transition_entity(root: Path, uid_value: str, entity: Dict[str, Any], status: str, force: bool = False) -> None:
    _, registry, _, _ = load_controls(root)
    lifecycle = (registry.get("types", {}).get(entity["entityType"], {}).get("lifecycle") or {})
    statuses = lifecycle.get("statuses") or {}
    current = str(entity.get("status"))
    if status not in statuses:
        raise ValueError(f"Status {status!r} is not configured for {entity['entityType']}")
    if status == current:
        return
    allowed = (lifecycle.get("transitions") or {}).get(current, [])
    if not force and status not in allowed:
        raise ValueError(f"Transition {current!r} -> {status!r} is not allowed. Allowed: {allowed}")
    entity = copy.deepcopy(entity)
    entity["status"] = status
    entity["revision"] = int(entity.get("revision", 0)) + 1
    entity["updatedAt"] = now_utc()
    save_json(entity_dest(root, registry, entity["entityType"], uid_value), entity)


def execute_plan_step(root: Path, step: Dict[str, Any], outputs: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    op = str(step.get("operation"))
    sid = str(step.get("stepId"))
    result: Dict[str, Any] = {"stepId": sid, "operation": op, "status": "completed", "at": now_utc()}
    if op == "create":
        uid_value, entity = plan_create_entity(root, step)
        outputs.setdefault("steps", {})[sid] = uid_value
        if step.get("handle"):
            outputs.setdefault("handles", {})[str(step.get("handle"))] = uid_value
        result.update({"uid": uid_value, "entityType": entity.get("entityType"), "localRef": entity.get("localRef")})
    elif op == "update":
        uid_value, entity = resolve_plan_runtime_ref(root, str(step.get("targetRef")), outputs)
        plan_update_entity(root, uid_value, entity, copy.deepcopy(step.get("patch") or {}))
        result["uid"] = uid_value
    elif op == "content-upsert":
        uid_value, entity = resolve_plan_runtime_ref(root, str(step.get("targetRef")), outputs)
        plan_content_upsert(root, uid_value, entity, copy.deepcopy(step.get("content") or {}))
        result["uid"] = uid_value
    elif op in {"link", "unlink"}:
        source_uid, _ = resolve_plan_runtime_ref(root, str(step.get("sourceRef")), outputs)
        target_uid, _ = resolve_plan_runtime_ref(root, str(step.get("targetRef")), outputs)
        plan_link_entities(root, source_uid, target_uid, str(step.get("relation")), unlink=(op == "unlink"))
        result.update({"sourceUid": source_uid, "targetUid": target_uid, "relation": step.get("relation")})
    elif op == "transition":
        uid_value, entity = resolve_plan_runtime_ref(root, str(step.get("targetRef")), outputs)
        plan_transition_entity(root, uid_value, entity, str(step.get("status")), bool(step.get("force")))
        result["uid"] = uid_value
    elif op in {"delete", "restore"}:
        uid_value, entity = resolve_plan_runtime_ref(root, str(step.get("targetRef")), outputs)
        _, registry, _, _ = load_controls(root)
        entity = copy.deepcopy(entity)
        if op == "delete":
            if not entity.get("isDeleted"):
                entity["isDeleted"] = True
                entity["deletedAt"] = now_utc()
                entity["revision"] = int(entity.get("revision", 0)) + 1
                entity["updatedAt"] = entity["deletedAt"]
                save_json(entity_dest(root, registry, entity["entityType"], uid_value), entity)
        else:
            if entity.get("isDeleted"):
                entity["isDeleted"] = False
                entity["deletedAt"] = None
                entity["revision"] = int(entity.get("revision", 0)) + 1
                entity["updatedAt"] = now_utc()
                save_json(entity_dest(root, registry, entity["entityType"], uid_value), entity)
        result["uid"] = uid_value
    else:
        raise ValueError(f"Unsupported WorkPlan operation: {op}")
    return result


def rollback_plan_execution(root: Path, before: Dict[str, Dict[str, Any]]) -> None:
    current = capture_workspace_records(root)
    changed_uids = sorted(set(before) | set(current))
    for uid_value in reversed(changed_uids):
        if record_hash(before.get(uid_value)) == record_hash(current.get(uid_value)):
            continue
        apply_entity_record(root, uid_value, before.get(uid_value), current.get(uid_value))
    build_manifest(root)
    update_audit_state(root, before)


def cmd_plan_execute(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    path, doc = find_workplan(root, args.plan)
    policy = load_agent_workflow(root)
    if policy.get("execution", {}).get("implementationOnlyViaImplementCommand", False) and not getattr(args, "via_implement", False):
        raise ValueError("Direct plan-execute is disabled by documentation-first policy. Use the implement command after documentation/review is complete.")
    planning = policy.get("planning", {})
    execution_policy = policy.get("execution", {})
    if planning.get("requireApprovalBeforeExecute", True) and doc.get("status") != "approved":
        raise ValueError(f"WorkPlan must be approved before execution; status={doc.get('status')}")
    if doc.get("status") not in {"approved", "draft"}:
        raise ValueError(f"WorkPlan cannot execute from status {doc.get('status')}")
    errors = validate_workplan_doc(root, doc, resolve_refs=True)
    if errors:
        raise ValueError("WorkPlan is invalid:\n- " + "\n- ".join(errors))
    stale = workplan_stale_findings(root, doc)
    if planning.get("protectAgainstStaleContext", True) and stale and not args.force_stale:
        raise ValueError("WorkPlan context is stale; refresh/review again or explicitly use --force-stale after review: " + json.dumps(stale, ensure_ascii=False))
    before = capture_workspace_records(root)
    before_quality = quality_report(root) if execution_policy.get("runQualityAfterPlan", True) else {"findings": []}
    doc["status"] = "executing"
    doc.setdefault("execution", {})["startedAt"] = now_utc()
    doc["execution"]["completedAt"] = None
    doc["execution"]["stepResults"] = []
    plan_event(doc, "execution-started", args.actor, args.reason)
    save_json(path, doc)
    outputs: Dict[str, Dict[str, str]] = {"steps": {}, "handles": {}}
    step_results: List[Dict[str, Any]] = []
    try:
        for step in ordered_plan_steps(doc.get("steps") or []):
            result = execute_plan_step(root, step, outputs)
            step_results.append(result)
            doc["execution"]["stepResults"] = step_results
            save_json(path, doc)
        if execution_policy.get("validateAfterPlan", True):
            validation_errors = validate_project(root)
            if validation_errors:
                raise ValueError("WorkPlan execution makes project invalid:\n- " + "\n- ".join(validation_errors))
        q_report = quality_report(root) if execution_policy.get("runQualityAfterPlan", True) else {"findings": []}
        threshold = str(execution_policy.get("blockAtQualitySeverity", "error"))
        severity_rank = {"info": 1, "warning": 2, "error": 3}
        threshold_rank = severity_rank.get(threshold, 99)
        blocking = [f for f in q_report.get("findings", []) if severity_rank.get(str(f.get("severity")), 0) >= threshold_rank]
        quality_mode = str(execution_policy.get("qualityMode", "no-new-blocking"))
        if quality_mode == "no-new-blocking":
            def _finding_key(item: Dict[str, Any]) -> Tuple[str, str, str, str]:
                return (str(item.get("ruleId") or ""), str(item.get("uid") or ""), str(item.get("kind") or ""), str(item.get("title") or ""))
            existing = {_finding_key(f) for f in before_quality.get("findings", []) if severity_rank.get(str(f.get("severity")), 0) >= threshold_rank}
            blocking = [f for f in blocking if _finding_key(f) not in existing]
        if blocking:
            raise ValueError(f"WorkPlan execution introduced {len(blocking)} blocking quality finding(s) at severity {threshold} or higher")
        build_manifest(root)
        after = capture_workspace_records(root)
        changes = make_changes(before, after)
        if not changes:
            raise ValueError("WorkPlan completed without making any project changes")
        related = list(dict.fromkeys([doc["planId"]] + list(doc.get("related") or [])))
        cs = write_change_set(
            root, changes, status="applied", source="workplan", actor=args.actor or (doc.get("actor") or {}).get("id"),
            reason=args.reason or doc.get("reason") or doc.get("request"), related=related,
            extra={"workPlanId": doc["planId"], "workPlanSteps": [r.get("stepId") for r in step_results]},
        )
        update_audit_state(root, after)
        doc["status"] = "completed"
        doc["execution"]["completedAt"] = now_utc()
        doc["execution"]["stepResults"] = step_results
        doc["execution"]["outputs"] = outputs
        doc["execution"]["changeSetId"] = (cs or {}).get("changeSetId")
        doc["execution"]["qualitySummary"] = q_report.get("summary", {})
        plan_event(doc, "execution-completed", args.actor, args.reason, changeSetId=(cs or {}).get("changeSetId"))
        save_json(path, doc)
        print(json.dumps({"planId": doc["planId"], "status": doc["status"], "changeSetId": (cs or {}).get("changeSetId"), "steps": len(step_results), "outputs": outputs}, ensure_ascii=False))
        return 0
    except Exception as exc:
        if execution_policy.get("atomic", True):
            try:
                rollback_plan_execution(root, before)
            except Exception as rollback_exc:
                doc.setdefault("execution", {})["rollbackError"] = str(rollback_exc)
        doc["status"] = "failed"
        doc.setdefault("execution", {})["completedAt"] = now_utc()
        doc["execution"]["stepResults"] = step_results
        doc["execution"]["error"] = str(exc)
        plan_event(doc, "execution-failed", args.actor, str(exc))
        save_json(path, doc)
        raise


def workplan_sync_payload(root: Path, sync_state: Dict[str, Any], plan_filter: Optional[set] = None, force_local: bool = False) -> Dict[str, Any]:
    policy = load_agent_workflow(root)
    if not policy.get("sync", {}).get("enabled", True):
        return {"workPlans": []}
    state_map = sync_state.get("workPlans", {})
    result = []
    for _, doc in load_workplans(root):
        plan_id = str(doc.get("planId") or "")
        if plan_filter and plan_id not in plan_filter:
            continue
        digest = workplan_hash(doc)
        state = state_map.get(plan_id) or {}
        if state.get("lastSyncedHash") == digest:
            continue
        result.append({
            "planId": plan_id,
            "baseRemoteVersion": state.get("remoteVersion"),
            "baseRemoteUpdatedAt": state.get("remoteUpdatedAt"),
            "payloadHash": digest,
            "conflictToken": state.get("conflictToken") if force_local else None,
            "document": doc,
        })
    return {"workPlans": result}


def write_workplan_conflict(root: Path, config: Dict[str, Any], sync_id: str, plan_id: str, local_doc: Optional[Dict[str, Any]], remote_item: Dict[str, Any]) -> Path:
    folder = project_path(root, config["storage"]["conflictFolder"]) / "__workplans__" / safe_plan_id(plan_id) / sync_id
    folder.mkdir(parents=True, exist_ok=True)
    if local_doc is not None:
        save_json(folder / "local.json", local_doc)
    save_json(folder / "remote.json", remote_item)
    return folder


def apply_workplan_sync(root: Path, config: Dict[str, Any], sync_state: Dict[str, Any], response: Dict[str, Any], sent: Dict[str, Any], sync_id: str, force_local: bool) -> List[str]:
    errors: List[str] = []
    state_map = sync_state.setdefault("workPlans", {})
    sent_map = {x.get("planId"): x for x in sent.get("workPlans", [])}
    for item in response.get("workPlansApplied", []) or []:
        data = {"planId": item} if isinstance(item, str) else item
        plan_id = str(data.get("planId") or "")
        if not plan_id or plan_id not in sent_map:
            continue
        state = state_map.setdefault(plan_id, {})
        state["lastSyncedHash"] = data.get("payloadHash") or sent_map[plan_id].get("payloadHash")
        state["remoteId"] = data.get("remoteId", state.get("remoteId"))
        state["remoteVersion"] = data.get("remoteVersion", state.get("remoteVersion"))
        state["remoteUpdatedAt"] = data.get("remoteUpdatedAt", state.get("remoteUpdatedAt"))
        state["lastSyncedAt"] = response.get("serverTime") or now_utc()
        state.pop("conflict", None)
        state.pop("conflictToken", None)
    for item in response.get("remoteWorkPlans", []) or []:
        plan_id = str(item.get("planId") or "")
        remote_doc = item.get("document")
        if not plan_id or not isinstance(remote_doc, dict):
            errors.append(f"invalid remote WorkPlan: {item}")
            continue
        try:
            safe_plan_id(plan_id)
            if str(remote_doc.get("planId") or "") != plan_id:
                raise ValueError("remote WorkPlan planId mismatch")
            state = state_map.setdefault(plan_id, {})
            path = workplan_path(root, plan_id, config)
            local_doc = load_json(path) if path.is_file() else None
            local_hash = workplan_hash(local_doc) if local_doc else None
            local_dirty = local_doc is not None and (state.get("lastSyncedHash") is None or local_hash != state.get("lastSyncedHash"))
            if local_dirty and not force_local:
                folder = write_workplan_conflict(root, config, sync_id, plan_id, local_doc, item)
                state["conflict"] = folder.relative_to(root).as_posix()
                state["conflictToken"] = item.get("conflictToken")
                continue
            if force_local:
                continue
            basic_errors = validate_workplan_storage_doc(remote_doc)
            if basic_errors:
                raise ValueError("remote WorkPlan invalid: " + "; ".join(basic_errors[:10]))
            save_json(path, remote_doc)
            state["lastSyncedHash"] = item.get("payloadHash") or workplan_hash(remote_doc)
            state["remoteVersion"] = item.get("remoteVersion")
            state["remoteUpdatedAt"] = item.get("remoteUpdatedAt")
            state["lastSyncedAt"] = response.get("serverTime") or now_utc()
            state.pop("conflict", None)
            state.pop("conflictToken", None)
        except Exception as exc:
            errors.append(f"remote WorkPlan {plan_id}: {exc}")
    for conflict in response.get("workPlanConflicts", []) or []:
        plan_id = str(conflict.get("planId") or "")
        if not plan_id:
            continue
        try:
            path = workplan_path(root, plan_id, config)
            local_doc = load_json(path) if path.is_file() else None
            folder = write_workplan_conflict(root, config, sync_id, plan_id, local_doc, conflict)
            state = state_map.setdefault(plan_id, {})
            state["conflict"] = folder.relative_to(root).as_posix()
            state["conflictToken"] = conflict.get("conflictToken")
        except Exception as exc:
            errors.append(f"WorkPlan conflict {plan_id}: {exc}")
    return errors



def audit_sync_payload(root: Path, sync_state: Dict[str, Any]) -> Dict[str, Any]:
    config, _, _, _ = load_controls(root)
    paths = change_paths(root, config)
    result = {"changeSets": [], "baselines": []}
    cs_state = sync_state.get("changeSets", {})
    if paths["changesets"].exists():
        for path in sorted(paths["changesets"].glob("*.json")):
            doc = load_json(path)
            change_set_id = str(doc.get("changeSetId") or path.stem)
            digest = sha256_bytes(canonical_json_bytes(doc))
            if (cs_state.get(change_set_id) or {}).get("lastSyncedHash") == digest:
                continue
            result["changeSets"].append({"changeSetId": change_set_id, "payloadHash": digest, "document": doc})
    base_state = sync_state.get("baselines", {})
    if paths["baselines"].exists():
        for path in sorted(paths["baselines"].glob("*.json")):
            doc = load_json(path)
            baseline_id = str(doc.get("baselineId") or path.stem)
            digest = sha256_bytes(canonical_json_bytes(doc))
            if (base_state.get(baseline_id) or {}).get("lastSyncedHash") == digest:
                continue
            result["baselines"].append({"baselineId": baseline_id, "name": doc.get("name"), "payloadHash": digest, "document": doc})
    return result


def apply_audit_sync_ack(sync_state: Dict[str, Any], response: Dict[str, Any], sent: Dict[str, Any]) -> None:
    sent_changes = {x.get("changeSetId"): x for x in sent.get("changeSets", [])}
    sent_baselines = {x.get("baselineId"): x for x in sent.get("baselines", [])}
    cs_state = sync_state.setdefault("changeSets", {})
    for item in response.get("auditApplied", []) or response.get("changeSetsApplied", []) or []:
        data = {"changeSetId": item} if isinstance(item, str) else item
        key = str(data.get("changeSetId") or "")
        if not key or key not in sent_changes:
            continue
        state = cs_state.setdefault(key, {})
        state["lastSyncedHash"] = data.get("payloadHash") or sent_changes[key].get("payloadHash")
        state["remoteId"] = data.get("remoteId", state.get("remoteId"))
        state["remoteVersion"] = data.get("remoteVersion", state.get("remoteVersion"))
        state["lastSyncedAt"] = response.get("serverTime") or now_utc()
    base_state = sync_state.setdefault("baselines", {})
    for item in response.get("baselinesApplied", []) or []:
        data = {"baselineId": item} if isinstance(item, str) else item
        key = str(data.get("baselineId") or "")
        if not key or key not in sent_baselines:
            continue
        state = base_state.setdefault(key, {})
        state["lastSyncedHash"] = data.get("payloadHash") or sent_baselines[key].get("payloadHash")
        state["remoteId"] = data.get("remoteId", state.get("remoteId"))
        state["remoteVersion"] = data.get("remoteVersion", state.get("remoteVersion"))
        state["lastSyncedAt"] = response.get("serverTime") or now_utc()


def cmd_sync(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    project = require_project(root)
    config, registry, _, sync_state = load_controls(root)

    errors = validate_project(root)
    if errors:
        print("Sync blocked because validation failed:")
        for item in errors:
            print(f"- {item}")
        return 1

    q_report = quality_report(root)
    threshold = str(config.get("validation", {}).get("qualityRulesBlockSyncAtSeverity", "error"))
    severity_rank = {"info": 1, "warning": 2, "error": 3}
    threshold_rank = severity_rank.get(threshold, 99)
    blocking_findings = [f for f in q_report.get("findings", []) if severity_rank.get(str(f.get("severity")), 0) >= threshold_rank]
    if blocking_findings:
        print(f"Sync blocked by {len(blocking_findings)} quality finding(s) at severity {threshold} or higher:")
        for item in blocking_findings:
            print(f"- {item.get('ruleId')}: {item.get('title')} ({item.get('uid')})")
        return 1

    manifest = build_manifest(root)
    local_definition = workspace_definition(root, config, registry)
    definition_state = sync_state.get("workspaceDefinition", {})
    include_definition = bool(config.get("sync", {}).get("includeWorkspaceDefinition", True)) and not args.skip_definition
    definition_change = None
    if include_definition and local_definition.get("payloadHash") != definition_state.get("lastSyncedHash"):
        definition_change = {
            "baseRemoteVersion": definition_state.get("remoteVersion"),
            "baseRemoteUpdatedAt": definition_state.get("remoteUpdatedAt"),
            "payloadHash": local_definition.get("payloadHash"),
            "conflictToken": definition_state.get("conflictToken") if args.force_local else None,
            "files": local_definition.get("files", []),
        }
    entity_filter = set(args.entity_type or [])
    uid_filter = set(args.uid or [])
    plan_filter = set(args.work_plan or [])
    current_entries = {e["uid"]: e for e in manifest["entities"]}
    entities, paths = collect_entities(root, registry)
    changes = []

    for uid_value, entry in current_entries.items():
        if entity_filter and entry.get("entityType") not in entity_filter:
            continue
        if uid_filter and uid_value not in uid_filter:
            continue
        state = sync_state.get("entities", {}).get(uid_value)
        if state and state.get("lastSyncedHash") == entry.get("payloadHash"):
            continue
        entity = entities[uid_value]
        changes.append(make_change(root, entity, state, args.force_local, config["storage"]["conflictFolder"]))

    audit_payload = audit_sync_payload(root, sync_state)
    planning_payload = workplan_sync_payload(root, sync_state, plan_filter=plan_filter, force_local=args.force_local)

    request_body = {
        "protocolVersion": config.get("protocolVersion", "1.0"),
        "project": {"uid": project.get("uid"), "id": project.get("id"), "code": project.get("code")},
        "client": {"instanceId": sync_state.get("clientInstanceId"), "cursor": sync_state.get("cursor")},
        "scope": {"entityTypes": sorted(entity_filter), "entityUids": sorted(uid_filter), "workPlanIds": sorted(plan_filter)},
        "options": {
            "direction": args.direction or config.get("sync", {}).get("defaultDirection", "bidirectional"),
            "forceLocal": bool(args.force_local),
            "includeDeleted": bool(config.get("sync", {}).get("includeDeleted", True)),
        },
        "workspaceDefinition": definition_change,
        "changes": changes,
        "audit": audit_payload,
        "planning": planning_payload,
    }

    if args.dry_run:
        print(json.dumps(request_body, indent=2, ensure_ascii=False))
        return 0

    base_url = config.get("sync", {}).get("baseUrl", "").rstrip("/")
    endpoint = config.get("sync", {}).get("endpoint", "/api/project-manager/sync")
    if not base_url:
        raise RuntimeError(".pm/config.json sync.baseUrl is empty")
    url = base_url + "/" + endpoint.lstrip("/")
    token_env = config.get("sync", {}).get("tokenEnv", "PM_API_TOKEN")
    token = os.environ.get(token_env) if token_env else None
    body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    timeout = int(config.get("sync", {}).get("timeoutSeconds", 30))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Sync HTTP {exc.code}: {detail}") from exc

    sync_id = str(response.get("syncId") or uuid.uuid4())
    sync_entities = sync_state.setdefault("entities", {})
    processing_errors: List[str] = []

    # Workspace-definition bookkeeping and pull. Definitions use the same optimistic concurrency idea as entities.
    applied_definition = response.get("definitionApplied") or response.get("workspaceDefinitionApplied")
    if isinstance(applied_definition, dict):
        state = sync_state.setdefault("workspaceDefinition", {})
        state["remoteVersion"] = applied_definition.get("remoteVersion")
        state["remoteUpdatedAt"] = applied_definition.get("remoteUpdatedAt")
        state["lastSyncedAt"] = response.get("serverTime") or now_utc()
        state["lastSyncedHash"] = applied_definition.get("payloadHash") or local_definition.get("payloadHash")
        state.pop("conflict", None)
        state.pop("conflictToken", None)

    remote_definition = response.get("remoteDefinition")
    if isinstance(remote_definition, dict) and remote_definition.get("files"):
        state = sync_state.setdefault("workspaceDefinition", {})
        current_definition = workspace_definition(root, config, registry)
        local_dirty = state.get("lastSyncedHash") is None or current_definition.get("payloadHash") != state.get("lastSyncedHash")
        if local_dirty and not args.force_local:
            folder = write_definition_conflict(root, config, sync_id, current_definition, remote_definition)
            state["conflict"] = folder.relative_to(root).as_posix()
            state["conflictToken"] = remote_definition.get("conflictToken")
        elif not args.force_local:
            previous_definition = current_definition
            try:
                apply_workspace_definition(root, remote_definition)
                # Reload and validate before accepting the remote definition.
                config, registry, _, _ = load_controls(root)
                definition_errors = validate_project(root)
                if definition_errors:
                    raise ValueError("remote definition makes the project invalid: " + "; ".join(definition_errors[:10]))
                updated_definition = workspace_definition(root, config, registry)
                state["remoteVersion"] = remote_definition.get("remoteVersion")
                state["remoteUpdatedAt"] = remote_definition.get("remoteUpdatedAt")
                state["lastSyncedAt"] = response.get("serverTime") or now_utc()
                state["lastSyncedHash"] = remote_definition.get("payloadHash") or updated_definition.get("payloadHash")
                state.pop("conflict", None)
                state.pop("conflictToken", None)
            except Exception as exc:
                try:
                    apply_workspace_definition(root, previous_definition)
                    config, registry, _, _ = load_controls(root)
                except Exception as rollback_exc:
                    processing_errors.append(f"workspace definition rollback: {rollback_exc}")
                processing_errors.append(f"workspace definition: {exc}")

    # Apply official project identity.
    server_project = response.get("project") or {}
    if server_project:
        locked = sync_state.get("projectServerIdentity")
        try:
            if locked:
                if server_project.get("id") is not None and locked.get("id") != server_project.get("id"):
                    raise ValueError("Project server id changed after lock")
                if server_project.get("code") is not None and locked.get("code") != server_project.get("code"):
                    raise ValueError("Project server code changed after lock")
                project["id"] = locked.get("id")
                project["code"] = locked.get("code")
            else:
                if server_project.get("id") is not None:
                    project["id"] = server_project.get("id")
                if server_project.get("code") is not None:
                    project["code"] = server_project.get("code")
                if project.get("id") is not None or project.get("code") is not None:
                    sync_state["projectServerIdentity"] = {"id": project.get("id"), "code": project.get("code")}
            save_json(root / "project.json", project)
        except Exception as exc:
            processing_errors.append(f"project identity: {exc}")

    # Applied local pushes / identity mappings.
    for item in response.get("applied", []):
        uid_value = str(item.get("uid", ""))
        entity = entities.get(uid_value)
        path = paths.get(uid_value)
        if not entity or not path:
            processing_errors.append(f"applied uid not found locally: {uid_value}")
            continue
        state = sync_entities.setdefault(uid_value, {})
        try:
            apply_server_identity(entity, item.get("id"), item.get("code"), state.get("serverIdentity"))
            if entity.get("id") is not None or entity.get("code") is not None:
                state["serverIdentity"] = {"id": entity.get("id"), "code": entity.get("code")}
            save_json(path, entity)
            state["remoteVersion"] = item.get("remoteVersion")
            state["remoteUpdatedAt"] = item.get("remoteUpdatedAt")
            state["lastSyncedAt"] = response.get("serverTime") or now_utc()
            state["lastSyncedHash"] = item.get("payloadHash") or payload_hash(root, entity)
            state.pop("conflict", None)
        except Exception as exc:
            processing_errors.append(f"applied {uid_value}: {exc}")

    # Pull remote changes. Only auto-apply if local is clean relative to last synced hash.
    for item in response.get("remoteChanges", []):
        uid_value = str(item.get("uid", ""))
        entity_type = str(item.get("entityType", ""))
        remote_entity = item.get("entity")
        if not uid_value or not entity_type or not isinstance(remote_entity, dict):
            processing_errors.append(f"invalid remote change: {item}")
            continue
        state = sync_entities.get(uid_value, {})
        local_entity = entities.get(uid_value)
        local_dirty = False
        if local_entity is not None:
            last_hash = state.get("lastSyncedHash")
            current_hash = payload_hash(root, local_entity)
            local_dirty = last_hash is None or current_hash != last_hash
        if local_entity is not None and local_dirty:
            conflict = {
                "entityType": entity_type,
                "uid": uid_value,
                "reason": "local_and_remote_modified",
                "localUpdatedAt": local_entity.get("updatedAt"),
                "remoteUpdatedAt": item.get("remoteUpdatedAt"),
                "baseRemoteVersion": state.get("remoteVersion"),
                "remoteVersion": item.get("remoteVersion"),
                "conflictToken": item.get("conflictToken"),
                "remoteEntity": remote_entity,
                "remoteFiles": item.get("files", []),
            }
            folder = write_conflict(root, config, sync_id, uid_value, conflict, local_entity)
            state["conflict"] = folder.relative_to(root).as_posix()
            sync_entities[uid_value] = state
            continue
        try:
            if str(remote_entity.get("uid")) != uid_value:
                raise ValueError("remote entity uid mismatch")
            if remote_entity.get("entityType") != entity_type:
                raise ValueError("remote entityType mismatch")
            locked = state.get("serverIdentity")
            apply_server_identity(remote_entity, item.get("id", remote_entity.get("id")), item.get("code", remote_entity.get("code")), locked)
            dest = entity_dest(root, registry, entity_type, uid_value)
            save_json(dest, remote_entity)
            apply_remote_files(root, item.get("files", []))
            new_state = sync_entities.setdefault(uid_value, {})
            if remote_entity.get("id") is not None or remote_entity.get("code") is not None:
                new_state["serverIdentity"] = {"id": remote_entity.get("id"), "code": remote_entity.get("code")}
            new_state["remoteVersion"] = item.get("remoteVersion")
            new_state["remoteUpdatedAt"] = item.get("remoteUpdatedAt")
            new_state["lastSyncedAt"] = response.get("serverTime") or now_utc()
            new_state["lastSyncedHash"] = item.get("payloadHash") or payload_hash(root, remote_entity)
            new_state.pop("conflict", None)
        except Exception as exc:
            processing_errors.append(f"remote {uid_value}: {exc}")

    # Explicit conflicts returned by the server.
    for conflict in response.get("conflicts", []):
        uid_value = str(conflict.get("uid", ""))
        local_entity = entities.get(uid_value)
        folder = write_conflict(root, config, sync_id, uid_value or "unknown", conflict, local_entity)
        if uid_value:
            state = sync_entities.setdefault(uid_value, {})
            state["conflict"] = folder.relative_to(root).as_posix()

    apply_audit_sync_ack(sync_state, response, audit_payload)
    processing_errors.extend(apply_workplan_sync(root, config, sync_state, response, planning_payload, sync_id, bool(args.force_local)))

    response_errors = response.get("errors", []) or []
    if not processing_errors and not response_errors:
        sync_state["cursor"] = response.get("nextCursor", sync_state.get("cursor"))
        sync_state["lastSyncAt"] = response.get("serverTime") or now_utc()
    save_json(project_path(root, config["storage"]["syncState"]), sync_state)
    build_manifest(root)

    print(f"Sync {sync_id}: {len(response.get('applied', []))} entities applied, {len(response.get('remoteChanges', []))} remote entities, {len(response.get('conflicts', []))} entity conflicts, {len(response.get('workPlansApplied', []) or [])} workplans applied, {len(response.get('remoteWorkPlans', []) or [])} remote workplans")
    if processing_errors or response_errors:
        for item in processing_errors:
            print(f"ERROR: {item}")
        for item in response_errors:
            print(f"ERROR: {item}")
        return 1
    return 0



def export_bundle(root: Path, output: Path) -> Dict[str, Any]:
    project = require_project(root)
    config, registry, relation_map, _ = load_controls(root)
    definition = workspace_definition(root, config, registry)
    entities = []
    for expected_type, path in iter_entity_files(root, registry):
        entity = load_json(path)
        item = {
            "entityType": expected_type,
            "entity": entity,
            "files": content_file_entries(root, entity),
        }
        entities.append(item)
    bundle = {
        "bundleVersion": "1.0",
        "exportedAt": now_utc(),
        "project": project,
        "workspaceDefinition": definition,
        "entities": entities,
        "changeManagement": bundle_change_files(root),
        "agentWorkflow": bundle_workplan_files(root),
        "requestTrace": bundle_request_files(root),
    }
    bundle["bundleHash"] = sha256_bytes(canonical_json_bytes({k:v for k,v in bundle.items() if k != "bundleHash"}))
    save_json(output, bundle)
    return bundle


def import_bundle(bundle_path: Path, root: Path, force: bool = False) -> Dict[str, Any]:
    bundle = load_json(bundle_path)
    if bundle.get("bundleVersion") != "1.0":
        raise ValueError(f"Unsupported bundleVersion: {bundle.get('bundleVersion')!r}")
    expected_hash = bundle.get("bundleHash")
    if expected_hash:
        actual_hash = sha256_bytes(canonical_json_bytes({k:v for k,v in bundle.items() if k != "bundleHash"}))
        if actual_hash != expected_hash:
            raise ValueError("Bundle hash mismatch; file may be corrupted or manually altered")
    if root.exists() and any(root.iterdir()) and not force:
        raise RuntimeError(f"Target folder is not empty: {root}. Use --force to overwrite bundle-owned files.")
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE_ROOT, root, dirs_exist_ok=True)
    project = bundle.get("project")
    if not isinstance(project, dict):
        raise ValueError("Bundle project object is missing")
    save_json(root / "project.json", project)
    definition = bundle.get("workspaceDefinition") or {}
    apply_workspace_definition(root, definition)
    config, registry, _, _ = load_controls(root)
    imported = 0
    for item in bundle.get("entities", []):
        entity = item.get("entity")
        if not isinstance(entity, dict):
            raise ValueError("Bundle entity item is missing entity object")
        entity_type = str(entity.get("entityType", ""))
        uid_value = str(entity.get("uid", ""))
        if not entity_type or not uid_value:
            raise ValueError("Bundle entity is missing entityType or uid")
        dest = entity_dest(root, registry, entity_type, uid_value)
        save_json(dest, entity)
        for f in item.get("files", []):
            rel = str(f.get("path", ""))
            if not rel:
                continue
            target = project_path(root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            if f.get("encoding") == "utf-8":
                target.write_text(f.get("content") or "", encoding="utf-8")
            else:
                raise ValueError(f"Binary bundle content is not supported for import: {rel}")
        imported += 1
    restore_bundle_change_files(root, bundle.get("changeManagement", []))
    restore_bundle_workplan_files(root, bundle.get("agentWorkflow", []))
    restore_bundle_request_files(root, bundle.get("requestTrace", []))
    errors = validate_project(root)
    if errors:
        raise ValueError("Imported bundle is invalid:\n- " + "\n- ".join(errors))
    build_manifest(root)
    return {"projectUid": project.get("uid"), "entitiesImported": imported, "path": str(root)}


def cmd_bundle_export(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    output = Path(args.output).resolve()
    bundle = export_bundle(root, output)
    print(json.dumps({"output": str(output), "entities": len(bundle.get("entities", [])), "bundleHash": bundle.get("bundleHash")}, ensure_ascii=False))
    return 0


def cmd_bundle_import(args: argparse.Namespace) -> int:
    result = import_bundle(Path(args.bundle).resolve(), Path(args.path).resolve(), force=args.force)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def load_source_intelligence(root: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if config is None:
        config, _, _, _ = load_controls(root)
    rel = config.get("storage", {}).get("sourceIntelligence", ".pm/source-intelligence.json")
    path = project_path(root, rel)
    if not path.is_file():
        return {
            "schemaVersion": "1.0",
            "scan": {
                "roots": ["src", "lib", "app", "apps", "packages", "server", "client", "backend", "frontend", "api", "test", "tests", "e2e", "playwright", "database", "db", "sql", "docs"],
                "includeExtensions": [".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".dart", ".java", ".kt", ".kts", ".go", ".rs", ".sql", ".graphql", ".gql", ".json", ".yaml", ".yml", ".md", ".html", ".css", ".scss"],
                "includeNames": ["Dockerfile", "Makefile"],
                "ignoreGlobs": [".git/**", ".pm/**", "entities/**", "schemas/**", "node_modules/**", "dist/**", "build/**", ".dart_tool/**", ".next/**", "coverage/**", "vendor/**", "bin/**", "obj/**"],
                "maxFileBytes": 1048576,
            },
            "mapping": {"scanEntityCodes": True, "scanLocalRefs": False, "explicit": []},
            "dependency": {
                "aliases": [],
                "maxDepth": 8,
                "testGlobs": ["**/*.spec.ts", "**/*.spec.tsx", "**/*.spec.js", "**/*.spec.jsx", "**/*.test.ts", "**/*.test.tsx", "**/*.test.js", "**/*.test.jsx", "test/**/*.dart", "tests/**/*.py", "test_*.py", "*_test.py"],
            },
        }
    return load_json(path)


def source_index_path(root: Path, config: Optional[Dict[str, Any]] = None) -> Path:
    if config is None:
        config, _, _, _ = load_controls(root)
    return project_path(root, config.get("storage", {}).get("sourceIndex", ".pm/indexes/source-index.json"))


def dependency_index_path(root: Path, config: Optional[Dict[str, Any]] = None) -> Path:
    if config is None:
        config, _, _, _ = load_controls(root)
    return project_path(root, config.get("storage", {}).get("dependencyIndex", ".pm/indexes/dependency-index.json"))


def source_language(path: Path) -> str:
    suffix = path.suffix.lower()
    mapping = {
        ".py": "python", ".js": "javascript", ".jsx": "javascript-react", ".ts": "typescript", ".tsx": "typescript-react",
        ".cs": "csharp", ".dart": "dart", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".go": "go", ".rs": "rust",
        ".sql": "sql", ".graphql": "graphql", ".gql": "graphql", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
        ".md": "markdown", ".html": "html", ".css": "css", ".scss": "scss",
    }
    if path.name == "Dockerfile": return "dockerfile"
    if path.name == "Makefile": return "makefile"
    return mapping.get(suffix, suffix.lstrip(".") or "text")


def source_symbols(text: str, language: str, limit: int = 200) -> List[Dict[str, Any]]:
    patterns: List[Tuple[str, str]] = []
    if language == "python":
        patterns = [("class", r"(?m)^\s*class\s+([A-Za-z_][\w]*)"), ("function", r"(?m)^\s*(?:async\s+)?def\s+([A-Za-z_][\w]*)\s*\(")]
    elif language in {"javascript", "javascript-react", "typescript", "typescript-react"}:
        patterns = [
            ("class", r"(?m)^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)"),
            ("function", r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
            ("function", r"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^\n]*\)\s*=>"),
        ]
    elif language == "csharp":
        patterns = [("type", r"(?m)^\s*(?:public|internal|private|protected|abstract|sealed|static|partial|\s)*\s*(?:class|interface|record|enum)\s+([A-Za-z_][\w]*)")]
    elif language == "dart":
        patterns = [("class", r"(?m)^\s*class\s+([A-Za-z_][\w]*)"), ("function", r"(?m)^\s*(?:Future<[^>]+>|Future|void|String|int|bool|double|dynamic|[A-Za-z_][\w<>?, ]*)\s+([A-Za-z_][\w]*)\s*\([^;{]*\)\s*(?:async\s*)?\{")]
    elif language == "sql":
        patterns = [("table", r"(?im)\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[\"`\[]?[A-Za-z_][\w$]*[\"`\]]?\.)?[\"`\[]?([A-Za-z_][\w$]*)"), ("function", r"(?im)\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:[\"`\[]?[A-Za-z_][\w$]*[\"`\]]?\.)?[\"`\[]?([A-Za-z_][\w$]*)")]
    elif language == "go":
        patterns = [("function", r"(?m)^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)\s*\("), ("type", r"(?m)^\s*type\s+([A-Za-z_][\w]*)\s+")]
    elif language == "rust":
        patterns = [("function", r"(?m)^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][\w]*)\s*\("), ("type", r"(?m)^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_][\w]*)")]
    found: List[Dict[str, Any]] = []
    seen = set()
    for kind, pattern in patterns:
        for m in re.finditer(pattern, text):
            name = m.group(1)
            key = (kind, name)
            if key in seen: continue
            seen.add(key)
            line = text.count("\n", 0, m.start()) + 1
            found.append({"kind": kind, "name": name, "line": line})
            if len(found) >= limit: return found
    return found


def source_imports(text: str, language: str, limit: int = 200) -> List[str]:
    values: List[str] = []
    patterns: List[str] = []
    if language == "python":
        patterns = [r"(?m)^\s*from\s+([\.\w]+)\s+import\s+", r"(?m)^\s*import\s+([\w.]+)"]
    elif language in {"javascript", "javascript-react", "typescript", "typescript-react"}:
        patterns = [r"(?m)\bfrom\s+['\"]([^'\"]+)['\"]", r"(?m)\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)", r"(?m)^\s*import\s+['\"]([^'\"]+)['\"]"]
    elif language == "dart":
        patterns = [r"(?m)^\s*(?:import|export)\s+['\"]([^'\"]+)['\"]"]
    elif language == "csharp":
        patterns = [r"(?m)^\s*using\s+([A-Za-z_][\w.]*)\s*;"]
    elif language in {"java", "kotlin"}:
        patterns = [r"(?m)^\s*import\s+([A-Za-z_][\w.]*)(?:\.\*)?\s*;?"]
    elif language == "go":
        patterns = [r"(?m)^\s*import\s+(?:[A-Za-z_][\w]*\s+)?\"([^\"]+)\"", r"(?ms)^\s*import\s*\((.*?)\)"]
    elif language == "rust":
        patterns = [r"(?m)^\s*(?:pub\s+)?mod\s+([A-Za-z_][\w]*)\s*;", r"(?m)^\s*use\s+([^;]+);"]
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            value = m.group(1)
            if language == "go" and "\n" in value:
                for q in re.finditer(r'\"([^\"]+)\"', value):
                    item = q.group(1)
                    if item not in values:
                        values.append(item)
                        if len(values) >= limit: return values
                continue
            value = value.strip()
            if value and value not in values:
                values.append(value)
                if len(values) >= limit: return values
    return values


def iter_scalar_strings(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from iter_scalar_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_scalar_strings(child)
    elif isinstance(value, str):
        yield value


def entity_path_evidence(root: Path, registry: Dict[str, Any], entities: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    by_path: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for uid_value, entity in entities.items():
        try:
            ep = entity_dest(root, registry, entity["entityType"], uid_value).relative_to(root).as_posix()
            by_path[ep].append({"uid": uid_value, "kind": "entity-json", "confidence": "exact"})
        except Exception:
            pass
        for item in entity.get("content", []):
            if item.get("mode") == "file" and item.get("path"):
                by_path[str(item["path"]).replace("\\", "/")].append({"uid": uid_value, "kind": "content-reference", "confidence": "exact"})
        for text in iter_scalar_strings(entity.get("data", {})):
            normalized = text.replace("\\", "/")
            if "/" in normalized or normalized.startswith("."):
                try:
                    candidate = project_path(root, normalized)
                    if candidate.is_file():
                        by_path[candidate.relative_to(root).as_posix()].append({"uid": uid_value, "kind": "data-path", "confidence": "exact"})
                except Exception:
                    pass
    return by_path


def source_scan_files(root: Path, roots_override: Optional[List[str]] = None) -> List[Path]:
    config, _, _, _ = load_controls(root)
    settings = (load_source_intelligence(root, config).get("scan") or {})
    roots = roots_override or settings.get("roots") or ["."]
    extensions = {str(x).lower() for x in (settings.get("includeExtensions") or [])}
    names = {str(x) for x in (settings.get("includeNames") or [])}
    ignores = [str(x) for x in (settings.get("ignoreGlobs") or [])]
    max_bytes = int(settings.get("maxFileBytes", 1048576))
    result: List[Path] = []
    seen = set()
    for rel_root in roots:
        try:
            base = project_path(root, str(rel_root))
        except Exception:
            continue
        if base.is_file(): candidates = [base]
        elif base.is_dir(): candidates = [p for p in base.rglob("*") if p.is_file()]
        else: continue
        for path in candidates:
            rel = path.relative_to(root).as_posix()
            if rel in seen: continue
            if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch("/" + rel, pat) for pat in ignores): continue
            if path.name not in names and path.suffix.lower() not in extensions: continue
            try:
                if path.stat().st_size > max_bytes: continue
            except OSError:
                continue
            seen.add(rel); result.append(path)
    return sorted(result, key=lambda x: x.relative_to(root).as_posix())


def source_fingerprint(root: Path, roots_override: Optional[List[str]] = None) -> str:
    config, _, _, _ = load_controls(root)
    source_cfg = load_source_intelligence(root, config)
    roots_used = roots_override or (source_cfg.get("scan") or {}).get("roots") or ["."]
    items = []
    for path in source_scan_files(root, roots_used):
        rel = path.relative_to(root).as_posix()
        try: digest = sha256_bytes(path.read_bytes())
        except OSError: continue
        items.append({"path": rel, "hash": digest})
    return sha256_bytes(canonical_json_bytes({"roots": roots_used, "config": source_cfg, "files": items}))


def build_source_index(root: Path, roots_override: Optional[List[str]] = None) -> Dict[str, Any]:
    config, registry, _, _ = load_controls(root)
    source_cfg = load_source_intelligence(root, config)
    mapping_cfg = source_cfg.get("mapping") or {}
    entities, _ = collect_entities(root, registry)
    path_evidence = entity_path_evidence(root, registry, entities)
    code_lookup = {str(e.get("code")): uid for uid, e in entities.items() if e.get("code")}
    local_lookup = {str(e.get("localRef")): uid for uid, e in entities.items() if e.get("localRef")}
    technical_identifiers: List[Tuple[str, str, str]] = []
    key_fields = mapping_cfg.get("scanKeyFields") or {"api": ["data.path", "data.operationId"], "database-table": ["data.tableName"], "screen": ["data.route"], "test-script": ["data.sourcePath"]}
    for uid_value, entity in entities.items():
        for field in key_fields.get(str(entity.get("entityType")), []):
            value = entity_field_value(entity, str(field))
            values = value if isinstance(value, list) else [value]
            for v in values:
                if isinstance(v, str) and len(v.strip()) >= 4:
                    technical_identifiers.append((v.strip(), uid_value, str(field)))
    explicit = mapping_cfg.get("explicit") or []
    files: Dict[str, Any] = {}
    by_entity: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    fp_items = []
    for path in source_scan_files(root, roots_override):
        rel = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        digest = sha256_bytes(raw)
        fp_items.append({"path": rel, "hash": digest})
        try: text = raw.decode("utf-8")
        except UnicodeDecodeError: continue
        lang = source_language(path)
        refs = list(path_evidence.get(rel, []))
        if mapping_cfg.get("scanEntityCodes", True):
            for code, uid_value in code_lookup.items():
                if code and re.search(r"(?<![\w-])" + re.escape(code) + r"(?![\w-])", text):
                    refs.append({"uid": uid_value, "kind": "code-mention", "value": code, "confidence": "medium"})
        if mapping_cfg.get("scanLocalRefs", False):
            for local_ref, uid_value in local_lookup.items():
                if local_ref and local_ref in text:
                    refs.append({"uid": uid_value, "kind": "local-ref-mention", "value": local_ref, "confidence": "medium"})
        for identifier, uid_value, field in technical_identifiers:
            if identifier in text:
                refs.append({"uid": uid_value, "kind": "technical-identifier", "field": field, "value": identifier, "confidence": "medium"})
        for item in explicit:
            pattern = str(item.get("path") or "")
            ref = str(item.get("entityRef") or "")
            if pattern and fnmatch.fnmatch(rel, pattern) and ref:
                try:
                    uid_value, _ = resolve_entity_ref(root, ref, entities, registry)
                    refs.append({"uid": uid_value, "kind": "explicit", "value": pattern, "confidence": "exact"})
                except Exception:
                    pass
        dedup = []
        seen_refs = set()
        for ref in refs:
            key = (ref.get("uid"), ref.get("kind"), ref.get("value"))
            if key in seen_refs: continue
            seen_refs.add(key); dedup.append(ref)
            by_entity[str(ref.get("uid"))].append({"path": rel, "kind": ref.get("kind"), "confidence": ref.get("confidence")})
        files[rel] = {
            "path": rel, "language": lang, "bytes": len(raw), "lines": text.count("\n") + (1 if text else 0),
            "hash": digest, "symbols": source_symbols(text, lang), "imports": source_imports(text, lang), "entityRefs": dedup,
        }
    roots_used = roots_override or (source_cfg.get("scan") or {}).get("roots") or ["."]
    fingerprint = sha256_bytes(canonical_json_bytes({"roots": roots_used, "config": source_cfg, "files": fp_items}))
    doc = {
        "schemaVersion": "1.0", "generatedAt": now_utc(), "roots": roots_used, "sourceFingerprint": fingerprint,
        "fileCount": len(files), "mappedEntityCount": len(by_entity), "files": files,
        "byEntity": {k: sorted(v, key=lambda x: x["path"]) for k, v in sorted(by_entity.items())},
    }
    save_json(source_index_path(root, config), doc)
    return doc


def ensure_source_index(root: Path, rebuild_if_stale: bool = True) -> Dict[str, Any]:
    config, _, _, _ = load_controls(root)
    path = source_index_path(root, config)
    if path.is_file():
        try:
            doc = load_json(path)
            roots_used = doc.get("roots") or None
            current = source_fingerprint(root, roots_used)
            if doc.get("sourceFingerprint") == current: return doc
            if not rebuild_if_stale:
                raise ValueError("Source index is missing or stale; run scan-source")
            return build_source_index(root, roots_override=roots_used)
        except ValueError:
            raise
        except Exception:
            pass
    if not rebuild_if_stale:
        raise ValueError("Source index is missing or stale; run scan-source")
    return build_source_index(root)


def map_paths_to_entities(root: Path, paths: List[str], rebuild_if_stale: bool = True) -> Dict[str, Any]:
    config, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    direct = entity_path_evidence(root, registry, entities)
    try: index = ensure_source_index(root, rebuild_if_stale=rebuild_if_stale)
    except Exception: index = {"files": {}}
    mapped: Dict[str, List[Dict[str, Any]]] = {}
    entity_evidence: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for raw_path in paths:
        rel = str(raw_path).replace("\\", "/").lstrip("./")
        evidence = list(direct.get(rel, []))
        for ref in (index.get("files", {}).get(rel) or {}).get("entityRefs", []): evidence.append(ref)
        dedup = []
        seen = set()
        for e in evidence:
            key = (e.get("uid"), e.get("kind"), e.get("value"))
            if key in seen: continue
            seen.add(key); dedup.append(e)
            if e.get("uid") in entities:
                entity_evidence[str(e["uid"])].append({"path": rel, **{k:v for k,v in e.items() if k != "uid"}})
        mapped[rel] = dedup
    return {
        "paths": mapped,
        "entities": [dict(entity_summary(entities[uid]), evidence=ev) for uid, ev in sorted(entity_evidence.items()) if uid in entities],
    }


def git_run(root: Path, args: List[str], check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(root)] + args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "git command failed").strip())
    return proc.stdout


def git_repo_root(root: Path) -> Path:
    value = git_run(root, ["rev-parse", "--show-toplevel"]).strip()
    if not value: raise RuntimeError("Project is not inside a Git repository")
    return Path(value).resolve()


def repo_path_to_project(root: Path, repo: Path, repo_rel: str) -> Optional[str]:
    candidate = (repo / repo_rel).resolve()
    try: return candidate.relative_to(root.resolve()).as_posix()
    except ValueError: return None


def parse_name_status(text: str, root: Path, repo: Path) -> List[Dict[str, Any]]:
    result = []
    for line in text.splitlines():
        if not line.strip(): continue
        parts = line.split("\t")
        status = parts[0]
        paths = parts[1:]
        if status.startswith("R") or status.startswith("C"):
            if len(paths) < 2: continue
            old_rel = repo_path_to_project(root, repo, paths[0]); new_rel = repo_path_to_project(root, repo, paths[1])
            if old_rel is None and new_rel is None: continue
            result.append({"status": status, "oldPath": old_rel, "path": new_rel})
        else:
            if not paths: continue
            rel = repo_path_to_project(root, repo, paths[0])
            if rel is None: continue
            result.append({"status": status, "path": rel})
    return result


def git_status_report(root: Path) -> Dict[str, Any]:
    repo = git_repo_root(root)
    branch = git_run(root, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    head = git_run(root, ["rev-parse", "HEAD"]).strip()
    raw = git_run(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    changes = []
    for line in raw.splitlines():
        if len(line) < 3: continue
        code = line[:2]
        value = line[3:]
        if " -> " in value:
            old, new = value.split(" -> ", 1)
            rel = repo_path_to_project(root, repo, new)
            old_rel = repo_path_to_project(root, repo, old)
            if rel is not None or old_rel is not None: changes.append({"status": code, "path": rel, "oldPath": old_rel})
        else:
            rel = repo_path_to_project(root, repo, value)
            if rel is not None: changes.append({"status": code, "path": rel})
    mapped = map_paths_to_entities(root, [c.get("path") or c.get("oldPath") for c in changes if c.get("path") or c.get("oldPath")])
    return {"schemaVersion": "1.0", "generatedAt": now_utc(), "mappingBasis": "current-workspace", "repoRoot": str(repo), "branch": branch, "head": head, "changeCount": len(changes), "changes": changes, "mappedEntities": mapped.get("entities", [])}


def git_range_changes(root: Path, from_ref: str, to_ref: str = "HEAD") -> Dict[str, Any]:
    repo = git_repo_root(root)
    raw = git_run(root, ["diff", "--name-status", from_ref, to_ref, "--"])
    changes = parse_name_status(raw, root, repo)
    mapped = map_paths_to_entities(root, [c.get("path") or c.get("oldPath") for c in changes if c.get("path") or c.get("oldPath")])
    return {"schemaVersion": "1.0", "generatedAt": now_utc(), "mappingBasis": "current-workspace", "from": from_ref, "to": to_ref, "changeCount": len(changes), "changes": changes, "mappedEntities": mapped.get("entities", [])}


def git_commit_report(root: Path, commit: str) -> Dict[str, Any]:
    repo = git_repo_root(root)
    fmt = "%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%s"
    meta = git_run(root, ["show", "-s", f"--format={fmt}", commit]).strip().split("\x1f")
    if len(meta) < 6: raise RuntimeError(f"Unable to read commit metadata: {commit}")
    raw = git_run(root, ["diff-tree", "--root", "--no-commit-id", "--name-status", "-r", meta[0]])
    changes = parse_name_status(raw, root, repo)
    mapped = map_paths_to_entities(root, [c.get("path") or c.get("oldPath") for c in changes if c.get("path") or c.get("oldPath")])
    return {
        "schemaVersion": "1.0", "generatedAt": now_utc(), "mappingBasis": "current-workspace",
        "commit": {"hash": meta[0], "parents": meta[1].split() if meta[1] else [], "authorName": meta[2], "authorEmail": meta[3], "authoredAt": meta[4], "subject": meta[5]},
        "changeCount": len(changes), "changes": changes, "mappedEntities": mapped.get("entities", []),
    }


def git_impact_report(root: Path, *, commit: Optional[str] = None, from_ref: Optional[str] = None, to_ref: str = "HEAD", working_tree: bool = False, max_depth: Optional[int] = None) -> Dict[str, Any]:
    if working_tree:
        base = git_status_report(root); source = {"kind": "working-tree", "head": base.get("head")}
    elif commit:
        base = git_commit_report(root, commit); source = {"kind": "commit", "commit": base.get("commit")}
    else:
        if not from_ref: raise ValueError("git-impact requires --commit, --from-ref, or --working-tree")
        base = git_range_changes(root, from_ref, to_ref); source = {"kind": "range", "from": from_ref, "to": to_ref}
    direct = base.get("mappedEntities", [])
    impacted: Dict[str, Dict[str, Any]] = {}
    direct_uids = []
    for item in direct:
        uid_value = str(item.get("uid")); direct_uids.append(uid_value)
        impacted[uid_value] = {**item, "impactDepth": 0, "impactRoots": [uid_value]}
        try:
            report = impact_report(root, uid_value, max_depth=max_depth)
        except Exception:
            continue
        for affected in report.get("affected", []):
            auid = str(affected.get("uid")); depth = int(affected.get("depth", 1))
            existing = impacted.get(auid)
            if not existing or depth < int(existing.get("impactDepth", 999)):
                impacted[auid] = {**affected, "impactDepth": depth, "impactRoots": [uid_value]}
            elif uid_value not in existing.get("impactRoots", []):
                existing.setdefault("impactRoots", []).append(uid_value)
    by_type: Dict[str, int] = defaultdict(int)
    for item in impacted.values(): by_type[str(item.get("entityType"))] += 1
    return {"schemaVersion": "1.0", "generatedAt": now_utc(), "source": source, "fileChanges": base.get("changes", []), "directEntities": direct, "impactedCount": len(impacted), "byType": dict(sorted(by_type.items())), "impactedEntities": sorted(impacted.values(), key=lambda x: (int(x.get("impactDepth", 0)), str(x.get("entityType")), str(x.get("title"))))}



def _source_cfg_dependency(root: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if config is None:
        config, _, _, _ = load_controls(root)
    return (load_source_intelligence(root, config).get("dependency") or {})


def _candidate_with_extensions(base: str, indexed_paths: set) -> List[str]:
    base = os.path.normpath(base.replace("\\", "/")).replace("\\", "/")
    if base.startswith("./"):
        base = base[2:]
    candidates: List[str] = []
    def add(value: str) -> None:
        value = str(Path(value).as_posix())
        if value in indexed_paths and value not in candidates:
            candidates.append(value)
    add(base)
    suffix = Path(base).suffix.lower()
    extensions = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".dart", ".cs", ".java", ".kt", ".kts", ".go", ".rs", ".json"]
    if suffix not in extensions:
        for ext in extensions:
            add(base + ext)
        for ext in extensions:
            add(str(Path(base) / ("index" + ext)))
        add(str(Path(base) / "__init__.py"))
    return candidates


def _module_roots(root: Path, source_index: Dict[str, Any]) -> List[str]:
    roots: List[str] = []
    for value in source_index.get("roots") or []:
        try:
            p = project_path(root, str(value))
            if p.exists():
                rel = p.relative_to(root).as_posix()
                if rel not in roots: roots.append(rel)
        except Exception:
            continue
    if not roots: roots = ["."]
    return roots


def _resolve_source_import(root: Path, importer: str, specifier: str, language: str, indexed_paths: set, source_index: Dict[str, Any], source_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    spec = str(specifier or "").strip()
    if not spec: return []
    dep_cfg = source_cfg.get("dependency") or {}
    aliases = dep_cfg.get("aliases") or []
    importer_parent = Path(importer).parent
    candidates: List[Tuple[str, str]] = []
    def add_base(base: Path | str, kind: str) -> None:
        value = Path(base).as_posix()
        for found in _candidate_with_extensions(value, indexed_paths):
            candidates.append((found, kind))

    # Explicit aliases are the safest way to resolve tsconfig/bundler aliases without executing project tooling.
    for item in aliases:
        prefix = str(item.get("prefix") or "")
        target = str(item.get("target") or "")
        if prefix and target and spec.startswith(prefix):
            add_base(Path(target) / spec[len(prefix):], "alias")

    if language in {"javascript", "javascript-react", "typescript", "typescript-react", "dart"}:
        if spec.startswith("."):
            add_base(importer_parent / spec, "relative-import")
        elif language == "dart" and spec.startswith("package:"):
            package_spec = spec.split(":", 1)[1]
            parts = package_spec.split("/", 1)
            if len(parts) == 2:
                add_base(Path("lib") / parts[1], "dart-package")
    elif language == "python":
        if spec.startswith("."):
            dots = len(spec) - len(spec.lstrip("."))
            module = spec[dots:]
            base = importer_parent
            for _ in range(max(0, dots - 1)):
                base = base.parent
            if module:
                base = base / Path(*module.split("."))
            add_base(base, "python-relative")
        else:
            mod = Path(*spec.split("."))
            for scan_root in _module_roots(root, source_index):
                base = Path() if scan_root == "." else Path(scan_root)
                add_base(base / mod, "python-module")
    elif language in {"java", "kotlin"}:
        suffixes = [str(Path(*spec.split("."))) + ext for ext in (".java", ".kt", ".kts")]
        for path in indexed_paths:
            if any(path.endswith(s) for s in suffixes):
                candidates.append((path, "language-import"))
    elif language == "go":
        module_name = None
        gomod = root / "go.mod"
        if gomod.is_file():
            try:
                m = re.search(r"(?m)^\s*module\s+(\S+)", gomod.read_text(encoding="utf-8"))
                if m: module_name = m.group(1)
            except Exception: pass
        if module_name and spec.startswith(module_name + "/"):
            add_base(spec[len(module_name)+1:], "go-module")
    elif language == "rust":
        if re.fullmatch(r"[A-Za-z_]\w*", spec):
            add_base(importer_parent / spec, "rust-mod")
        elif spec.startswith("crate::"):
            add_base(Path("src") / Path(*spec[len("crate::"):].split("::")), "rust-crate")

    dedup: List[Dict[str, Any]] = []
    seen = set()
    for path, kind in candidates:
        if path == importer: continue
        key = (path, kind)
        if key in seen: continue
        seen.add(key)
        dedup.append({"path": path, "kind": kind, "specifier": spec, "confidence": "exact" if kind in {"relative-import", "alias", "python-relative"} else "high"})
    return dedup


def build_dependency_index(root: Path, rebuild_source: bool = True) -> Dict[str, Any]:
    config, _, _, _ = load_controls(root)
    source_index = ensure_source_index(root, rebuild_if_stale=rebuild_source)
    source_cfg = load_source_intelligence(root, config)
    indexed = set((source_index.get("files") or {}).keys())
    files: Dict[str, Dict[str, Any]] = {p: {"outgoing": [], "incoming": []} for p in sorted(indexed)}
    unresolved: List[Dict[str, Any]] = []
    edge_seen = set()
    for importer, info in (source_index.get("files") or {}).items():
        language = str(info.get("language") or "")
        for specifier in info.get("imports") or []:
            resolved = _resolve_source_import(root, importer, str(specifier), language, indexed, source_index, source_cfg)
            if not resolved:
                # Bare external packages/namespaces are useful metadata but not a local graph error.
                unresolved.append({"from": importer, "specifier": specifier, "language": language})
                continue
            for edge in resolved:
                target = edge["path"]
                key = (importer, target, str(specifier))
                if key in edge_seen: continue
                edge_seen.add(key)
                out = {"to": target, "kind": edge["kind"], "specifier": specifier, "confidence": edge["confidence"]}
                inc = {"from": importer, "kind": edge["kind"], "specifier": specifier, "confidence": edge["confidence"]}
                files.setdefault(importer, {"outgoing": [], "incoming": []})["outgoing"].append(out)
                files.setdefault(target, {"outgoing": [], "incoming": []})["incoming"].append(inc)
    dep_cfg = source_cfg.get("dependency") or {}
    fingerprint = sha256_bytes(canonical_json_bytes({"sourceFingerprint": source_index.get("sourceFingerprint"), "dependency": dep_cfg}))
    doc = {
        "schemaVersion": "1.0", "generatedAt": now_utc(), "sourceFingerprint": source_index.get("sourceFingerprint"),
        "dependencyFingerprint": fingerprint, "fileCount": len(files), "edgeCount": len(edge_seen),
        "files": files, "unresolvedImports": unresolved,
    }
    save_json(dependency_index_path(root, config), doc)
    return doc


def ensure_dependency_index(root: Path, rebuild_if_stale: bool = True) -> Dict[str, Any]:
    config, _, _, _ = load_controls(root)
    path = dependency_index_path(root, config)
    source_index = ensure_source_index(root, rebuild_if_stale=rebuild_if_stale)
    dep_cfg = _source_cfg_dependency(root, config)
    expected = sha256_bytes(canonical_json_bytes({"sourceFingerprint": source_index.get("sourceFingerprint"), "dependency": dep_cfg}))
    if path.is_file():
        try:
            doc = load_json(path)
            if doc.get("dependencyFingerprint") == expected:
                return doc
        except Exception:
            pass
    if not rebuild_if_stale:
        raise ValueError("Dependency index is missing or stale; run code-deps --rebuild or reindex")
    return build_dependency_index(root)


def _normalize_code_path(root: Path, value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return path.resolve().relative_to(root.resolve()).as_posix()
    return project_path(root, value).relative_to(root).as_posix()


def code_dependency_report(root: Path, paths: List[str], *, direction: str = "both", max_depth: int = 3, rebuild_if_stale: bool = True) -> Dict[str, Any]:
    index = ensure_dependency_index(root, rebuild_if_stale=rebuild_if_stale)
    files = index.get("files") or {}
    starts = []
    for value in paths:
        rel = _normalize_code_path(root, value)
        if rel not in files:
            raise KeyError(f"Source path is not indexed: {rel}")
        if rel not in starts: starts.append(rel)
    queue: List[Tuple[str, int, Optional[str], Optional[str]]] = [(p, 0, None, None) for p in starts]
    best: Dict[str, Dict[str, Any]] = {}
    while queue:
        current, depth, parent, edge_kind = queue.pop(0)
        existing = best.get(current)
        if existing and int(existing.get("depth", 999)) <= depth: continue
        best[current] = {"path": current, "depth": depth, "via": parent, "edgeKind": edge_kind, "isRoot": current in starts}
        if depth >= max_depth: continue
        info = files.get(current) or {}
        neighbors: List[Tuple[str, str]] = []
        if direction in {"dependencies", "both"}:
            neighbors += [(str(e.get("to")), "dependency") for e in info.get("outgoing") or [] if e.get("to")]
        if direction in {"dependents", "both"}:
            neighbors += [(str(e.get("from")), "dependent") for e in info.get("incoming") or [] if e.get("from")]
        for target, kind in neighbors:
            queue.append((target, depth + 1, current, kind))
    nodes = sorted(best.values(), key=lambda x: (int(x["depth"]), x["path"]))
    return {"schemaVersion": "1.0", "generatedAt": now_utc(), "direction": direction, "maxDepth": max_depth, "roots": starts, "count": len(nodes), "nodes": nodes}


def dependency_tree_report(root: Path, path: str, *, direction: str = "dependencies", max_depth: int = 4, max_nodes: int = 200) -> Dict[str, Any]:
    index = ensure_dependency_index(root)
    files = index.get("files") or {}
    start = _normalize_code_path(root, path)
    if start not in files: raise KeyError(f"Source path is not indexed: {start}")
    counter = {"n": 0}
    def walk(current: str, depth: int, ancestry: set) -> Dict[str, Any]:
        counter["n"] += 1
        node = {"path": current, "depth": depth, "children": []}
        if current in ancestry:
            node["cycle"] = True; return node
        if depth >= max_depth or counter["n"] >= max_nodes: return node
        info = files.get(current) or {}
        next_paths: List[str] = []
        if direction in {"dependencies", "both"}: next_paths += [str(x.get("to")) for x in info.get("outgoing") or [] if x.get("to")]
        if direction in {"dependents", "both"}: next_paths += [str(x.get("from")) for x in info.get("incoming") or [] if x.get("from")]
        for child in sorted(set(next_paths)):
            if counter["n"] >= max_nodes: break
            node["children"].append(walk(child, depth + 1, ancestry | {current}))
        return node
    return {"schemaVersion": "1.0", "generatedAt": now_utc(), "direction": direction, "maxDepth": max_depth, "root": walk(start, 0, set()), "truncated": counter["n"] >= max_nodes}


def dependency_cycles_report(root: Path, min_size: int = 2) -> Dict[str, Any]:
    index = ensure_dependency_index(root)
    files = index.get("files") or {}
    graph = {p: [str(e.get("to")) for e in info.get("outgoing") or [] if e.get("to") in files] for p, info in files.items()}
    idx = 0; stack: List[str] = []; on_stack = set(); indices: Dict[str, int] = {}; low: Dict[str, int] = {}; components: List[List[str]] = []
    def strong(v: str) -> None:
        nonlocal idx
        indices[v] = idx; low[v] = idx; idx += 1; stack.append(v); on_stack.add(v)
        for w in graph.get(v, []):
            if w not in indices:
                strong(w); low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], indices[w])
        if low[v] == indices[v]:
            comp = []
            while stack:
                w = stack.pop(); on_stack.remove(w); comp.append(w)
                if w == v: break
            self_loop = len(comp) == 1 and comp[0] in graph.get(comp[0], [])
            if len(comp) >= min_size or self_loop: components.append(sorted(comp))
    for v in sorted(graph):
        if v not in indices: strong(v)
    components.sort(key=lambda x: (-len(x), x))
    return {"schemaVersion": "1.0", "generatedAt": now_utc(), "cycleCount": len(components), "cycles": [{"size": len(c), "files": c} for c in components]}


def _change_paths_report(root: Path, *, paths: Optional[List[str]] = None, commit: Optional[str] = None, from_ref: Optional[str] = None, to_ref: str = "HEAD", working_tree: bool = False) -> Dict[str, Any]:
    if paths:
        changes = [{"status": "M", "path": _normalize_code_path(root, p)} for p in paths]
        return {"source": {"kind": "paths"}, "changes": changes}
    if commit:
        report = git_commit_report(root, commit); return {"source": {"kind": "commit", "commit": report.get("commit")}, "changes": report.get("changes", [])}
    if working_tree or not from_ref:
        report = git_status_report(root); return {"source": {"kind": "working-tree", "head": report.get("head")}, "changes": report.get("changes", [])}
    report = git_range_changes(root, from_ref, to_ref); return {"source": {"kind": "range", "from": from_ref, "to": to_ref}, "changes": report.get("changes", [])}


def code_impact_report(root: Path, *, paths: Optional[List[str]] = None, commit: Optional[str] = None, from_ref: Optional[str] = None, to_ref: str = "HEAD", working_tree: bool = False, code_depth: int = 5, project_depth: int = 3) -> Dict[str, Any]:
    base = _change_paths_report(root, paths=paths, commit=commit, from_ref=from_ref, to_ref=to_ref, working_tree=working_tree)
    changed = []
    dep_index = ensure_dependency_index(root)
    indexed = set((dep_index.get("files") or {}).keys())
    for item in base.get("changes", []):
        rel = item.get("path") or item.get("oldPath")
        if rel and rel in indexed and rel not in changed: changed.append(rel)
    code = code_dependency_report(root, changed, direction="dependents", max_depth=code_depth) if changed else {"roots": [], "nodes": [], "count": 0}
    impacted_paths = [x["path"] for x in code.get("nodes", [])]
    mapped = map_paths_to_entities(root, impacted_paths)
    direct_entity_uids = {str(e.get("uid")) for e in map_paths_to_entities(root, changed).get("entities", [])}
    entities_by_uid: Dict[str, Dict[str, Any]] = {}
    for entity in mapped.get("entities", []):
        uid_value = str(entity.get("uid")); entity = dict(entity); entity["codeImpactDepth"] = min((int(n.get("depth", 0)) for n in code.get("nodes", []) for ev in entity.get("evidence", []) if ev.get("path") == n.get("path")), default=0); entity["directFromChangedFile"] = uid_value in direct_entity_uids
        entities_by_uid[uid_value] = entity
    # Expand from source-mapped entities through the durable project graph.
    project_impacted: Dict[str, Dict[str, Any]] = {}
    for uid_value, entity in list(entities_by_uid.items()):
        project_impacted[uid_value] = {**entity, "projectImpactDepth": 0, "impactRoots": [uid_value]}
        try: report = impact_report(root, uid_value, max_depth=project_depth)
        except Exception: continue
        for item in report.get("affected", []):
            auid = str(item.get("uid")); depth = int(item.get("depth", 1)); current = project_impacted.get(auid)
            if current is None or depth < int(current.get("projectImpactDepth", 999)):
                project_impacted[auid] = {**item, "projectImpactDepth": depth, "impactRoots": [uid_value]}
            elif uid_value not in current.get("impactRoots", []): current.setdefault("impactRoots", []).append(uid_value)
    return {
        "schemaVersion": "1.0", "generatedAt": now_utc(), "source": base.get("source"), "changes": base.get("changes", []),
        "changedIndexedFiles": changed, "codeImpact": code, "sourceMappedEntities": sorted(entities_by_uid.values(), key=lambda x: (int(x.get("codeImpactDepth", 0)), str(x.get("entityType")), str(x.get("title")))),
        "projectImpactedEntities": sorted(project_impacted.values(), key=lambda x: (int(x.get("projectImpactDepth", 0)), str(x.get("entityType")), str(x.get("title")))),
    }


def _is_test_path(path: str, source_cfg: Dict[str, Any]) -> bool:
    globs = (source_cfg.get("dependency") or {}).get("testGlobs") or []
    name = Path(path).name
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat) for pat in globs)


def _test_command_for_path(path: str, source_info: Dict[str, Any]) -> Optional[str]:
    imports = set(str(x) for x in source_info.get("imports") or [])
    if "@playwright/test" in imports or re.search(r"\.(?:spec|test)\.(?:ts|tsx|js|jsx)$", path):
        return f"npx playwright test {path}" if "@playwright/test" in imports else None
    if path.endswith(".py"):
        return f"pytest {path}"
    if path.endswith("_test.dart") or "/test/" in ("/" + path):
        return f"flutter test {path}"
    return None


def test_selection_report(root: Path, *, paths: Optional[List[str]] = None, commit: Optional[str] = None, from_ref: Optional[str] = None, to_ref: str = "HEAD", working_tree: bool = False, code_depth: int = 8, project_depth: int = 4) -> Dict[str, Any]:
    impact = code_impact_report(root, paths=paths, commit=commit, from_ref=from_ref, to_ref=to_ref, working_tree=working_tree, code_depth=code_depth, project_depth=project_depth)
    config, registry, _, _ = load_controls(root); entities, _ = collect_entities(root, registry); source_cfg = load_source_intelligence(root, config); source_index = ensure_source_index(root)
    selections: Dict[str, Dict[str, Any]] = {}
    depth_by_path = {str(n.get("path")): int(n.get("depth", 0)) for n in (impact.get("codeImpact") or {}).get("nodes", [])}
    for path, depth in depth_by_path.items():
        if _is_test_path(path, source_cfg):
            info = (source_index.get("files") or {}).get(path) or {}
            selections["path:" + path] = {"kind": "source-test", "path": path, "codeImpactDepth": depth, "reason": "Test file depends on a changed/impacted source file", "command": _test_command_for_path(path, info)}
    for item in impact.get("projectImpactedEntities", []):
        if item.get("entityType") != "test-script": continue
        uid_value = str(item.get("uid")); entity = entities.get(uid_value)
        if not entity: continue
        path = str(entity.get("data", {}).get("sourcePath") or "") or None
        if not path:
            for content in entity.get("content", []):
                if content.get("mode") == "file" and content.get("path"): path = str(content.get("path")); break
        if path:
            selections.pop("path:" + path, None)
        selections["entity:" + uid_value] = {"kind": "test-script-entity", **entity_summary(entity), "path": path, "projectImpactDepth": item.get("projectImpactDepth"), "reason": "Test Script is in the project impact graph", "command": entity.get("data", {}).get("command")}
    rows = sorted(selections.values(), key=lambda x: (0 if x.get("kind") == "test-script-entity" else 1, int(x.get("projectImpactDepth") or x.get("codeImpactDepth") or 0), str(x.get("path") or x.get("title") or "")))
    commands = []
    for row in rows:
        command = row.get("command")
        if command and command not in commands: commands.append(command)
    return {"schemaVersion": "1.0", "generatedAt": now_utc(), "source": impact.get("source"), "changes": impact.get("changes"), "selectedCount": len(rows), "tests": rows, "commands": commands, "impactSummary": {"codeFiles": (impact.get("codeImpact") or {}).get("count", 0), "projectEntities": len(impact.get("projectImpactedEntities", []))}}


def _analysis_plan_document(root: Path, *, title: str, request: str, analysis: Dict[str, Any], actor: str, reason: str, related: Optional[List[str]] = None) -> Dict[str, Any]:
    impacted = analysis.get("projectImpactedEntities") or (analysis.get("impact") or {}).get("projectImpactedEntities") or []
    refs: List[str] = []
    for item in impacted[:50]:
        ref = item.get("code") or item.get("localRef") or item.get("uid")
        if ref and ref not in refs: refs.append(str(ref))
    ts = now_utc(); plan_id = new_plan_id()
    doc = {
        "schemaVersion": "1.0", "planId": plan_id, "title": title, "request": request, "status": "draft",
        "actor": actor_object(actor, "workplan"), "reason": reason, "related": related or [], "contextRefs": refs,
        "baseObjects": [], "baseWorkspaceDefinitionHash": None,
        "assumptions": ["Source dependency and project graph impact are potential impact, not proof of runtime behavior."],
        "risks": ["Review source mapping confidence and impacted entities before authoring executable mutation steps."],
        "acceptanceCriteria": ["Review all directly changed files and mapped project entities.", "Review selected regression tests and add/remove tests using project-specific knowledge.", "Amend this draft with executable WorkPlan steps before submission."],
        "steps": [], "requiresAuthoring": True, "changeAnalysis": analysis,
        "execution": {"changeSetId": None, "startedAt": None, "completedAt": None, "stepResults": []}, "events": [], "createdAt": ts, "updatedAt": ts,
    }
    plan_event(doc, "generated-from-code-change", actor, reason)
    return doc


def cmd_code_deps(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root)
    if args.rebuild: build_dependency_index(root)
    report = code_dependency_report(root, args.path, direction=args.direction, max_depth=args.max_depth, rebuild_if_stale=not args.no_reindex)
    emit_json(report, args.output); return 0


def cmd_dependency_tree(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root)
    emit_json(dependency_tree_report(root, args.path, direction=args.direction, max_depth=args.max_depth, max_nodes=args.max_nodes), args.output); return 0


def cmd_circular_dependencies(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root)
    emit_json(dependency_cycles_report(root, min_size=args.min_size), args.output); return 0


def cmd_code_impact(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root)
    report = code_impact_report(root, paths=args.path, commit=args.commit, from_ref=args.from_ref, to_ref=args.to_ref, working_tree=args.working_tree, code_depth=args.code_depth, project_depth=args.project_depth)
    emit_json(report, args.output); return 0


def cmd_test_selection(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root)
    report = test_selection_report(root, paths=args.path, commit=args.commit, from_ref=args.from_ref, to_ref=args.to_ref, working_tree=args.working_tree, code_depth=args.code_depth, project_depth=args.project_depth)
    emit_json(report, args.output); return 0


def cmd_changed_tests(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root)
    report = test_selection_report(root, commit=args.commit, from_ref=args.from_ref, to_ref=args.to_ref, working_tree=args.working_tree or (not args.commit and not args.from_ref), code_depth=args.code_depth, project_depth=args.project_depth)
    emit_json(report, args.output); return 0


def _save_or_emit_generated_plan(root: Path, doc: Dict[str, Any], dry_run: bool, output: Optional[str]) -> int:
    errors = validate_workplan_doc(root, doc, resolve_refs=True)
    if errors: raise ValueError("Generated WorkPlan is invalid:\n- " + "\n- ".join(errors))
    if not dry_run:
        path = workplan_path(root, str(doc["planId"])); path.parent.mkdir(parents=True, exist_ok=True); save_json(path, doc)
    result = {"saved": not dry_run, "planId": doc.get("planId"), "status": doc.get("status"), "requiresAuthoring": True, "contextRefs": doc.get("contextRefs"), "selectedTests": ((doc.get("changeAnalysis") or {}).get("tests") or {}).get("selectedCount"), "plan": doc if dry_run else None}
    emit_json(result, output); return 0


def cmd_plan_from_git(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root)
    impact = code_impact_report(root, commit=args.commit, code_depth=args.code_depth, project_depth=args.project_depth)
    tests = test_selection_report(root, commit=args.commit, code_depth=args.code_depth, project_depth=args.project_depth)
    commit = git_commit_report(root, args.commit).get("commit") or {}
    request = args.request or f"Review and plan project updates for Git commit {commit.get('hash', args.commit)}: {commit.get('subject', '')}".strip()
    title = args.title or f"Plan changes for {str(commit.get('hash') or args.commit)[:12]}"
    doc = _analysis_plan_document(root, title=title, request=request, analysis={"kind": "git-commit", "commit": commit, "impact": impact, "tests": tests}, actor=args.actor, reason=args.reason or "Generate a reviewable WorkPlan shell from Git commit impact", related=args.related)
    return _save_or_emit_generated_plan(root, doc, args.dry_run, args.output)


def cmd_plan_from_diff(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root)
    working = args.working_tree or not args.from_ref
    impact = code_impact_report(root, from_ref=args.from_ref, to_ref=args.to_ref, working_tree=working, code_depth=args.code_depth, project_depth=args.project_depth)
    tests = test_selection_report(root, from_ref=args.from_ref, to_ref=args.to_ref, working_tree=working, code_depth=args.code_depth, project_depth=args.project_depth)
    source = impact.get("source") or {}
    label = "working tree" if source.get("kind") == "working-tree" else f"{args.from_ref}..{args.to_ref}"
    request = args.request or f"Review current code changes and plan required project-model/document/test updates for {label}."
    title = args.title or f"Plan from diff: {label}"
    doc = _analysis_plan_document(root, title=title, request=request, analysis={"kind": "git-diff", "source": source, "impact": impact, "tests": tests}, actor=args.actor, reason=args.reason or "Generate a reviewable WorkPlan shell from source diff impact", related=args.related)
    return _save_or_emit_generated_plan(root, doc, args.dry_run, args.output)

def markdown_metadata(text: str, fallback_title: str) -> Tuple[str, Optional[str]]:
    title = fallback_title
    for line in text.splitlines():
        if re.match(r"^#\s+\S", line):
            title = re.sub(r"^#\s+", "", line).strip(); break
    paras = []
    for block in re.split(r"\n\s*\n", text):
        cleaned = " ".join(line.strip() for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#"))
        if cleaned:
            paras.append(cleaned)
            break
    return title, (paras[0][:500] if paras else None)


def create_import_entity(root: Path, entity_type: str, title: str, data: Dict[str, Any], content: Optional[List[Dict[str, Any]]] = None, tags: Optional[List[str]] = None) -> Dict[str, Any]:
    _, registry, _, _ = load_controls(root)
    spec = registry.get("types", {}).get(entity_type)
    if not spec: raise KeyError(f"Unknown entity type: {entity_type}")
    uid_value = str(uuid.uuid4()); ts = now_utc(); defaults = copy.deepcopy(spec.get("defaults", {}))
    entity = {
        "schemaVersion": "1.0", "entityType": entity_type, "uid": uid_value, "id": None, "code": None,
        "localRef": f"local:{entity_type}:{uid_value[:8]}", "title": title, "status": defaults.pop("status", "draft"),
        "isDeleted": False, "deletedAt": None, "tags": tags or [], "relations": [], "data": {}, "content": content or [],
        "revision": 1, "createdAt": ts, "updatedAt": ts,
    }
    entity = deep_merge(entity, defaults); entity["data"] = deep_merge(entity.get("data", {}), data)
    errors = validate_entity_definition(root, registry, entity_type, entity)
    if errors: raise ValueError("Imported entity is invalid:\n- " + "\n- ".join(errors))
    save_json(entity_dest(root, registry, entity_type, uid_value), entity)
    return entity


def path_for_entity_content(root: Path, source: Path, entity_type: str, uid_value: str) -> str:
    source = source.resolve()
    try:
        return source.relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = f"files/{entity_type}/{uid_value}/{source.name}"
        target = project_path(root, rel); target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)
        return rel


def openapi_operations(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    if path.suffix.lower() == ".json" or text.lstrip().startswith("{"):
        doc = json.loads(text); result = []
        for route, item in (doc.get("paths") or {}).items():
            if not isinstance(item, dict): continue
            for method, op in item.items():
                if str(method).lower() not in methods or not isinstance(op, dict): continue
                result.append({"method": str(method).upper(), "path": route, "operationId": op.get("operationId"), "summary": op.get("summary"), "tags": op.get("tags") or []})
        return result
    # Minimal OpenAPI YAML path/method reader; intentionally ignores complex YAML features.
    result = []; current_path = None; current = None; in_paths = False; paths_indent = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"): continue
        indent = len(raw) - len(raw.lstrip(" ")); stripped = raw.strip()
        if stripped == "paths:": in_paths = True; paths_indent = indent; current_path = None; current = None; continue
        if not in_paths: continue
        if paths_indent is not None and indent <= paths_indent and stripped != "paths:": break
        m_path = re.match(r"^(['\"]?)(/[^:'\"]+)\1:\s*$", stripped)
        if m_path:
            current_path = m_path.group(2); current = None; continue
        m_method = re.match(r"^(get|post|put|patch|delete|head|options):\s*$", stripped, re.I)
        if current_path and m_method:
            current = {"method": m_method.group(1).upper(), "path": current_path, "operationId": None, "summary": None, "tags": []}; result.append(current); continue
        if current:
            m = re.match(r"^operationId:\s*['\"]?([^'\"]+)['\"]?\s*$", stripped)
            if m: current["operationId"] = m.group(1).strip(); continue
            m = re.match(r"^summary:\s*['\"]?(.*?)['\"]?\s*$", stripped)
            if m: current["summary"] = m.group(1).strip()
    return result


def sql_objects(text: str) -> List[Dict[str, Any]]:
    pat = re.compile(r"\bCREATE\s+(MATERIALIZED\s+VIEW|TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[\"`\[]?([A-Za-z_][\w$]*)[\"`\]]?\.)?[\"`\[]?([A-Za-z_][\w$]*)[\"`\]]?", re.I)
    result = []
    for m in pat.finditer(text):
        kind = re.sub(r"\s+", "-", m.group(1).lower()); schema = m.group(2); name = m.group(3)
        item = {"schema": schema, "tableName": name, "objectType": kind, "primaryKey": [], "columns": []}
        if kind == "table":
            start = text.find("(", m.end())
            if start >= 0:
                depth = 0; end = None
                for i in range(start, min(len(text), start + 200000)):
                    if text[i] == "(": depth += 1
                    elif text[i] == ")":
                        depth -= 1
                        if depth == 0: end = i; break
                if end:
                    body = text[start+1:end]
                    pk = re.search(r"PRIMARY\s+KEY\s*\(([^)]+)\)", body, re.I)
                    if pk: item["primaryKey"] = [x.strip().strip('"`[] ') for x in pk.group(1).split(",")]
                    for line in body.split(","):
                        first = line.strip().split(None, 1)
                        if len(first) >= 2 and first[0].upper() not in {"PRIMARY", "FOREIGN", "CONSTRAINT", "UNIQUE", "CHECK"}:
                            col = first[0].strip('"`[]')
                            item["columns"].append(col)
                            if re.search(r"\bPRIMARY\s+KEY\b", first[1], re.I) and col not in item["primaryKey"]:
                                item["primaryKey"].append(col)
        result.append(item)
    return result


def playwright_tests(text: str) -> List[Dict[str, Any]]:
    pat = re.compile(r"\btest(?:\.(skip|only|fixme))?\s*\(\s*(['\"`])(.+?)\2", re.S)
    result = []
    for m in pat.finditer(text):
        title = re.sub(r"\s+", " ", m.group(3)).strip()
        result.append({"title": title[:500], "modifier": m.group(1), "line": text.count("\n", 0, m.start()) + 1})
    return result


def cmd_scan_source(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root)
    doc = build_source_index(root, roots_override=args.root)
    summary = {"schemaVersion": doc.get("schemaVersion"), "generatedAt": doc.get("generatedAt"), "sourceFingerprint": doc.get("sourceFingerprint"), "fileCount": doc.get("fileCount"), "mappedEntityCount": doc.get("mappedEntityCount"), "sourceIndex": source_index_path(root).relative_to(root).as_posix()}
    emit_json(doc if args.full else summary, args.output)
    return 0


def cmd_source_map(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root)
    index = ensure_source_index(root, rebuild_if_stale=not args.no_reindex)
    if args.path:
        rel = str(args.path).replace("\\", "/").lstrip("./")
        mapped = map_paths_to_entities(root, [rel], rebuild_if_stale=not args.no_reindex)
        result = {"path": rel, "source": index.get("files", {}).get(rel), "mappedEntities": mapped.get("entities", [])}
    else:
        uid_value, entity = resolve_entity_ref(root, args.ref)
        result = {"entity": entity_summary(entity), "sources": index.get("byEntity", {}).get(uid_value, [])}
    emit_json(result, args.output); return 0


def cmd_git_status(args: argparse.Namespace) -> int:
    emit_json(git_status_report(Path(args.project).resolve()), args.output); return 0


def cmd_changes_since_commit(args: argparse.Namespace) -> int:
    emit_json(git_range_changes(Path(args.project).resolve(), args.from_ref, args.to_ref), args.output); return 0


def cmd_git_impact(args: argparse.Namespace) -> int:
    report = git_impact_report(Path(args.project).resolve(), commit=args.commit, from_ref=args.from_ref, to_ref=args.to_ref, working_tree=args.working_tree, max_depth=args.max_depth)
    emit_json(report, args.output); return 0


def cmd_commit_context(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); base = git_commit_report(root, args.commit)
    contexts = []
    for item in base.get("mappedEntities", [])[:args.max_entities]:
        try: contexts.append(context_pack(root, str(item.get("uid")), detail=args.detail, max_depth=args.max_depth, max_entities=args.max_entities))
        except Exception: pass
    base["entityContexts"] = contexts
    emit_json(base, args.output); return 0


def cmd_git_changeset(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root)
    report = git_commit_report(root, args.commit)
    doc = write_change_set(root, [], status="evidence", source="git", actor=args.actor, reason=args.reason or report.get("commit", {}).get("subject") or "Git commit evidence", related=args.related, extra={"gitEvidence": report}, allow_empty=True)
    if not doc: raise RuntimeError("Unable to create Git evidence ChangeSet")
    print(json.dumps({"changeSetId": doc.get("changeSetId"), "commit": report.get("commit", {}).get("hash"), "mappedEntities": len(report.get("mappedEntities", []))}, ensure_ascii=False)); return 0


def cmd_git_link(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); path, doc = find_change_set(root, args.change_set)
    commit = git_commit_report(root, args.commit).get("commit")
    doc.setdefault("gitLinks", [])
    if not any(x.get("hash") == commit.get("hash") for x in doc["gitLinks"]): doc["gitLinks"].append(commit)
    doc["auditRevision"] = int(doc.get("auditRevision", 1)) + 1; doc["updatedAt"] = now_utc(); doc.setdefault("events", []).append({"type": "git-linked", "at": now_utc(), "actor": actor_object(args.actor, "git"), "commit": commit.get("hash")})
    save_json(path, doc); print(json.dumps({"changeSetId": args.change_set, "commit": commit.get("hash")}, ensure_ascii=False)); return 0


def cmd_import_markdown(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root); source = Path(args.path).resolve()
    if not source.is_file(): raise FileNotFoundError(source)
    text = source.read_text(encoding="utf-8"); title, summary = markdown_metadata(text, source.stem)
    if args.title: title = args.title
    entity = create_import_entity(root, "document", title, {"documentType": args.document_type, "summary": summary, "sourcePath": None}, tags=args.tag or ["imported"])
    rel = path_for_entity_content(root, source, "document", entity["uid"]); entity["data"]["sourcePath"] = rel; entity["content"] = [{"name": "body", "format": "markdown", "mode": "file", "path": rel}]
    _, registry, _, _ = load_controls(root); save_json(entity_dest(root, registry, "document", entity["uid"]), entity); build_manifest(root)
    print(json.dumps({"uid": entity["uid"], "localRef": entity["localRef"], "title": entity["title"], "sourcePath": rel}, ensure_ascii=False)); return 0


def cmd_scan_openapi(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root); source = Path(args.path).resolve()
    if not source.is_file(): raise FileNotFoundError(source)
    ops = openapi_operations(source); created = []; skipped = []
    if args.apply:
        _, registry, _, _ = load_controls(root); entities, _ = collect_entities(root, registry)
        existing = {(str(e.get("data", {}).get("method")), str(e.get("data", {}).get("path"))) for e in entities.values() if e.get("entityType") == "api" and not e.get("isDeleted")}
        for op in ops:
            key = (op["method"], str(op["path"]))
            if key in existing: skipped.append({**op, "reason": "existing-api"}); continue
            title = op.get("summary") or f"{op['method']} {op['path']}"
            entity = create_import_entity(root, "api", title, {"method": op["method"], "path": op["path"], "operationId": op.get("operationId"), "sourcePath": None}, tags=["openapi-import"] + [str(x) for x in op.get("tags", [])])
            rel = path_for_entity_content(root, source, "api", entity["uid"]); entity["data"]["sourcePath"] = rel; entity["content"] = [{"name": "openapi", "format": "openapi", "mode": "file", "path": rel}]
            save_json(entity_dest(root, registry, "api", entity["uid"]), entity); created.append(entity_summary(entity)); existing.add(key)
        build_manifest(root)
    emit_json({"schemaVersion": "1.0", "source": str(source), "operationCount": len(ops), "operations": ops, "applied": args.apply, "created": created, "skipped": skipped}, args.output); return 0


def cmd_scan_database(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root); source = Path(args.path).resolve()
    if not source.is_file(): raise FileNotFoundError(source)
    text = source.read_text(encoding="utf-8"); objects = sql_objects(text); created = []; skipped = []
    if args.apply:
        _, registry, _, _ = load_controls(root); entities, _ = collect_entities(root, registry)
        existing = {(str(e.get("data", {}).get("schema") or ""), str(e.get("data", {}).get("tableName"))) for e in entities.values() if e.get("entityType") == "database-table" and not e.get("isDeleted")}
        for obj in objects:
            key = (str(obj.get("schema") or ""), str(obj.get("tableName")))
            if key in existing: skipped.append({**obj, "reason": "existing-database-object"}); continue
            title = ".".join(x for x in [obj.get("schema"), obj.get("tableName")] if x)
            data = {**obj, "engine": args.engine, "sourcePath": None}
            entity = create_import_entity(root, "database-table", title, data, tags=["sql-import"])
            rel = path_for_entity_content(root, source, "database-table", entity["uid"]); entity["data"]["sourcePath"] = rel; entity["content"] = [{"name": "ddl", "format": "sql", "mode": "file", "path": rel}]
            save_json(entity_dest(root, registry, "database-table", entity["uid"]), entity); created.append(entity_summary(entity)); existing.add(key)
        build_manifest(root)
    emit_json({"schemaVersion": "1.0", "source": str(source), "objectCount": len(objects), "objects": objects, "applied": args.apply, "created": created, "skipped": skipped}, args.output); return 0


def cmd_scan_playwright(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve(); require_project(root); source_arg = Path(args.path).resolve()
    files = [source_arg] if source_arg.is_file() else sorted([p for p in source_arg.rglob("*") if p.is_file() and re.search(r"\.(?:spec|test)\.(?:ts|tsx|js|jsx)$", p.name)])
    candidates = []
    for source in files:
        try: text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError: continue
        tests = playwright_tests(text)
        if tests: candidates.append({"source": str(source), "title": source.stem, "language": source_language(source), "testCount": len(tests), "tests": tests})
    created = []; skipped = []
    if args.apply:
        _, registry, _, _ = load_controls(root); entities, _ = collect_entities(root, registry)
        existing_paths = {str(e.get("data", {}).get("sourcePath")) for e in entities.values() if e.get("entityType") == "test-script" and not e.get("isDeleted")}
        for item in candidates:
            source = Path(item["source"]); rel_in = None
            try: rel_in = source.relative_to(root).as_posix()
            except ValueError: pass
            if rel_in and rel_in in existing_paths: skipped.append({**item, "reason": "existing-test-script"}); continue
            data = {"framework": "playwright", "language": "typescript" if "typescript" in item["language"] else "javascript", "command": f"npx playwright test {rel_in or source.name}", "enabled": True, "sourcePath": None, "testCount": item["testCount"], "tests": item["tests"]}
            entity = create_import_entity(root, "test-script", item["title"], data, tags=["playwright-import"])
            rel = path_for_entity_content(root, source, "test-script", entity["uid"]); entity["data"]["sourcePath"] = rel; entity["content"] = [{"name": "script", "format": "typescript" if "typescript" in item["language"] else "javascript", "mode": "file", "path": rel}]
            save_json(entity_dest(root, registry, "test-script", entity["uid"]), entity); created.append(entity_summary(entity)); existing_paths.add(rel)
        build_manifest(root)
    emit_json({"schemaVersion": "1.0", "candidateCount": len(candidates), "candidates": candidates, "applied": args.apply, "created": created, "skipped": skipped}, args.output); return 0

def load_documentation_policy(root: Path) -> Dict[str, Any]:
    config, _, _, _ = load_controls(root)
    rel = config.get("storage", {}).get("documentationPolicy", ".pm/documentation-policy.json")
    path = project_path(root, rel)
    if not path.is_file():
        return {
            "schemaVersion": "1.0",
            "traceAllInteractions": True,
            "documentationFirst": True,
            "implementationCommand": "implement",
            "implementationRequiresApprovedPlan": True,
            "implementationRequiresDocumentation": True,
            "autoCreateImplementationTask": True,
            "graphHtml": {"enabled": True, "path": "docs/project-graph.html"},
        }
    return load_json(path)


def request_folder(root: Path) -> Path:
    config, _, _, _ = load_controls(root)
    rel = config.get("storage", {}).get("requestFolder", ".pm/requests")
    path = project_path(root, rel)
    path.mkdir(parents=True, exist_ok=True)
    return path


def request_id() -> str:
    return "REQLOG-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def request_path(root: Path, rid: str) -> Path:
    if not re.fullmatch(r"REQLOG-[A-Za-z0-9TZ-]+", rid):
        raise ValueError(f"Invalid request id: {rid}")
    return request_folder(root) / f"{rid}.json"


def load_requests(root: Path) -> List[Tuple[Path, Dict[str, Any]]]:
    result = []
    folder = request_folder(root)
    for path in sorted(folder.glob("REQLOG-*.json")):
        try:
            doc = load_json(path)
            if isinstance(doc, dict):
                result.append((path, doc))
        except Exception:
            continue
    return result


def bundle_request_files(root: Path) -> List[Dict[str, Any]]:
    return [{"path": p.relative_to(root).as_posix(), "json": d} for p, d in load_requests(root)]


def restore_bundle_request_files(root: Path, items: List[Dict[str, Any]]) -> None:
    for item in items:
        rel = str(item.get("path") or "")
        if not rel.startswith(".pm/requests/REQLOG-") or not rel.endswith(".json"):
            raise ValueError(f"Unsupported request-trace bundle path: {rel}")
        save_json(project_path(root, rel), item.get("json"))


def resolve_refs_to_uids(root: Path, refs: Optional[List[str]]) -> List[str]:
    if not refs:
        return []
    _, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    out = []
    for ref in refs:
        try:
            uid_value, _ = resolve_entity_ref(root, str(ref), entities=entities, registry=registry)
            if uid_value not in out:
                out.append(uid_value)
        except Exception:
            continue
    return out


def create_document_with_file(root: Path, title: str, document_type: str, filename: str, body: str, *, summary: Optional[str] = None, tags: Optional[List[str]] = None, fmt: str = "markdown", extra_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = {"documentType": document_type, "summary": summary, "audience": []}
    if extra_data:
        data.update(extra_data)
    entity = create_import_entity(root, "document", title, data, tags=tags or [document_type])
    ext = {"markdown": "md", "html": "html", "text": "txt"}.get(fmt, fmt)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-") or f"document.{ext}"
    if "." not in safe_name:
        safe_name += "." + ext
    rel = f"files/document/{entity['uid']}/{safe_name}"
    path = project_path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    entity["data"]["sourcePath"] = rel
    entity["content"] = [{"name": "body", "format": fmt, "mode": "file", "path": rel}]
    _, registry, _, _ = load_controls(root)
    save_json(entity_dest(root, registry, "document", entity["uid"]), entity)
    return entity


def add_relation_direct(root: Path, source_uid: str, relation: str, target_uid: str) -> None:
    _, registry, relation_map, _ = load_controls(root)
    entities, paths = collect_entities(root, registry)
    source = entities.get(source_uid)
    target = entities.get(target_uid)
    if not source or not target:
        raise KeyError("Source or target entity not found")
    if not any(matches_mapping(source["entityType"], relation, target["entityType"], m) for m in relation_map.get("mappings", [])):
        raise ValueError(f"Relation not allowed: {source['entityType']} --{relation}--> {target['entityType']}")
    if any(r.get("type") == relation and r.get("targetUid") == target_uid for r in source.get("relations", [])):
        return
    source.setdefault("relations", []).append({"type": relation, "targetUid": target_uid, "targetType": target["entityType"], "metadata": {}})
    source["revision"] = int(source.get("revision", 0)) + 1
    source["updatedAt"] = now_utc()
    save_json(paths[source_uid], source)


def cmd_request_capture(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    require_project(root)
    policy = load_documentation_policy(root)
    rid = request_id()
    ts = now_utc()
    doc_uids: List[str] = []
    auto_doc = bool(policy.get("documentationFirst", True)) and not args.no_document and args.kind in {"idea", "request", "requirement", "change"}
    if auto_doc:
        title = args.title or (args.text.strip().splitlines()[0][:100] if args.text.strip() else "Project request")
        body = f"# {title}\n\n## Original request\n\n{args.text.strip()}\n\n## Understanding\n\n- TODO: clarify intent, scope, constraints, and affected areas.\n\n## Proposed documentation changes\n\n- TODO\n\n## Implementation decision\n\nDocumentation first. Code changes require an explicit `implement` command after an approved WorkPlan.\n"
        entity = create_document_with_file(root, f"Request: {title}", "request-note", "request.md", body, summary=args.text.strip()[:500], tags=["request-trace", args.kind], extra_data={"requestId": rid})
        doc_uids.append(entity["uid"])
    record = {
        "schemaVersion": "1.0",
        "requestId": rid,
        "kind": args.kind,
        "title": args.title,
        "text": args.text,
        "actor": args.actor,
        "status": "captured",
        "capturedAt": ts,
        "updatedAt": ts,
        "relatedEntityUids": resolve_refs_to_uids(root, args.related),
        "documentUids": doc_uids,
        "workPlanIds": [],
        "taskUids": [],
        "resultSummary": None,
        "closedAt": None,
    }
    save_json(request_path(root, rid), record)
    build_manifest(root)
    print(json.dumps({"requestId": rid, "status": record["status"], "documentUids": doc_uids}, ensure_ascii=False))
    return 0


def cmd_request_list(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    rows = []
    for _, d in load_requests(root):
        if args.status and d.get("status") != args.status:
            continue
        rows.append({k: d.get(k) for k in ["requestId", "kind", "title", "status", "actor", "capturedAt", "updatedAt", "closedAt"]})
    emit_json({"count": len(rows), "requests": rows[-args.limit:]}, args.output)
    return 0


def cmd_request_show(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    path = request_path(root, args.request)
    if not path.is_file():
        raise KeyError(f"Request not found: {args.request}")
    emit_json(load_json(path), args.output)
    return 0


def cmd_request_close(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    path = request_path(root, args.request)
    if not path.is_file():
        raise KeyError(f"Request not found: {args.request}")
    d = load_json(path)
    d["status"] = "answered" if d.get("kind") == "question" else "completed"
    d["resultSummary"] = args.summary
    d["updatedAt"] = now_utc()
    d["closedAt"] = d["updatedAt"]
    save_json(path, d)
    print(json.dumps({"requestId": d["requestId"], "status": d["status"]}, ensure_ascii=False))
    return 0


def idea_doc_templates(title: str, idea: str) -> List[Tuple[str, str, str]]:
    return [
        ("Idea Brief", "idea-brief", f"# {title}\n\n## Idea\n\n{idea}\n\n## Problem\n\n- TODO\n\n## Target users\n\n- TODO\n\n## Goals\n\n- TODO\n\n## Non-goals\n\n- TODO\n\n## Success criteria\n\n- TODO\n"),
        ("Product Scope", "product-scope", f"# {title} - Product Scope\n\n## In scope\n\n- TODO\n\n## Out of scope\n\n- TODO\n\n## Candidate modules\n\nModules are feature groups. Define functional groups first, then place features such as Login/Register under them.\n\n- TODO\n"),
        ("Functional Overview", "functional-overview", f"# {title} - Functional Overview\n\n## Actors\n\n- TODO\n\n## Main business flows\n\n- TODO\n\n## Candidate features\n\n- TODO\n\n## Requirements to refine\n\n- TODO\n"),
        ("Business Rules & Assumptions", "business-rules", f"# {title} - Business Rules & Assumptions\n\n## Business rules\n\n- TODO\n\n## Assumptions\n\n- TODO\n\n## Constraints\n\n- TODO\n"),
        ("Technical Outline", "technical-outline", f"# {title} - Technical Outline\n\n## Proposed architecture\n\n- TODO\n\n## APIs / integrations\n\n- TODO\n\n## Data model\n\n- TODO\n\n## Security / non-functional requirements\n\n- TODO\n"),
        ("Open Questions & Decisions", "open-questions", f"# {title} - Open Questions & Decisions\n\n## Open questions\n\n- TODO\n\n## Decisions\n\n- TODO\n\n## Risks\n\n- TODO\n"),
    ]


def cmd_idea_bootstrap(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    require_project(root)
    _, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    if any(not e.get("isDeleted") for e in entities.values()) and not args.force:
        raise ValueError("Idea bootstrap is intended for an empty project. Use --force to add the documentation pack anyway.")
    rid = request_id()
    created = []
    root_doc = None
    for name, dtype, body in idea_doc_templates(args.title, args.idea):
        entity = create_document_with_file(root, f"{args.title} - {name}", dtype, re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + ".md", body, summary=args.idea[:500], tags=["idea-bootstrap", dtype], extra_data={"requestId": rid})
        created.append(entity)
        root_doc = root_doc or entity
    for entity in created[1:]:
        add_relation_direct(root, root_doc["uid"], "documents", entity["uid"])
    record = {
        "schemaVersion": "1.0",
        "requestId": rid,
        "kind": "idea",
        "title": args.title,
        "text": args.idea,
        "actor": args.actor,
        "status": "documented",
        "capturedAt": now_utc(),
        "updatedAt": now_utc(),
        "relatedEntityUids": [],
        "documentUids": [e["uid"] for e in created],
        "workPlanIds": [],
        "taskUids": [],
        "resultSummary": "Initial idea documentation pack created. No code implementation was performed.",
        "closedAt": None,
    }
    save_json(request_path(root, rid), record)
    build_manifest(root)
    print(json.dumps({"requestId": rid, "documents": [entity_summary(e) for e in created], "graphHtml": load_documentation_policy(root).get("graphHtml", {}).get("path")}, ensure_ascii=False))
    return 0


def graph_entity_data(root: Path, registry: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    entities, _ = collect_entities(root, registry)
    nodes = []
    edges = []
    for uid_value, e in entities.items():
        if e.get("isDeleted"):
            continue
        nodes.append({"uid": uid_value, "type": e.get("entityType"), "title": e.get("title"), "code": e.get("code") or e.get("localRef"), "status": e.get("status")})
        for rel in e.get("relations", []):
            if rel.get("targetUid") in entities and not entities[rel.get("targetUid")].get("isDeleted"):
                edges.append({"source": uid_value, "target": rel.get("targetUid"), "relation": rel.get("type")})
    return nodes, edges


def build_project_graph_html(root: Path, registry: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    try:
        policy = load_documentation_policy(root)
    except Exception:
        return None
    graph_cfg = policy.get("graphHtml", {})
    if not graph_cfg.get("enabled", True):
        return None
    if registry is None:
        _, registry, _, _ = load_controls(root)
    nodes, edges = graph_entity_data(root, registry)
    types = list(registry.get("types", {}).keys())
    grouped = defaultdict(list)
    for n in nodes:
        grouped[str(n.get("type"))].append(n)
    pos = {}
    width = 240
    col_gap = 45
    row_gap = 105
    margin = 40
    for ci, t in enumerate(types):
        for ri, n in enumerate(sorted(grouped.get(t, []), key=lambda x: (str(x.get("title")), str(x.get("uid"))))):
            pos[n["uid"]] = (margin + ci * (width + col_gap), 80 + ri * row_gap)
    max_rows = max([len(v) for v in grouped.values()] or [1])
    svg_w = max(900, margin * 2 + len(types) * (width + col_gap))
    svg_h = max(500, 160 + max_rows * row_gap)
    edge_parts = []
    for e in edges:
        if e["source"] not in pos or e["target"] not in pos:
            continue
        x1, y1 = pos[e["source"]]
        x2, y2 = pos[e["target"]]
        x1 += width / 2; y1 += 26; x2 += width / 2; y2 += 26
        edge_parts.append(f'<g class="edge" data-source="{html.escape(e["source"])}" data-target="{html.escape(e["target"])}"><line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" marker-end="url(#arrow)"/><text x="{(x1+x2)/2}" y="{(y1+y2)/2-4}">{html.escape(str(e["relation"]))}</text></g>')
    node_parts = []
    for n in nodes:
        x, y = pos[n["uid"]]
        label = str(n.get("title") or "")[:34]
        meta = f'{n.get("code") or ""} · {n.get("status") or ""}'
        search = (str(n.get("title")) + " " + str(n.get("code")) + " " + str(n.get("type"))).lower()
        node_parts.append(f'<g class="node" data-type="{html.escape(str(n.get("type")))}" data-uid="{html.escape(n["uid"])}" data-search="{html.escape(search)}"><rect x="{x}" y="{y}" width="{width}" height="58" rx="8"/><text class="title" x="{x+12}" y="{y+23}">{html.escape(label)}</text><text class="meta" x="{x+12}" y="{y+44}">{html.escape(meta[:38])}</text></g>')
    type_options = ''.join(f'<option value="{html.escape(t)}">{html.escape(t)}</option>' for t in types if grouped.get(t))
    payload = json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False).replace('</', '<\\/')
    page = "<!doctype html><html><head><meta charset='utf-8'><title>Project Graph</title>" \
        "<style>body{font-family:Arial,sans-serif;margin:0;background:#f6f7f9;color:#202124}header{position:sticky;top:0;background:white;padding:14px 18px;border-bottom:1px solid #ddd;z-index:3}input,select{padding:8px;margin-right:8px}.wrap{overflow:auto;padding:16px}svg{background:white;border:1px solid #ddd;border-radius:10px}.node rect{fill:#fff;stroke:#666;stroke-width:1.4}.node .title{font-size:13px;font-weight:700}.node .meta{font-size:11px;fill:#666}.edge line{stroke:#999;stroke-width:1.2}.edge text{font-size:10px;fill:#666}.hidden{display:none}.stats{font-size:12px;color:#666;margin-left:10px}</style></head><body>" \
        f"<header><b>Project Knowledge Graph</b> <span class='stats'>{len(nodes)} entities · {len(edges)} relations</span><div style='margin-top:10px'><input id='q' placeholder='Search entity'><select id='type'><option value=''>All types</option>{type_options}</select><button onclick='applyFilter()'>Filter</button><button onclick='resetFilter()'>Reset</button></div></header>" \
        f"<div class='wrap'><svg width='{svg_w}' height='{svg_h}' viewBox='0 0 {svg_w} {svg_h}'><defs><marker id='arrow' markerWidth='8' markerHeight='8' refX='7' refY='3' orient='auto'><path d='M0,0 L0,6 L8,3 z' fill='#999'/></marker></defs>{''.join(edge_parts)}{''.join(node_parts)}</svg></div>" \
        f"<script>const graph={payload};function applyFilter(){{const q=document.getElementById('q').value.toLowerCase();const t=document.getElementById('type').value;const visible=new Set();document.querySelectorAll('.node').forEach(n=>{{const ok=(!q||n.dataset.search.includes(q))&&(!t||n.dataset.type===t);n.classList.toggle('hidden',!ok);if(ok)visible.add(n.dataset.uid)}});document.querySelectorAll('.edge').forEach(e=>e.classList.toggle('hidden',!(visible.has(e.dataset.source)&&visible.has(e.dataset.target))));}}function resetFilter(){{document.getElementById('q').value='';document.getElementById('type').value='';applyFilter();}}</script></body></html>"
    rel = str(graph_cfg.get("path") or "docs/project-graph.html")
    path = project_path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return path


def cmd_graph_build(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    require_project(root)
    _, registry, _, _ = load_controls(root)
    path = build_project_graph_html(root, registry)
    if not path:
        raise ValueError("Graph HTML generation is disabled")
    print(json.dumps({"path": path.relative_to(root).as_posix()}, ensure_ascii=False))
    return 0


def render_diagram_html(title: str, kind: str, spec: Dict[str, Any]) -> str:
    nodes = spec.get("nodes") or []
    edges = spec.get("edges") or []
    direction = str(spec.get("direction") or "LR").upper()
    if not nodes:
        raise ValueError("Diagram spec must contain nodes")
    width = 220; height = 64; gapx = 90; gapy = 80; positions = {}
    for i, n in enumerate(nodes):
        if direction == "TB":
            positions[str(n.get("id"))] = (80 + (i % 3) * (width + gapx), 80 + (i // 3) * (height + gapy))
        else:
            positions[str(n.get("id"))] = (80 + (i % 4) * (width + gapx), 80 + (i // 4) * (height + gapy))
    svg_w = max(900, max((x for x, _ in positions.values()), default=0) + width + 100)
    svg_h = max(480, max((y for _, y in positions.values()), default=0) + height + 120)
    ep = []
    for e in edges:
        s = str(e.get("from")); t = str(e.get("to"))
        if s not in positions or t not in positions:
            continue
        x1, y1 = positions[s]; x2, y2 = positions[t]
        x1 += width/2; y1 += height/2; x2 += width/2; y2 += height/2
        ep.append(f'<g><line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" marker-end="url(#a)"/><text x="{(x1+x2)/2}" y="{(y1+y2)/2-5}">{html.escape(str(e.get("label") or ""))}</text></g>')
    np = []
    for n in nodes:
        nid = str(n.get("id")); x, y = positions[nid]
        label = html.escape(str(n.get("label") or nid)); subtitle = html.escape(str(n.get("description") or n.get("type") or ""))
        np.append(f'<g><rect x="{x}" y="{y}" width="{width}" height="{height}" rx="10"/><text class="t" x="{x+12}" y="{y+25}">{label[:45]}</text><text class="s" x="{x+12}" y="{y+47}">{subtitle[:48]}</text></g>')
    notes = html.escape(str(spec.get("notes") or "")).replace('\n', '<br>')
    return "<!doctype html><html><head><meta charset='utf-8'>" + f"<title>{html.escape(title)}</title>" + \
        "<style>body{font-family:Arial,sans-serif;margin:24px;color:#202124}.badge{display:inline-block;background:#eef2ff;padding:5px 9px;border-radius:12px;font-size:12px}svg{border:1px solid #ddd;border-radius:10px;background:#fafafa}rect{fill:white;stroke:#555;stroke-width:1.5}line{stroke:#777;stroke-width:1.4}text{font-size:11px;fill:#555}.t{font-size:13px;font-weight:bold;fill:#202124}.s{font-size:10px;fill:#777}.notes{margin-top:18px;padding:14px;background:#f7f7f7;border-radius:8px}</style></head><body>" + \
        f"<h1>{html.escape(title)}</h1><span class='badge'>{html.escape(kind)}</span><p>Generated from project documentation. Edit the diagram spec/source documentation, then regenerate when the flow changes.</p>" + \
        f"<svg width='{svg_w}' height='{svg_h}' viewBox='0 0 {svg_w} {svg_h}'><defs><marker id='a' markerWidth='8' markerHeight='8' refX='7' refY='3' orient='auto'><path d='M0,0 L0,6 L8,3 z' fill='#777'/></marker></defs>{''.join(ep)}{''.join(np)}</svg><div class='notes'>{notes}</div></body></html>"


def diagram_spec_from_entity(root: Path, ref: str, depth: int = 2) -> Tuple[str, Dict[str, Any], List[str]]:
    _, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    uid_value, entity = resolve_entity_ref(root, ref, entities=entities, registry=registry)
    rel_path = root / ".pm" / "indexes" / "relation-index.json"
    if not rel_path.is_file():
        build_manifest(root)
    rel_index = load_json(rel_path)
    seen = {uid_value}; frontier = [uid_value]
    for _ in range(max(0, depth)):
        nxt = []
        for u in frontier:
            for edge in rel_index.get("outgoing", {}).get(u, []) + rel_index.get("incoming", {}).get(u, []):
                v = edge.get("targetUid") if edge.get("sourceUid") == u else edge.get("sourceUid")
                if v and v not in seen and v in entities and not entities[v].get("isDeleted"):
                    seen.add(v); nxt.append(v)
        frontier = nxt
    nodes = []
    edges = []
    for u in seen:
        e = entities[u]
        nodes.append({"id": u, "label": e.get("title"), "type": e.get("entityType"), "description": e.get("code") or e.get("localRef")})
        for r in e.get("relations", []):
            if r.get("targetUid") in seen:
                edges.append({"from": u, "to": r.get("targetUid"), "label": r.get("type")})
    return str(entity.get("title") or ref), {"direction": "LR", "nodes": nodes, "edges": edges, "notes": "Auto-generated feature/project map from current entity relations."}, list(seen)


def cmd_diagram_create(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    require_project(root)
    related = []
    if args.spec_file:
        spec = load_json(Path(args.spec_file).resolve())
        title = args.title or str(spec.get("title") or "Project diagram")
    elif args.from_ref:
        base_title, spec, related = diagram_spec_from_entity(root, args.from_ref, args.depth)
        title = args.title or f"{base_title} - {args.kind}"
    else:
        raise ValueError("Use --spec-file or --from-ref")
    page = render_diagram_html(title, args.kind, spec)
    entity = create_document_with_file(root, title, "diagram", f"{args.kind}.html", page, summary=f"{args.kind} diagram", tags=["diagram", args.kind], fmt="html", extra_data={"diagramKind": args.kind, "relatedRef": args.from_ref})
    for uid_value in related:
        if uid_value != entity["uid"]:
            try:
                add_relation_direct(root, entity["uid"], "documents", uid_value)
            except Exception:
                pass
    build_manifest(root)
    print(json.dumps({"documentUid": entity["uid"], "path": entity["data"]["sourcePath"], "kind": args.kind}, ensure_ascii=False))
    return 0


def create_implementation_task(root: Path, title: str, related_refs: List[str]) -> str:
    task = create_import_entity(root, "task", title, {"priority": "normal", "taskType": "development", "assignee": None, "dueDate": None, "estimateHours": None, "actualHours": None}, tags=["implementation", "auto-created"])
    for uid_value in resolve_refs_to_uids(root, related_refs):
        try:
            add_relation_direct(root, task["uid"], "relates_to", uid_value)
        except Exception:
            pass
    build_manifest(root)
    return task["uid"]


def set_task_status(root: Path, uid_value: str, status: str) -> None:
    _, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    task = entities.get(uid_value)
    if not task or task.get("entityType") != "task":
        raise KeyError(f"Task not found: {uid_value}")
    plan_transition_entity(root, uid_value, task, status, force=False)


def cmd_implement(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    require_project(root)
    policy = load_documentation_policy(root)
    _, plan = find_workplan(root, args.plan)
    if policy.get("implementationRequiresApprovedPlan", True) and plan.get("status") != "approved":
        raise ValueError(f"Implementation requires an approved WorkPlan; status={plan.get('status')}")
    related = list(plan.get("related") or []) + list(args.related or [])
    _, registry, _, _ = load_controls(root)
    entities, _ = collect_entities(root, registry)
    related_uids = resolve_refs_to_uids(root, related)
    doc_uids = [u for u in related_uids if entities.get(u, {}).get("entityType") == "document"]
    if policy.get("implementationRequiresDocumentation", True) and not doc_uids and not args.allow_undocumented:
        raise ValueError("Documentation-first policy requires at least one related Document on the WorkPlan/implement command. Add/update documentation before implementation, or explicitly use --allow-undocumented after review.")
    task_uids = []
    for ref in list(args.task or []) + related:
        try:
            uid_value, e = resolve_entity_ref(root, str(ref), entities=entities, registry=registry)
            if e.get("entityType") == "task" and uid_value not in task_uids:
                task_uids.append(uid_value)
        except Exception:
            pass
    if not task_uids and policy.get("autoCreateImplementationTask", True):
        task_uids = [create_implementation_task(root, f"Implement: {plan.get('title') or plan.get('planId')}", related)]
    original = {}
    for uid_value in task_uids:
        entities, _ = collect_entities(root, registry)
        original[uid_value] = entities[uid_value].get("status")
        if original[uid_value] in {"todo", "blocked"}:
            set_task_status(root, uid_value, "in_progress")
    build_manifest(root)
    ns = copy.copy(args)
    ns.via_implement = True
    ns.force_stale = args.force_stale
    ns.actor = args.actor
    ns.reason = args.reason or "Explicit implementation command"
    try:
        rc = cmd_plan_execute(ns)
        for uid_value in task_uids:
            entities, _ = collect_entities(root, registry)
            status = entities[uid_value].get("status")
            if status != "done":
                if status != "in_progress":
                    plan_transition_entity(root, uid_value, entities[uid_value], "in_progress", force=True)
                    entities, _ = collect_entities(root, registry)
                plan_transition_entity(root, uid_value, entities[uid_value], "done", force=False)
        if args.request:
            rp = request_path(root, args.request)
            if rp.is_file():
                r = load_json(rp)
                r["status"] = "completed"
                r["updatedAt"] = now_utc()
                r["closedAt"] = r["updatedAt"]
                r["workPlanIds"] = sorted(set((r.get("workPlanIds") or []) + [str(plan.get("planId"))]))
                r["taskUids"] = sorted(set((r.get("taskUids") or []) + task_uids))
                r["resultSummary"] = args.reason or "Implementation completed"
                save_json(rp, r)
        build_manifest(root)
        print(json.dumps({"implemented": True, "planId": plan.get("planId"), "taskUids": task_uids, "taskStatus": "done"}, ensure_ascii=False))
        return rc
    except Exception:
        for uid_value, status in original.items():
            try:
                entities, _ = collect_entities(root, registry)
                task = entities.get(uid_value)
                if task and task.get("status") != status:
                    plan_transition_entity(root, uid_value, task, status, force=True)
            except Exception:
                pass
        build_manifest(root)
        raise


def add_change_meta_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", default=None, help="Actor id; prefix AI actors with ai:, e.g. ai:codex")
    parser.add_argument("--reason", default=None, help="Why this change is being made")
    parser.add_argument("--related", action="append", help="Related task/bug/change reference; repeatable")


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Manage a local-first JSON project workspace")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="Initialize a project workspace")
    s.add_argument("--path", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--timezone", default=None)
    s.add_argument("--language", default=None)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("idea-bootstrap", help="Bootstrap an empty project with a documentation-first idea pack")
    s.add_argument("--project", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--idea", required=True)
    s.add_argument("--actor", default="user")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_idea_bootstrap)

    s = sub.add_parser("request-capture", help="Trace every project-scoped question/request and create documentation first when configured")
    s.add_argument("--project", required=True)
    s.add_argument("--kind", choices=["question","idea","request","requirement","change"], default="request")
    s.add_argument("--text", required=True)
    s.add_argument("--title", default=None)
    s.add_argument("--actor", default="user")
    s.add_argument("--related", action="append")
    s.add_argument("--no-document", action="store_true")
    s.set_defaults(func=cmd_request_capture)

    s = sub.add_parser("request-list", help="List local interaction/request trace records")
    s.add_argument("--project", required=True)
    s.add_argument("--status", default=None)
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_request_list)

    s = sub.add_parser("request-show", help="Show one request trace record")
    s.add_argument("--project", required=True)
    s.add_argument("--request", required=True)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_request_show)

    s = sub.add_parser("request-close", help="Close a traced question/request with a result summary")
    s.add_argument("--project", required=True)
    s.add_argument("--request", required=True)
    s.add_argument("--summary", required=True)
    s.set_defaults(func=cmd_request_close)

    s = sub.add_parser("graph-build", help="Regenerate the standalone local HTML knowledge graph")
    s.add_argument("--project", required=True)
    s.set_defaults(func=cmd_graph_build)

    s = sub.add_parser("diagram-create", help="Create an HTML diagram document from a JSON flow spec or an existing entity graph")
    s.add_argument("--project", required=True)
    s.add_argument("--kind", choices=["flow","business-flow","feature-flow","feature-map","architecture","sequence"], default="flow")
    s.add_argument("--title", default=None)
    g=s.add_mutually_exclusive_group(required=True)
    g.add_argument("--spec-file", default=None)
    g.add_argument("--from-ref", default=None)
    s.add_argument("--depth", type=int, default=2)
    s.set_defaults(func=cmd_diagram_create)

    s = sub.add_parser("implement", help="Explicit documentation-gated WorkPlan implementation command with automatic task status updates")
    s.add_argument("--project", required=True)
    s.add_argument("--plan", required=True)
    s.add_argument("--request", default=None)
    s.add_argument("--task", action="append")
    s.add_argument("--related", action="append")
    s.add_argument("--allow-undocumented", action="store_true")
    s.add_argument("--force-stale", action="store_true")
    s.add_argument("--actor", default="ai:agent")
    s.add_argument("--reason", default=None)
    s.set_defaults(func=cmd_implement)

    s = sub.add_parser("create", help="Create a local entity")
    s.add_argument("--project", required=True)
    s.add_argument("--type", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--data-json", default=None, help="JSON object merged into entity.data")
    add_change_meta_args(s)
    s.set_defaults(func=cmd_create)

    s = sub.add_parser("link", help="Create a validated relation")
    s.add_argument("--project", required=True)
    s.add_argument("--source-uid", required=True)
    s.add_argument("--relation", required=True)
    s.add_argument("--target-uid", required=True)
    add_change_meta_args(s)
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("unlink", help="Remove a relation")
    s.add_argument("--project", required=True)
    s.add_argument("--source-uid", required=True)
    s.add_argument("--relation", required=True)
    s.add_argument("--target-uid", required=True)
    add_change_meta_args(s)
    s.set_defaults(func=cmd_unlink)

    s = sub.add_parser("update", help="Update title, data, or tags without changing identity/status")
    s.add_argument("--project", required=True)
    s.add_argument("--uid", required=True)
    s.add_argument("--title", default=None)
    s.add_argument("--data-json", default=None)
    s.add_argument("--tags-json", default=None)
    add_change_meta_args(s)
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("transition", help="Change status using the configured lifecycle")
    s.add_argument("--project", required=True)
    s.add_argument("--uid", required=True)
    s.add_argument("--status", required=True)
    s.add_argument("--force", action="store_true", help="Bypass lifecycle transition rule but still require a configured target status")
    add_change_meta_args(s)
    s.set_defaults(func=cmd_transition)

    s = sub.add_parser("query", help="Query local entities with field expressions and optional graph scope")
    s.add_argument("--project", required=True)
    s.add_argument("--expr", default=None, help="Boolean expression, e.g. 'type=bug AND status!=closed AND data.severity=critical'")
    s.add_argument("--type", action="append")
    s.add_argument("--status", action="append")
    s.add_argument("--tag", action="append")
    s.add_argument("--text", default=None, help="Backward-compatible indexed full-text filter")
    s.add_argument("--related-to", default=None, help="Limit results to graph neighbors of uid/id/code/localRef/title")
    s.add_argument("--relation", action="append", help="Limit graph traversal to relation type; repeatable")
    s.add_argument("--direction", choices=["incoming", "outgoing", "both"], default="both")
    s.add_argument("--max-depth", type=int, default=1)
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--include-deleted", action="store_true")
    s.add_argument("--no-reindex", action="store_true", help="Fail instead of rebuilding a stale search index when --text is used")
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_query)

    s = sub.add_parser("reindex", help="Rebuild manifest, relation/entity indexes, and local full-text search index")
    s.add_argument("--project", required=True)
    s.set_defaults(func=cmd_reindex)

    s = sub.add_parser("search", help="Search indexed entity metadata, data, and referenced UTF-8 content")
    s.add_argument("--project", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--type", action="append")
    s.add_argument("--status", action="append")
    s.add_argument("--tag", action="append")
    s.add_argument("--include-deleted", action="store_true")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--no-reindex", action="store_true")
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("view-list", help="List saved local project views")
    s.add_argument("--project", required=True)
    s.set_defaults(func=cmd_view_list)

    s = sub.add_parser("view-show", help="Show one saved view definition")
    s.add_argument("--project", required=True)
    s.add_argument("--view", required=True)
    s.set_defaults(func=cmd_view_show)

    s = sub.add_parser("view-run", help="Run a saved view against the current local workspace")
    s.add_argument("--project", required=True)
    s.add_argument("--view", required=True)
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_view_run)

    s = sub.add_parser("doctor", help="Diagnose workspace/index/view/link problems and optionally apply deterministic repairs")
    s.add_argument("--project", required=True)
    s.add_argument("--fix", action="store_true", help="Apply safe derived-cache/folder repairs")
    s.add_argument("--fix-level", choices=["safe", "semantic"], default="safe", help="semantic additionally repairs deterministic relation targetType mismatches")
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("describe", help="Describe configured entity types for agents or generic clients")
    s.add_argument("--project", required=True)
    s.add_argument("--type", default=None)
    s.set_defaults(func=cmd_describe)

    s = sub.add_parser("quality", help="Generate traceability/coverage quality report")
    s.add_argument("--project", required=True)
    s.add_argument("--fail-on", action="append", choices=["info", "warning", "error"], help="Return non-zero when a finding has this severity")
    s.set_defaults(func=cmd_quality)

    s = sub.add_parser("restore", help="Restore a soft-deleted entity")
    s.add_argument("--project", required=True)
    s.add_argument("--uid", required=True)
    add_change_meta_args(s)
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("delete", help="Soft-delete an entity")
    s.add_argument("--project", required=True)
    s.add_argument("--uid", required=True)
    add_change_meta_args(s)
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("manifest", help="Rebuild manifest and indexes")
    s.add_argument("--project", required=True)
    s.set_defaults(func=cmd_manifest)

    s = sub.add_parser("validate", help="Validate project structure and invariants")
    s.add_argument("--project", required=True)
    s.set_defaults(func=cmd_validate)


    s = sub.add_parser("trace", help="Traverse the configured project relationship graph from one entity")
    s.add_argument("--project", required=True)
    s.add_argument("--ref", required=True, help="uid, id, code, localRef, or unique exact title")
    s.add_argument("--profile", default="context", help="Traversal profile from .pm/intelligence.json")
    s.add_argument("--max-depth", type=int, default=None)
    s.add_argument("--direction", choices=["outgoing", "incoming", "both"], default=None)
    s.add_argument("--max-entities", type=int, default=None)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_trace)

    s = sub.add_parser("context", help="Build a bounded AI context pack around one entity")
    s.add_argument("--project", required=True)
    s.add_argument("--ref", required=True)
    s.add_argument("--profile", default="context")
    s.add_argument("--detail", choices=["summary", "full"], default=None)
    s.add_argument("--max-depth", type=int, default=None)
    s.add_argument("--max-entities", type=int, default=None)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_context)

    s = sub.add_parser("impact", help="Find direct and indirect entities connected through the configured impact profile")
    s.add_argument("--project", required=True)
    s.add_argument("--ref", required=True)
    s.add_argument("--max-depth", type=int, default=None)
    s.add_argument("--max-entities", type=int, default=None)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_impact)

    s = sub.add_parser("coverage", help="Report configured quality/coverage rules globally or for one entity")
    s.add_argument("--project", required=True)
    s.add_argument("--ref", default=None)
    s.add_argument("--category", default=None, help="Quality rule category; defaults to test and automation coverage")
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_coverage)

    s = sub.add_parser("orphans", help="List non-deleted entities with no valid incoming or outgoing relations")
    s.add_argument("--project", required=True)
    s.add_argument("--output", default=None)
    s.add_argument("--fail-if-found", action="store_true")
    s.set_defaults(func=cmd_orphans)

    s = sub.add_parser("broken-links", help="Find missing/deleted/mismatched relation targets and mapping violations")
    s.add_argument("--project", required=True)
    s.add_argument("--output", default=None)
    s.add_argument("--fail-if-found", action="store_true")
    s.set_defaults(func=cmd_broken_links)

    s = sub.add_parser("missing-tests", help="List failures from quality rules categorized as test-coverage")
    s.add_argument("--project", required=True)
    s.add_argument("--ref", default=None)
    s.add_argument("--output", default=None)
    s.add_argument("--fail-if-found", action="store_true")
    s.set_defaults(func=cmd_missing_tests)

    s = sub.add_parser("health", help="Summarize validation, quality, graph, identity, and sync health without inventing a score")
    s.add_argument("--project", required=True)
    s.add_argument("--output", default=None)
    s.add_argument("--fail-on-validation", action="store_true")
    s.set_defaults(func=cmd_health)

    s = sub.add_parser("plan-context", help="Build bounded planning context and optional impact packs for a natural-language request")
    s.add_argument("--project", required=True)
    s.add_argument("--request", required=True)
    s.add_argument("--ref", action="append", help="Known entity reference relevant to the request; repeatable")
    s.add_argument("--profile", default="context")
    s.add_argument("--max-depth", type=int, default=None)
    s.add_argument("--impact-depth", type=int, default=None)
    s.add_argument("--max-entities", type=int, default=None)
    s.add_argument("--include-impact", action="store_true")
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_plan_context)

    s = sub.add_parser("plan-create", help="Create a draft WorkPlan from an AI/user-authored JSON plan spec")
    s.add_argument("--project", required=True)
    s.add_argument("--request", default=None)
    s.add_argument("--title", default=None)
    group = s.add_mutually_exclusive_group()
    group.add_argument("--spec-json", default=None, help="JSON object containing steps/assumptions/risks/acceptanceCriteria")
    group.add_argument("--spec-file", default=None, help="Path to a JSON plan spec")
    s.add_argument("--context-ref", action="append")
    s.add_argument("--actor", default="ai:agent")
    s.add_argument("--reason", default=None)
    s.add_argument("--related", action="append")
    s.set_defaults(func=cmd_plan_create)

    s = sub.add_parser("plans", help="List WorkPlans")
    s.add_argument("--project", required=True)
    s.add_argument("--status", action="append")
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_plans)

    s = sub.add_parser("plan-show", help="Show one WorkPlan plus current stale-context findings")
    s.add_argument("--project", required=True)
    s.add_argument("--plan", required=True)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_plan_show)

    s = sub.add_parser("plan-validate", help="Validate one WorkPlan and optionally fail on stale context")
    s.add_argument("--project", required=True)
    s.add_argument("--plan", required=True)
    s.add_argument("--fail-on-stale", action="store_true")
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_plan_validate)

    s = sub.add_parser("plan-amend", help="Amend a draft/failed WorkPlan from a partial JSON spec and invalidate old base hashes")
    s.add_argument("--project", required=True)
    s.add_argument("--plan", required=True)
    group = s.add_mutually_exclusive_group(required=True)
    group.add_argument("--spec-json", default=None)
    group.add_argument("--spec-file", default=None)
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.set_defaults(func=cmd_plan_amend)

    s = sub.add_parser("plan-submit", help="Freeze base entity/definition hashes and submit a draft WorkPlan for review")
    s.add_argument("--project", required=True)
    s.add_argument("--plan", required=True)
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.set_defaults(func=cmd_plan_submit)

    s = sub.add_parser("plan-refresh", help="Refresh base hashes after project drift and return the WorkPlan to draft for re-review")
    s.add_argument("--project", required=True)
    s.add_argument("--plan", required=True)
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.set_defaults(func=cmd_plan_refresh)

    s = sub.add_parser("plan-approve", help="Approve a pending WorkPlan only if its reviewed context is still current")
    s.add_argument("--project", required=True)
    s.add_argument("--plan", required=True)
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.set_defaults(func=cmd_plan_approve)

    s = sub.add_parser("plan-reject", help="Reject a pending WorkPlan without changing project entities")
    s.add_argument("--project", required=True)
    s.add_argument("--plan", required=True)
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.set_defaults(func=cmd_plan_reject)

    s = sub.add_parser("plan-cancel", help="Cancel a non-terminal WorkPlan")
    s.add_argument("--project", required=True)
    s.add_argument("--plan", required=True)
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.set_defaults(func=cmd_plan_cancel)

    s = sub.add_parser("plan-execute", help="Execute an approved WorkPlan atomically and create one linked ChangeSet")
    s.add_argument("--project", required=True)
    s.add_argument("--plan", required=True)
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.add_argument("--force-stale", action="store_true", help="Execute despite stale base hashes only after explicit human review")
    s.set_defaults(func=cmd_plan_execute)

    s = sub.add_parser("scan-source", help="Build a disposable local source-code index with symbols, imports, hashes, and entity mapping evidence")
    s.add_argument("--project", required=True)
    s.add_argument("--root", action="append", help="Project-relative source root override; repeatable")
    s.add_argument("--full", action="store_true", help="Print the complete source index instead of summary")
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_scan_source)

    s = sub.add_parser("source-map", help="Show source-to-entity mapping for one file or entity")
    s.add_argument("--project", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--path", default=None, help="Project-relative source path")
    g.add_argument("--ref", default=None, help="Entity uid/id/code/localRef/title")
    s.add_argument("--no-reindex", action="store_true")
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_source_map)

    s = sub.add_parser("git-status", help="Show Git working-tree changes mapped to project entities")
    s.add_argument("--project", required=True)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_git_status)

    s = sub.add_parser("changes-since-commit", help="Show changed files and mapped entities between two Git refs")
    s.add_argument("--project", required=True)
    s.add_argument("--from-ref", required=True)
    s.add_argument("--to-ref", default="HEAD")
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_changes_since_commit)

    s = sub.add_parser("git-impact", help="Map Git file changes to direct project entities and graph-based potential impact")
    s.add_argument("--project", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--commit", default=None)
    g.add_argument("--from-ref", default=None)
    g.add_argument("--working-tree", action="store_true")
    s.add_argument("--to-ref", default="HEAD")
    s.add_argument("--max-depth", type=int, default=None)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_git_impact)

    s = sub.add_parser("commit-context", help="Build entity context packs for entities mapped to one Git commit")
    s.add_argument("--project", required=True)
    s.add_argument("--commit", required=True)
    s.add_argument("--detail", choices=["summary", "full"], default="summary")
    s.add_argument("--max-depth", type=int, default=2)
    s.add_argument("--max-entities", type=int, default=20)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_commit_context)

    s = sub.add_parser("git-changeset", help="Create a non-reversible evidence ChangeSet for one Git commit")
    s.add_argument("--project", required=True)
    s.add_argument("--commit", required=True)
    add_change_meta_args(s)
    s.set_defaults(func=cmd_git_changeset)

    s = sub.add_parser("git-link", help="Link an existing ChangeSet to a Git commit")
    s.add_argument("--project", required=True)
    s.add_argument("--change-set", required=True)
    s.add_argument("--commit", required=True)
    s.add_argument("--actor", default=None)
    s.set_defaults(func=cmd_git_link)

    s = sub.add_parser("import-markdown", help="Import a Markdown file as a local document entity")
    s.add_argument("--project", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--title", default=None)
    s.add_argument("--document-type", default="markdown")
    s.add_argument("--tag", action="append")
    add_change_meta_args(s)
    s.set_defaults(func=cmd_import_markdown)

    s = sub.add_parser("scan-openapi", help="Inspect OpenAPI JSON/YAML and optionally create API entities")
    s.add_argument("--project", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--apply", action="store_true")
    s.add_argument("--output", default=None)
    add_change_meta_args(s)
    s.set_defaults(func=cmd_scan_openapi)

    s = sub.add_parser("scan-database", help="Inspect SQL DDL and optionally create database-table entities")
    s.add_argument("--project", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--engine", default=None)
    s.add_argument("--apply", action="store_true")
    s.add_argument("--output", default=None)
    add_change_meta_args(s)
    s.set_defaults(func=cmd_scan_database)

    s = sub.add_parser("scan-playwright", help="Inspect Playwright tests and optionally create test-script entities")
    s.add_argument("--project", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--apply", action="store_true")
    s.add_argument("--output", default=None)
    add_change_meta_args(s)
    s.set_defaults(func=cmd_scan_playwright)

    s = sub.add_parser("code-deps", help="Traverse local source import dependencies/dependents from one or more indexed files")
    s.add_argument("--project", required=True)
    s.add_argument("--path", action="append", required=True, help="Project-relative indexed source path; repeatable")
    s.add_argument("--direction", choices=["dependencies", "dependents", "both"], default="both")
    s.add_argument("--max-depth", type=int, default=3)
    s.add_argument("--rebuild", action="store_true")
    s.add_argument("--no-reindex", action="store_true")
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_code_deps)

    s = sub.add_parser("dependency-tree", help="Render a bounded nested source dependency/dependent tree")
    s.add_argument("--project", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--direction", choices=["dependencies", "dependents", "both"], default="dependencies")
    s.add_argument("--max-depth", type=int, default=4)
    s.add_argument("--max-nodes", type=int, default=200)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_dependency_tree)

    s = sub.add_parser("circular-dependencies", help="Find strongly connected cycles in the local source dependency graph")
    s.add_argument("--project", required=True)
    s.add_argument("--min-size", type=int, default=2)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_circular_dependencies)

    s = sub.add_parser("code-impact", help="Expand changed source files through code dependents, then map them into project-graph potential impact")
    s.add_argument("--project", required=True)
    g = s.add_mutually_exclusive_group(required=False)
    g.add_argument("--path", action="append", help="Project-relative changed source path; repeatable")
    g.add_argument("--commit", default=None)
    g.add_argument("--from-ref", default=None)
    g.add_argument("--working-tree", action="store_true")
    s.add_argument("--to-ref", default="HEAD")
    s.add_argument("--code-depth", type=int, default=5)
    s.add_argument("--project-depth", type=int, default=3)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_code_impact)

    s = sub.add_parser("test-selection", help="Select regression tests from source dependents plus the durable project impact graph")
    s.add_argument("--project", required=True)
    g = s.add_mutually_exclusive_group(required=False)
    g.add_argument("--path", action="append", help="Changed source path; repeatable")
    g.add_argument("--commit", default=None)
    g.add_argument("--from-ref", default=None)
    g.add_argument("--working-tree", action="store_true")
    s.add_argument("--to-ref", default="HEAD")
    s.add_argument("--code-depth", type=int, default=8)
    s.add_argument("--project-depth", type=int, default=4)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_test_selection)

    s = sub.add_parser("changed-tests", help="Convenience Git-oriented regression test selection for a commit/range/working tree")
    s.add_argument("--project", required=True)
    g = s.add_mutually_exclusive_group(required=False)
    g.add_argument("--commit", default=None)
    g.add_argument("--from-ref", default=None)
    g.add_argument("--working-tree", action="store_true")
    s.add_argument("--to-ref", default="HEAD")
    s.add_argument("--code-depth", type=int, default=8)
    s.add_argument("--project-depth", type=int, default=4)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_changed_tests)

    s = sub.add_parser("plan-from-git", help="Create a non-executable draft WorkPlan shell from commit impact and selected tests")
    s.add_argument("--project", required=True)
    s.add_argument("--commit", required=True)
    s.add_argument("--title", default=None)
    s.add_argument("--request", default=None)
    s.add_argument("--code-depth", type=int, default=5)
    s.add_argument("--project-depth", type=int, default=3)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--output", default=None)
    add_change_meta_args(s)
    s.set_defaults(func=cmd_plan_from_git)

    s = sub.add_parser("plan-from-diff", help="Create a non-executable draft WorkPlan shell from working-tree or Git range impact")
    s.add_argument("--project", required=True)
    g = s.add_mutually_exclusive_group(required=False)
    g.add_argument("--from-ref", default=None)
    g.add_argument("--working-tree", action="store_true")
    s.add_argument("--to-ref", default="HEAD")
    s.add_argument("--title", default=None)
    s.add_argument("--request", default=None)
    s.add_argument("--code-depth", type=int, default=5)
    s.add_argument("--project-depth", type=int, default=3)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--output", default=None)
    add_change_meta_args(s)
    s.set_defaults(func=cmd_plan_from_diff)

    s = sub.add_parser("bundle-export", help="Export the portable workspace as one JSON bundle")
    s.add_argument("--project", required=True)
    s.add_argument("--output", required=True)
    s.set_defaults(func=cmd_bundle_export)

    s = sub.add_parser("bundle-import", help="Import one portable JSON bundle into a workspace folder")
    s.add_argument("--bundle", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_bundle_import)

    s = sub.add_parser("sync", help="Sync through the configured single endpoint")
    s.add_argument("--project", required=True)
    s.add_argument("--entity-type", action="append", help="Limit sync to an entity type; repeatable")
    s.add_argument("--uid", action="append", help="Limit sync to a uid; repeatable")
    s.add_argument("--work-plan", action="append", help="Limit WorkPlan sync to a planId; repeatable")
    s.add_argument("--direction", choices=["bidirectional", "push", "pull"], default=None)
    s.add_argument("--force-local", action="store_true", help="Explicitly prefer local for selected conflicts")
    s.add_argument("--skip-definition", action="store_true", help="Do not include changed workspace schemas/definitions in this sync")
    s.add_argument("--dry-run", action="store_true", help="Print sync request without network access")
    add_change_meta_args(s)
    s.set_defaults(func=cmd_sync)


    s = sub.add_parser("history", help="Show audited change history globally or for one entity")
    s.add_argument("--project", required=True)
    s.add_argument("--ref", default=None, help="Optional uid/id/code/localRef/title filter")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("diff", help="Show entity and referenced-text diff from latest change, a change set, or baseline")
    s.add_argument("--project", required=True)
    s.add_argument("--ref", required=True)
    s.add_argument("--change-set", default=None)
    s.add_argument("--baseline", default=None)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("propose", help="Stage an entity merge-patch as a pending review without changing the live entity")
    s.add_argument("--project", required=True)
    s.add_argument("--ref", required=True)
    s.add_argument("--patch-json", required=True, help="JSON Merge Patch object")
    s.add_argument("--actor", default="ai:agent")
    s.add_argument("--reason", required=True)
    s.add_argument("--related", action="append")
    s.set_defaults(func=cmd_propose)

    s = sub.add_parser("reviews", help="List pending proposed change sets")
    s.add_argument("--project", required=True)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_reviews)

    s = sub.add_parser("review-show", help="Show a pending/applied/rejected change set with field diffs")
    s.add_argument("--project", required=True)
    s.add_argument("--change-set", required=True)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_review_show)

    s = sub.add_parser("review-approve", help="Approve a pending change set if its base version is still current")
    s.add_argument("--project", required=True)
    s.add_argument("--change-set", required=True)
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.set_defaults(func=cmd_review_approve)

    s = sub.add_parser("review-reject", help="Reject a pending change set without changing live project data")
    s.add_argument("--project", required=True)
    s.add_argument("--change-set", required=True)
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.set_defaults(func=cmd_review_reject)

    s = sub.add_parser("undo", help="Reverse an applied change set with stale-state protection")
    s.add_argument("--project", required=True)
    s.add_argument("--change-set", required=True)
    s.add_argument("--force", action="store_true")
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.set_defaults(func=cmd_undo)

    s = sub.add_parser("audit-scan", help="Detect direct/manual entity or referenced-file edits not made through the CLI")
    s.add_argument("--project", required=True)
    s.add_argument("--initialize", action="store_true", help="Capture current state without creating a change set")
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.set_defaults(func=cmd_audit_scan)

    s = sub.add_parser("baseline-create", help="Create a named immutable-style project snapshot for releases or milestones")
    s.add_argument("--project", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--label", default=None)
    s.add_argument("--release", default=None)
    s.add_argument("--actor", default=None)
    s.add_argument("--reason", default=None)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_baseline_create)

    s = sub.add_parser("baseline-list", help="List project baselines")
    s.add_argument("--project", required=True)
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_baseline_list)

    s = sub.add_parser("baseline-compare", help="Compare two baselines, or a baseline with current workspace state")
    s.add_argument("--project", required=True)
    s.add_argument("--from", dest="from_baseline", required=True)
    s.add_argument("--to", dest="to_baseline", default="current")
    s.add_argument("--output", default=None)
    s.set_defaults(func=cmd_baseline_compare)

    return p


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()
    audited_commands = {"create", "link", "unlink", "update", "transition", "restore", "delete", "sync", "import-markdown", "scan-openapi", "scan-database", "scan-playwright"}
    before_records = None
    audit_root = None
    try:
        if args.command in audited_commands and getattr(args, "project", None):
            audit_root = Path(args.project).resolve()
            if (audit_root / "project.json").is_file():
                before_records = capture_workspace_records(audit_root)
        rc = int(args.func(args) or 0)
        if rc == 0 and before_records is not None and audit_root is not None:
            audit_mutation(audit_root, before_records, args.command, args)
        return rc
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
