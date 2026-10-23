"""평가 기준(루브릭) 저장. 평가 기준 생성 Agent가 rubric 실행 토큰으로 호출한다.

- 관리자 승인 없이 바로 현행이 된다. 새 버전(RUB-{work_type} vN+1)을 만들고 이전 현행 버전은 폐기로 바꾼다.
  기존 평가 건은 자기 rubric_version을 그대로 쓰므로 영향이 없다.
- Postgres(documents·rubric_items)와 Chroma(rubrics 컬렉션, rag-mcp search_rubric이 읽음)를 함께 바꾼다.
  Chroma 반영이 실패하면 Postgres 트랜잭션도 되돌린다.
- based_on_version을 주면 그 버전이 아직 현행일 때만 저장한다(동시 수정 방지).
- 항목은 5개(문제 정의·지시 품질·결과 검증·보안 준수·결과물 품질) 순서 그대로이고 item_id는 서버가 붙인다.
  related_clauses는 현행 내규에 있는 조항이어야 한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from common.chroma import RUBRIC_ITEM_FIELDS, clean_metadata, collection_name, connect
from common.embedding import EMBEDDING_ID, embed_many
from common.runtime import MCPFail, validation, version_conflict, version_number

from .idempotency import need_key, remember, replay
from .pg import Db
from .store import Store

KST = timezone(timedelta(hours=9))
ITEM_NAMES = ["문제 정의", "지시 품질", "결과 검증", "보안 준수", "결과물 품질"]
WORK_TYPES = ("rfp_analysis", "proposal", "quote", "inquiry_reply")
REQUIRED_TEXT = ("criterion", "score_2", "score_1", "score_0")
OPTIONAL_TEXT = ("unobserved", "needs_review")
SECURITY_SCORE_1 = "해당 없음"     # 보안 준수는 0 또는 2점만


class RubricWriter:
    def __init__(self, db: Db, store: Store):
        self.db = db
        self.store = store
        self._client = None

    def _collection(self):
        if self._client is None:
            self._client = connect(wait_sec=10)
        return self._client.get_or_create_collection(
            collection_name("rubrics"), metadata={"hnsw:space": "cosine", "embedding": EMBEDDING_ID}, embedding_function=None)

    def _clauses(self, company_id: str) -> set[str]:
        return {ch["clause_id"] for d in self.store.documents
                if d.get("collection") == "policies" and d.get("status") == "현행" and d.get("company_id") == company_id
                for ch in d.get("chunks") or [] if ch.get("clause_id")}

    def _check_items(self, work_type: str, items: list, clauses: set[str]) -> list[dict]:
        if not isinstance(items, list) or len(items) != len(ITEM_NAMES):
            raise validation(f"items는 {len(ITEM_NAMES)}개({', '.join(ITEM_NAMES)})여야 합니다")
        errors = []
        out = []
        for n, (it, name) in enumerate(zip(items, ITEM_NAMES), 1):
            if not isinstance(it, dict):
                errors.append(f"#{n}: 객체가 아님")
                continue
            if it.get("item") != name:
                errors.append(f"#{n}: item은 '{name}'이어야 합니다(순서 고정)")
            for f in REQUIRED_TEXT:
                if not isinstance(it.get(f), str) or not it[f].strip():
                    errors.append(f"#{n} {name}: {f} 필요")
            for f in OPTIONAL_TEXT:
                if it.get(f) is not None and not isinstance(it[f], str):
                    errors.append(f"#{n} {name}: {f}는 문자열")
            if name == "보안 준수" and it.get("score_1") != SECURITY_SCORE_1:
                errors.append(f"#{n} 보안 준수: score_1은 '{SECURITY_SCORE_1}'(0 또는 2점만)")
            for f in ("evidence_sources", "related_clauses"):
                v = it.get(f)
                if not isinstance(v, list) or not v or not all(isinstance(x, str) and x.strip() for x in v):
                    errors.append(f"#{n} {name}: {f}는 비어 있지 않은 문자열 목록")
            unknown = [x for x in it.get("related_clauses") or [] if isinstance(x, str) and x not in clauses]
            if unknown:
                errors.append(f"#{n} {name}: 현행 내규에 없는 조항 {unknown}")
            texts = [it.get(f) for f in REQUIRED_TEXT + OPTIONAL_TEXT] + list(it.get("evidence_sources") or [])
            if any(self.store.masker.has_sensitive(t) for t in texts if isinstance(t, str)):
                errors.append(f"#{n} {name}: 민감정보 패턴")
            out.append({"item_id": f"{work_type}.{n}", "seq": n, "item": name,
                        **{f: it.get(f) for f in REQUIRED_TEXT + OPTIONAL_TEXT},
                        "evidence_sources": list(it.get("evidence_sources") or []),
                        "related_clauses": list(it.get("related_clauses") or [])})
        if errors:
            raise validation("; ".join(errors))
        return out

    def save(self, claims: dict, work_type: str, items: list, idempotency_key: str,
             based_on_version: str | None = None, note: str | None = None):
        need_key(idempotency_key)
        if work_type not in WORK_TYPES:
            raise validation(f"work_type은 {', '.join(WORK_TYPES)} 중 하나입니다")
        company = claims["company_id"]
        clean_items = self._check_items(work_type, items, self._clauses(company))
        if note is not None and (not isinstance(note, str) or self.store.masker.has_sensitive(note)):
            raise validation("note는 민감정보가 없는 문자열이어야 합니다")
        with self.db.tx() as c:
            if (dup := replay(c, "save_rubric", idempotency_key)) is not None:
                return dup
            # 같은 회사·유형의 기준 행을 잠가 동시 저장을 한 줄로 세운다
            rows = c.execute("SELECT doc_id, version, status FROM documents WHERE company_id=%s AND collection='rubrics' AND work_type=%s "
                             "FOR UPDATE", (company, work_type)).fetchall()
            current = max((r for r in rows if r["status"] == "현행"), key=lambda r: version_number(r["version"]), default=None)
            if based_on_version is not None and (current is None or current["version"] != based_on_version):
                now = current["version"] if current else "없음"
                raise version_conflict(f"현행 기준은 {now}입니다 (요청 기준 {based_on_version}). 최신 기준을 읽고 다시 만드세요")
            doc_id = current["doc_id"] if current else (rows[0]["doc_id"] if rows else f"RUB-{work_type}")
            taken = c.execute("SELECT 1 FROM documents WHERE doc_id=%s AND company_id<>%s LIMIT 1", (doc_id, company)).fetchone()
            if taken and not rows:
                doc_id = f"RUB-{company}-{work_type}"
            last = c.execute("SELECT version FROM documents WHERE doc_id=%s", (doc_id,)).fetchall()
            version = f"v{max((version_number(r['version']) for r in last), default=0) + 1}"
            retired = sorted(r["version"] for r in rows if r["status"] == "현행" and r["doc_id"] == doc_id)
            today = datetime.now(KST).date()
            title = f"평가 기준표 — {work_type}"
            c.execute(
                "INSERT INTO documents (doc_id, version, company_id, title, grade, collection, indexed, status, effective_from, "
                "work_type, note) VALUES (%s,%s,%s,%s,'사내','rubrics',true,'현행',%s,%s,%s)",
                (doc_id, version, company, title, today, work_type, note or "평가 기준 생성 Agent"))
            for it in clean_items:
                c.execute("INSERT INTO rubric_items (doc_id, version, item_id, seq, item, criterion, score_2, score_1, score_0, unobserved, "
                          "needs_review, evidence_sources, related_clauses) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                          (doc_id, version, it["item_id"], it["seq"], it["item"], it["criterion"], it["score_2"], it["score_1"],
                           it["score_0"], it["unobserved"], it["needs_review"], it["evidence_sources"], it["related_clauses"]))
            if retired:
                c.execute("UPDATE documents SET status='폐기' WHERE doc_id=%s AND version = ANY(%s)", (doc_id, retired))
            try:
                self._index(doc_id, version, company, title, today, work_type, clean_items, retired)
            except Exception as e:  # Chroma가 안 되면 Postgres도 되돌린다(트랜잭션 예외)
                raise MCPFail("TEMPORARY_FAILURE", f"검색 색인(Chroma) 반영에 실패해 저장하지 않았습니다 ({type(e).__name__})") from e
            result = {"doc_id": doc_id, "version": version, "work_type": work_type, "status": "현행",
                      "retired_versions": retired, "items_saved": len(clean_items)}
            remember(c, "save_rubric", idempotency_key, result)
        self.store.reload()   # 최종 평가 초안 검증(save_evaluation_draft)이 새 버전을 알도록
        return result

    def _index(self, doc_id: str, version: str, company: str, title: str, today, work_type: str, items: list[dict],
               retired: list[str]) -> None:
        col = self._collection()
        base = {"doc_id": doc_id, "version": version, "doc_key": f"{doc_id}@{version}", "title": title, "grade": "사내",
                "status": "현행", "company_id": company, "effective_from": today.isoformat(), "work_type": work_type}
        ids, docs, metas = [], [], []
        for it in items:
            ids.append(f"{doc_id}@{version}#{it['item_id']}")
            docs.append(f"{it['item']}: {it['criterion']}")
            metas.append(clean_metadata({**base, **{k: it[k] for k in RUBRIC_ITEM_FIELDS}}))
        for old in retired:
            g = col.get(where={"doc_key": f"{doc_id}@{old}"}, include=["documents", "metadatas"])
            for i, d, m in zip(g["ids"], g["documents"], g["metadatas"]):
                ids.append(i)
                docs.append(d)
                metas.append({**m, "status": "폐기"})
        col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embed_many(docs))
