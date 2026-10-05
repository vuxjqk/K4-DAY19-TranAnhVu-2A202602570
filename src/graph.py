"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# KG_ONTOLOGY=hint rebuilds the suggested ontology (baseline); default is the custom one (report/ONTOLOGY.md).
ONTOLOGY = os.getenv("KG_ONTOLOGY", "custom")

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    target = normalize(name or "")
    if not target:
        return None
    by_norm: dict[str, str] = {}
    for item in known:
        by_norm.setdefault(normalize(item), item)
    if target in by_norm:
        return by_norm[target]
    close = difflib.get_close_matches(target, list(by_norm), n=1, cutoff=0.8)
    return by_norm[close[0]] if close else None

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# Custom ontology (report/ONTOLOGY.md): extraction helpers
# ----------------------------------------------------------------------------------------------

# Surface forms seen in news -> canonical law name. Generic words are not a substance (None).
SUBSTANCE_ALIASES = {
    "heroin": "Heroine", "hêrôin": "Heroine", "cocain": "Cocaine", "côcain": "Cocaine",
    "ma túy đá": "Methamphetamine", "methamphetamin": "Methamphetamine", "meth": "Methamphetamine",
    "thuốc lắc": "MDMA", "ecstasy": "MDMA", "ketamin": "Ketamine", "ke": "Ketamine",
    "amphetamin": "Amphetamine", "cỏ mỹ": "cần sa", "bồ đà": "cần sa", "marijuana": "cần sa",
    "ma túy": None, "chất ma túy": None, "ma túy tổng hợp": None, "chất ma túy tổng hợp": None,
    "ma túy các loại": None, "không rõ": None,
}
GENERIC_SUBSTANCE = {k for k, v in SUBSTANCE_ALIASES.items() if v is None}
STAGES = ["bắt giữ", "khởi tố", "truy tố", "xét xử sơ thẩm", "xét xử phúc thẩm", "khác"]
POINT_START = re.compile(r"^([a-zđ])\)\s", re.MULTILINE)
WEIGHT_RANGE = re.compile(r"(?:từ )?([\d.,]+) (gam|kilôgam)(?: đến dưới ([\d.,]+) (gam|kilôgam)| trở lên)")
TERM_DEF = re.compile(r"^\d+\.\s+(.{3,80}?) là ")

PLACEHOLDERS = {"chuỗi rỗng", "không rõ", "không có", "n/a", "null", "none", "?"}

def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text or "")

def _clean(value: Any) -> str:
    """LLM sometimes copies the prompt literally ('chuỗi rỗng') or answers 'không rõ': treat as empty."""
    text = re.sub(r"\s+", " ", _nfc(value if isinstance(value, str) else "")).strip()
    return "" if text.lower() in PLACEHOLDERS else text

def normalize_substance(name: str) -> str:
    name = re.sub(r"\s+", " ", _nfc(name).strip().lower())
    return name.replace("tuý", "túy").replace("hoá", "hóa")

def person_key(name: str) -> str:
    return re.sub(r"\s+", " ", _nfc(name).strip().lower())

def canonical_substance(name: str) -> str | None:
    """'thuốc lắc' -> 'MDMA', 'ketamin' -> 'Ketamine', 'ma túy' -> None (not a specific substance)."""
    norm = normalize_substance(name)
    inner = re.findall(r"\(([^)]*)\)", norm)
    norm = re.sub(r"\s*\([^)]*\)", "", norm).strip()
    if inner and (not norm or norm in GENERIC_SUBSTANCE or norm not in SUBSTANCE_ALIASES
                  and not link_entity(norm, SUBSTANCES, normalize=normalize_substance)):
        norm = inner[0].strip()      # 'ma túy (ketamine)' -> 'ketamine'
    if not norm or norm in GENERIC_SUBSTANCE:
        return None
    if norm in SUBSTANCE_ALIASES:
        return SUBSTANCE_ALIASES[norm]
    return link_entity(norm, SUBSTANCES, normalize=normalize_substance) or norm

def _number(text: str) -> float:
    """Vietnamese number: '9,6' -> 9.6, '1.200' -> 1200, '4.3' -> 4.3."""
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")
    return float(text.replace(",", "."))

