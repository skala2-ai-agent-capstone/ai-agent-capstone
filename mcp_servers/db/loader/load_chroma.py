"""PostgreSQL의 사내 문서(documents·document_chunks·rubric_items)로 ChromaDB 컬렉션 4개를 만든다 (명세 5.1).

    python -m db.loader.load_chroma            # 컬렉션을 지우고 다시 만든다

- indexed=false인 기밀 문서(ROADMAP-2027·COST-2026)는 어떤 컬렉션에도 넣지 않는다.
- 문서 ID = {doc_id}@{version}#{chunk_id} (근거 ID와 같음). 루브릭은 항목 하나가 문서 하나다.
- Chroma metadata는 str·int·float·bool만 되므로 목록 값(evidence_sources 등)은 JSON 문자열로 넣는다.
"""
from __future__ import annotations

import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common.chroma import COLLECTIONS, collection_name, connect  # noqa: E402
from common.embedding import EMBEDDING_ID, embed_many  # noqa: E402
from common.runtime import Masker  # noqa: E402


def clean(m: dict) -> dict:
    out = {}
    for k, v in m.items():
        if v is None:
            continue
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        elif not isinstance(v, (str, int, float, bool)):
            v = str(v)
        out[k] = v
    return out


def build(conn) -> dict[str, list[tuple[str, str, dict]]]:
    rules = conn.execute("SELECT rule_id, kind, pattern, replace_with FROM policy_rules ORDER BY seq").fetchall()
    masker = Masker({"rules": rules})
    docs = conn.execute(
        "SELECT * FROM documents WHERE indexed AND collection IS NOT NULL ORDER BY collection, doc_id, version"
    ).fetchall()
    out: dict[str, list] = {k: [] for k in COLLECTIONS}
    for d in docs:
        base = {"doc_id": d["doc_id"], "version": d["version"], "doc_key": f"{d['doc_id']}@{d['version']}", "title": d["title"],
                "grade": d["grade"], "status": d["status"], "company_id": d["company_id"],
                "effective_from": d["effective_from"].isoformat() if d["effective_from"] else None}
        coll = d["collection"]
        if coll in ("references", "policies"):
            for c in conn.execute("SELECT * FROM document_chunks WHERE doc_id=%s AND version=%s ORDER BY seq", (d["doc_id"], d["version"])):
                meta = {**base, "chunk_id": c["chunk_id"], "seq": c["seq"], "clause_id": c["clause_id"], "label": c["label"]}
                out[coll].append((f"{d['doc_id']}@{d['version']}#{c['chunk_id']}", masker.mask(c["text"]), clean(meta)))
        elif coll == "rubrics":
            for it in conn.execute("SELECT * FROM rubric_items WHERE doc_id=%s AND version=%s ORDER BY seq", (d["doc_id"], d["version"])):
                meta = {**base, "work_type": d["work_type"], "item_id": it["item_id"], "item": it["item"], "seq": it["seq"],
                        "criterion": it["criterion"], "score_2": it["score_2"], "score_1": it["score_1"], "score_0": it["score_0"],
                        "unobserved": it["unobserved"], "needs_review": it["needs_review"],
                        "evidence_sources": it["evidence_sources"], "related_clauses": it["related_clauses"]}
                text = f"{it['item']}: {it['criterion']}"
                out[coll].append((f"{d['doc_id']}@{d['version']}#{it['item_id']}", text, clean(meta)))
        elif coll == "coaching_examples":
            meta = {**base, "item": d["item"], "work_type": d["work_type"], "score_level": d["score_level"]}
            out[coll].append((f"{d['doc_id']}@{d['version']}", d["content"], clean(meta)))
    return out


def main() -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL 환경 변수가 필요합니다")
    client = connect()
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        data = build(conn)
    summary = {}
    for kind, rows in data.items():
        name = collection_name(kind)
        try:
            client.delete_collection(name)
        except Exception:
            pass
        col = client.get_or_create_collection(name, metadata={"hnsw:space": "cosine", "embedding": EMBEDDING_ID},
                                              embedding_function=None)
        for i in range(0, len(rows), 100):
            batch = rows[i : i + 100]
            col.upsert(ids=[r[0] for r in batch], documents=[r[1] for r in batch], metadatas=[r[2] for r in batch],
                       embeddings=embed_many([r[1] for r in batch]))
        summary[name] = col.count()
    print(json.dumps({"collections": summary, "embedding": EMBEDDING_ID}, ensure_ascii=False))


if __name__ == "__main__":
    main()