def parse_grams(amount: str) -> float | None:
    """'hơn 9,6kg' -> 9600.0, '0,686g' -> 0.686, '5 viên' -> None."""
    m = re.search(r"(\d[\d.,]*)\s*(kg|kilôgam|kilogam|ki-lô-gam|tấn|mg|miligam|g|gam|gram)\b", _nfc(amount).lower())
    if not m:
        return None
    factor = {"kg": 1000, "kilôgam": 1000, "kilogam": 1000, "ki-lô-gam": 1000, "tấn": 1_000_000,
              "mg": 0.001, "miligam": 0.001}.get(m.group(2), 1)
    try:
        return _number(m.group(1).rstrip(".,")) * factor
    except ValueError:
        return None

def penalty_severity(penalty: str) -> float:
    """Comparable weight of a clause's penalty: tử hình 100 > chung thân 50 > max years of prison > 0."""
    if "tử hình" in penalty:
        return 100
    if "chung thân" in penalty:
        return 50
    index = penalty.find("tù")
    years = [int(y) for y in re.findall(r"(\d+) năm", penalty[index:])] if index >= 0 else []
    return max(years, default=0)

def parse_law_article_custom(doc: Document) -> dict[str, Any]:
    """parse_law_article + severity per clause, weight thresholds per point, defined terms (regex only)."""
    article = parse_law_article(doc)
    for clause in article["clauses"]:
        clause["severity"] = penalty_severity(clause["penalty"])
        thresholds, starts = [], list(POINT_START.finditer(clause["text"]))
        for i, start in enumerate(starts):
            point = clause["text"][start.start():starts[i + 1].start() if i + 1 < len(starts) else None]
            weight = WEIGHT_RANGE.search(point)
            if not weight:
                continue
            unit = lambda u: 1000 if u == "kilôgam" else 1
            low = _number(weight.group(1)) * unit(weight.group(2))
            high = _number(weight.group(3)) * unit(weight.group(4)) if weight.group(3) else None
            for substance in find_substances(point):
                thresholds.append({"substance": substance, "point": start.group(1), "min_g": low, "max_g": high,
                                   "text": point.strip()})
        clause["thresholds"] = thresholds
        with_threshold = {t["substance"] for t in thresholds}
        clause["mentions"] = [s for s in clause["substances"] if s not in with_threshold]
    article["terms"] = []
    if "Giải thích từ ngữ" in article["title"]:
        for clause in article["clauses"]:
            term = TERM_DEF.match(clause["text"].replace("\n", " "))
            if term:
                article["terms"].append({"name": term.group(1).strip(), "clause_id": clause["id"],
                                         "definition": re.sub(r"\s+", " ", clause["text"])})
    return article

CUSTOM_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài, không suy đoán. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt, nêu hành vi, chất và khối lượng",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "stage": "giai đoạn tố tụng MỚI NHẤT bài nói tới, một trong: {stages}",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất cụ thể, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp (thuốc lắc = MDMA, ma túy đá = Methamphetamine, ketamin = Ketamine)",
                  "amount": "khối lượng nguyên văn trong bài, ví dụ: hơn 9,6kg; chuỗi rỗng nếu không có"}}],
  "people": [{{"name": "họ tên đầy đủ", "aliases": ["biệt danh nếu có"], "role": "bị cáo|bị can|nghi phạm|người liên quan",
               "charge": "tội danh của RIÊNG người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án của RIÊNG người này nếu có, ví dụ: tử hình, 36 tháng tù"}}]
}}]}}
Quy tắc: mỗi vụ án riêng là một phần tử; không đưa cán bộ, công an, luật sư vào "people";
không ghi "ma túy" chung chung vào "substances". Bài không nói về vụ việc cụ thể thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases_custom(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM -> JSON, then every name is re-linked in code: crimes, substances (+grams), people (key)."""
    prompt = CUSTOM_EXTRACTION_PROMPT.format(
        stages="|".join(STAGES), crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for index, case in enumerate(cases):
        case["id"] = f"{doc.id}#{index}"
        for field in ("name", "summary", "date", "location"):
            case[field] = _clean(case.get(field))
        case["stage"] = case.get("stage") if case.get("stage") in STAGES else "khác"
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        substances = {}
        for s in case.get("substances", []):
            name = canonical_substance(_clean(s.get("name")))
            if name and name not in substances:
                amount = _clean(s.get("amount"))
                substances[name] = {"name": name, "raw": _clean(s.get("name")), "amount": amount,
                                    "grams": parse_grams(amount)}
        case["substances"] = list(substances.values())
        people = {}
        for p in case.get("people", []):
            name = _clean(p.get("name"))
            if name and person_key(name) not in people:
                people[person_key(name)] = {
                    "key": person_key(name), "name": name,
                    "aliases": [a for a in (_clean(x) for x in p.get("aliases") or []) if a],
                    "role": _clean(p.get("role")), "sentence": _clean(p.get("sentence")),
                    "charge": link_entity(p.get("charge") or "", known_crimes) or "",
                }
        case["people"] = list(people.values())
    return cases

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- custom ontology: writes

    def custom_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Term", "name"),
                           ("Case", "id"), ("Substance", "name"), ("Person", "key"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article_custom(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.severity = clause.severity,
                  cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (t IN clause.thresholds | MERGE (sub:Substance {name: t.substance})
                MERGE (cl)-[r:THRESHOLD {point: t.point}]->(sub) SET r.min_g = t.min_g, r.max_g = t.max_g, r.text = t.text)
            FOREACH (s IN clause.mentions | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **{k: v for k, v in article.items() if k != "terms"},
        )
        self.run(
            """
            UNWIND $terms AS t
            MATCH (cl:Clause {id: t.clause_id})
            MERGE (term:Term {name: t.name}) SET term.definition = t.definition, term.doc_id = $doc_id
            MERGE (term)-[:DEFINED_IN]->(cl)
            """,
            terms=article["terms"], doc_id=article["doc_id"],
        )

    def add_news_case_custom(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {id: $id})
              SET k.name = $name, k.summary = $summary, k.date = $date, k.stage = $stage,
                  k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name})
                SET sub.aliases = CASE WHEN s.raw = s.name OR s.raw IN coalesce(sub.aliases, []) THEN coalesce(sub.aliases, [])
                                       ELSE coalesce(sub.aliases, []) + s.raw END
                MERGE (k)-[r:INVOLVES]->(sub) SET r.amount = s.amount, r.grams = s.grams)
            FOREACH (p IN $people | MERGE (person:Person {key: p.key})
                ON CREATE SET person.name = p.name
                SET person.aliases = coalesce(person.aliases, []) + [a IN p.aliases WHERE NOT a IN coalesce(person.aliases, [])]
                MERGE (person)-[r:INVOLVED_IN]->(k)
                SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence, r.stage = $stage)
            """,
            id=case["id"], name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), stage=case["stage"],
            location=case.get("location") or "", charges=case["charges"], people=case["people"],
            substances=case["substances"], doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + 1 hop, then the legal basis of every case reached."""
        if ONTOLOGY == "hint":
            return self._context_hint(question, doc_ids, max_facts)
        return self._context_custom(question, doc_ids, max_facts)

    def _context_hint(self, question: str, doc_ids: list[str], max_facts: int) -> list[str]:
        seed_ids, facts = self.seed_facts(question, doc_ids, limit=max_facts // 2)
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary
            """,
            ids=seed_ids,
        )
        facts += [f"Vụ việc '{c['name']}': {c['summary']}" for c in cases]
        clauses = self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE elementId(k) IN $case_ids
              AND (cl.number = 1 OR EXISTS { (k)-[:INVOLVES]->(:Substance)<-[:MENTIONS]-(cl) })
            RETURN DISTINCT a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            UNION
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE any(n IN $articles WHERE a.id STARTS WITH 'Điều ' + n + ' ')
              AND (cl.number = 1 OR EXISTS { (cl)-[:MENTIONS]->(s:Substance) WHERE s.name IN $subs })
            RETURN DISTINCT a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            """,
            case_ids=[c["id"] for c in cases], articles=re.findall(r"[Đđ]iều (\d+)", question),
            subs=find_substances(question),
        )
        facts += [f"[{c['article']} - {c['title']}] khoản {c['number']}: {c['text']}" for c in clauses]
        return facts[:max_facts]

    def _context_custom(self, question: str, doc_ids: list[str], max_facts: int) -> list[str]:
        """Seeds -> cases -> legal basis: clause 1, the article's heaviest clause, and the clause whose
        weight THRESHOLD contains the amount the case INVOLVES. Plus defined terms and named articles."""
        seed_ids, edge_facts = self.seed_facts(question, doc_ids, skip_labels=("Clause", "Term"), limit=20)
        seeds = self.run("MATCH (n) WHERE elementId(n) IN $ids RETURN elementId(n) AS id, labels(n)[0] AS label",
                         ids=seed_ids)
        by_label: dict[str, list[str]] = {}
        for s in seeds:
            by_label.setdefault(s["label"], []).append(s["id"])

        # 1. Which cases. Named people win; otherwise the retrieved articles; a substance in the question
        #    (aggregation: "vụ nào liên quan MDMA") pulls every case that INVOLVES it.
        person_cases = [r["id"] for r in self.run(
            "MATCH (p:Person)-[:INVOLVED_IN]->(k:Case) WHERE elementId(p) IN $ids RETURN DISTINCT elementId(k) AS id",
            ids=by_label.get("Person", []))]
        doc_cases = by_label.get("Case", [])
        focal = person_cases or doc_cases
        others = [] if person_cases else [r["id"] for r in self.run(
            "MATCH (s:Substance)<-[:INVOLVES]-(k:Case) WHERE elementId(s) IN $ids RETURN DISTINCT elementId(k) AS id",
            ids=by_label.get("Substance", []))]
        case_ids = list(dict.fromkeys(focal + doc_cases + others))

        facts: list[str] = []
        for c in self.run(
            """
            MATCH (k:Case) WHERE elementId(k) IN $ids
            OPTIONAL MATCH (k)-[:CHARGED_WITH]->(c:Crime)
            WITH k, collect(DISTINCT c.name) AS crimes
            OPTIONAL MATCH (k)-[r:INVOLVES]->(s:Substance)
            WITH k, crimes, collect(DISTINCT s.name + CASE WHEN coalesce(r.amount, '') <> ''
                                                        THEN ' (' + r.amount + ')' ELSE '' END) AS subs
            OPTIONAL MATCH (p:Person)-[ri:INVOLVED_IN]->(k)
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary, k.stage AS stage, k.date AS date,
                   k.doc_id AS doc_id, crimes, subs,
                   [x IN collect({name: p.name, aliases: p.aliases, role: ri.role, charge: ri.charge,
                                  sentence: ri.sentence}) WHERE x.name IS NOT NULL] AS people
            """,
            ids=case_ids,
        ):
            facts.append(f"Vụ việc '{c['name']}' [{c['doc_id']}; giai đoạn: {c['stage']}; ngày: {c['date'] or '?'}]: "
                         f"{c['summary']} Tội danh: {', '.join(c['crimes']) or 'chưa rõ'}. "
                         f"Chất: {', '.join(c['subs']) or 'không rõ'}.")
            if c["id"] in focal:
                for p in c["people"]:
                    alias = f" (biệt danh: {', '.join(p['aliases'])})" if p["aliases"] else ""
                    facts.append(f"Người '{p['name']}'{alias} trong vụ '{c['name']}': vai trò {p['role'] or '?'}; "
                                 f"tội: {p['charge'] or 'chưa rõ'}; mức án: {p['sentence'] or 'chưa có'}")

        # 2. Legal basis of the focal cases, through the bridge Crime (+ Substance thresholds).
        for r in self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE elementId(k) IN $ids
            CALL (a) { MATCH (a)-[:HAS_CLAUSE]->(x:Clause) RETURN max(x.severity) AS top }
            OPTIONAL MATCH (k)-[inv:INVOLVES]->(s:Substance)<-[t:THRESHOLD]-(cl)
              WHERE inv.grams IS NOT NULL AND inv.grams >= t.min_g AND (t.max_g IS NULL OR inv.grams < t.max_g)
            WITH k, a, cl, top, [h IN collect({sub: s.name, amount: inv.amount, grams: inv.grams, point: t.point,
                                               text: t.text}) WHERE h.sub IS NOT NULL] AS hits
            WHERE cl.number = 1 OR (cl.severity = top AND top > 0) OR size(hits) > 0
            RETURN k.name AS case_name, a.id AS article, a.title AS title, cl.number AS number,
                   cl.penalty AS penalty, cl.severity = top AS is_max, hits
            ORDER BY article, number
            """,
            ids=focal,
        ):
            note = []
            if r["number"] == 1:
                note.append("khung cơ bản")
            if r["is_max"]:
                note.append("khung nặng nhất của Điều")
            line = f"[{r['article']} - {r['title']}] khoản {r['number']}: bị {r['penalty']} ({', '.join(note) or 'khung tăng nặng'})"
            for h in r["hits"]:
                line += (f". ÁP DỤNG cho vụ '{r['case_name']}': {h['sub']} {h['amount']} (~{h['grams']:g} gam) "
                         f"thuộc điểm {h['point']} khoản {r['number']}: {h['text']}")
            facts.append(line)

        # 3. Articles named in the question ("Điều 255") or defining a crime named in it: every clause's penalty.
        for r in self.run(
            """
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE any(n IN $articles WHERE a.id STARTS WITH 'Điều ' + n + ' ')
               OR EXISTS { MATCH (a)-[:DEFINES]->(c:Crime) WHERE toLower($q) CONTAINS c.name }
            RETURN a.id AS article, a.title AS title, cl.number AS number, cl.penalty AS penalty
            ORDER BY article, number
            """,
            articles=re.findall(r"[Đđ]iều (\d+)", question), q=question,
        ):
            if r["penalty"]:
                facts.append(f"[{r['article']} - {r['title']}] khoản {r['number']}: bị {r['penalty']}")

        # 4. Defined terms (Điều 2 Luật PCMT) named in the question.
        for r in self.run(
            """
            MATCH (t:Term)-[:DEFINED_IN]->(cl:Clause)<-[:HAS_CLAUSE]-(a:Article)
            WHERE toLower($q) CONTAINS toLower(t.name)
            RETURN t.name AS term, a.id AS article, cl.number AS number, t.definition AS definition
            """,
            q=question,
        ):
            facts.append(f"Thuật ngữ '{r['term']}' [{r['article']} khoản {r['number']}]: {r['definition']}")

        # Raw 1-hop edges repeat the case lines above; keep them only as a fallback.
        return list(dict.fromkeys(facts or edge_facts))[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    if ONTOLOGY == "hint":
        _build_graph_hint(graph, law_docs, news_docs, llm_fn)
    else:
        _build_graph_custom(graph, law_docs, news_docs, llm_fn)

def _build_graph_hint(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                      llm_fn: Callable[..., str]) -> None:
    """Suggested ontology, unchanged (baseline for ket_qua_benchmark_kg.hint.txt)."""
    graph.suggested_constraints()
    articles = [parse_law_article(d) for d in law_docs]
    for a in articles:
        graph.add_law_article(a)
    crimes = [a["crime"] for a in articles if a["crime"]]
    for d in news_docs:
        for case in extract_news_cases(d, lambda p: llm_fn(p, json_mode=True), crimes):
            graph.add_news_case(case, d)

def _build_graph_custom(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                        llm_fn: Callable[..., str]) -> None:
    """Custom ontology (report/ONTOLOGY.md): law by regex (thresholds, severity, terms), news by LLM."""
    graph.custom_constraints()
    articles = [parse_law_article_custom(d) for d in law_docs]
    for a in articles:
        graph.add_law_article_custom(a)
    crimes = [a["crime"] for a in articles if a["crime"]]
    for d in news_docs:
        for case in extract_news_cases_custom(d, lambda p: llm_fn(p, json_mode=True), crimes):
            graph.add_news_case_custom(case, d)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(c["metadata"].get("doc_id") for c in chunks if c["metadata"].get("doc_id")))
        facts = self.graph.context(question, doc_ids)
        prompt = GRAPH_PROMPT.format(
            facts="\n".join(f"- {f}" for f in facts) or "- (không có)",
            chunks="\n\n".join(f"[{i}] {c['content']}" for i, c in enumerate(chunks, start=1)),
            question=question,
        )
        return self.llm_fn(prompt)
