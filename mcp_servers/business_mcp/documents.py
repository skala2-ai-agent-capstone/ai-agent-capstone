"""내규 적재. 관리자가 올린 내규 파일을 새 버전으로 저장하고 바로 현행으로 만든다 (백엔드가 내부 API /internal/documents로 호출).

- 파일(PDF·TXT)을 조·항 구간으로 나눠(common/policy_doc.py) Postgres(documents·document_chunks)와
  Chroma(policies 컬렉션, rag-mcp search_policy가 읽음)를 함께 바꾼다. Chroma 반영이 실패하면 Postgres도 되돌린다.
- 회사의 현행 내규는 하나다(평가 기준 생성 Agent는 한 문서의 한 버전만 받는다). 이전 현행은 폐기로 바꾸고 구간은 남긴다.
  지난 평가의 근거 ID({doc_id}@{version}#{chunk_id})가 이전 버전을 가리키기 때문이다.
- 평가 기준 생성 Agent가 찾는 조항과 회사의 현행 평가 기준이 인용하는 조항이 하나라도 없으면 저장하지 않는다.
- 같은 (doc_id, version)을 다시 올리면 내용이 같을 때만 그 결과를 돌려주고(duplicate:true), 다르면 VERSION_CONFLICT.
- 저장 뒤 rag-mcp /internal/reload를 불러 키워드 검색 사본을 새로 읽게 한다. 실패해도 저장은 유지하고 rag_reloaded:false로 알린다.
"""
from __future__ import annotations

import base64
import binascii
import logging
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from common.chroma import clean_metadata, collection_name, connect
from common.embedding import EMBEDDING_ID, embed_many
from common.policy_doc import AGENT_REQUIRED_CLAUSES, read_policy_lines, split_policy_lines
from common.runtime import MCPFail, not_found, validation, version_conflict

from .pg import Db
from .store import Store

KST = timezone(timedelta(hours=9))
GRADES = ("사내", "대외비")   # 기밀은 색인하지 않으므로(indexed=false) 이 경로로 올리지 않는다
log = logging.getLogger("business-mcp")


def _squash(text: str) -> str:
    """버전 대조용: PDF 줄바꿈 위치가 달라도 같은 문장으로 본다."""
    return "".join(text.split())


class PolicyDocWriter:
    def __init__(self, db: Db, store: Store, rag_url: str | None, internal_key: str | None):
        self.db = db
        self.store = store
        self.rag_url = rag_url
        self.internal_key = internal_key
        self._client = None

    def _collection(self):
        if self._client is None:
            self._client = connect(wait_sec=10)
        return self._client.get_or_create_collection(
            collection_name("policies"), metadata={"hnsw:space": "cosine", "embedding": EMBEDDING_ID}, embedding_function=None)

    def _required(self, company_id: str) -> set[str]:
        cited = {cl for d in self.store.documents
                 if d.get("collection") == "rubrics" and d.get("status") == "현행" and d.get("company_id") == company_id
                 for it in d.get("items") or [] for cl in it.get("related_clauses") or []}
        return set(AGENT_REQUIRED_CLAUSES) | cited

    def _parse(self, body: dict) -> tuple[str, list[dict]]:
        source_file = body.get("source_file")
        if not isinstance(source_file, str) or not source_file.strip():
            raise validation("source_file(원본 파일명, .pdf 또는 .txt)이 필요합니다")
        try:
            data = base64.b64decode(body.get("content_base64") or "", validate=True)
        except (binascii.Error, TypeError) as e:
            raise validation("content_base64는 파일 내용의 base64 문자열이어야 합니다") from e
        if not data:
            raise validation("content_base64가 비어 있습니다")
        try:
            chunks = split_policy_lines(read_policy_lines(data, source_file))
        except ValueError as e:
            raise validation(str(e)) from e
        if not chunks:
            raise validation("조항(제N조·별표 N.)을 찾지 못했습니다. 내규 형식인지 확인하세요")
        seen, dup = set(), []
        for ch in chunks:
            if ch["chunk_id"] in seen:
                dup.append(ch["chunk_id"])
            seen.add(ch["chunk_id"])
        if dup:
            raise validation(f"같은 구간 번호가 두 번 나옵니다: {', '.join(sorted(set(dup)))}")
        return source_file.strip(), chunks

    def save(self, body: dict) -> dict:
        company = body.get("company_id")
        if company not in {c["company_id"] for c in self.store.org["companies"]}:
            raise not_found(f"회사 {company}")
        version = body.get("version")
        if not isinstance(version, str) or not version.strip():
            raise validation("version(예 v1.1)이 필요합니다")
        version = version.strip()
        grade = body.get("grade")
        if grade is not None and grade not in GRADES:
            raise validation(f"grade는 {'·'.join(GRADES)} 중 하나입니다")
        try:
            effective_from = date.fromisoformat(body["effective_from"]) if body.get("effective_from") else datetime.now(KST).date()
        except (TypeError, ValueError) as e:
            raise validation("effective_from은 YYYY-MM-DD입니다") from e
        source_file, chunks = self._parse(body)
        missing = sorted(self._required(company) - {ch["clause_id"] for ch in chunks})
        if missing:
            raise validation(f"평가 기준 생성·현행 평가 기준에 필요한 조항이 새 내규에 없습니다: {', '.join(missing)}")
        doc = {"company_id": company, "version": version, "grade": grade, "effective_from": effective_from,
               "source_file": source_file, "content": "\n".join(ch["text"] for ch in chunks)}
        with self.db.tx() as c:
            result = self._write(c, body, doc, chunks)
        if not result["duplicate"]:
            self.store.reload()   # save_rubric의 현행 내규 조항 검사가 새 버전을 보도록
        return {**result, "rag_reloaded": self._reload_rag()}

    def _write(self, c, body: dict, doc: dict, chunks: list[dict]) -> dict:
        company, version = doc["company_id"], doc["version"]
        summary = {"chunks": len(chunks), "clauses": len({ch["clause_id"] for ch in chunks})}
        # 같은 회사의 내규 적재를 한 줄로 세운다(첫 적재처럼 잠글 행이 없어도 동작)
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"policies:{company}",))
        current = c.execute("SELECT doc_id, version, title, grade FROM documents WHERE company_id=%s AND collection='policies' "
                            "AND status='현행' ORDER BY doc_id, version", (company,)).fetchall()
        doc_id = body.get("doc_id") or (current[0]["doc_id"] if current else None)
        if not doc_id:
            raise validation("회사에 현행 내규가 없어 doc_id가 필요합니다")
        same = c.execute("SELECT company_id, collection, status, title, content FROM documents WHERE doc_id=%s AND version=%s",
                         (doc_id, version)).fetchone()
        if same:
            if same["company_id"] == company and same["collection"] == "policies" and same["content"] == doc["content"]:
                return {"doc_id": doc_id, "version": version, "status": same["status"], "title": same["title"], **summary,
                        "retired_docs": [], "diff": None, "duplicate": True}
            raise version_conflict(f"{doc_id} {version}은(는) 이미 있습니다. 새 버전 번호로 올리세요")
        if c.execute("SELECT 1 FROM documents WHERE doc_id=%s AND company_id<>%s LIMIT 1", (doc_id, company)).fetchone():
            raise validation(f"{doc_id}는 다른 회사의 문서 ID입니다")
        prev = next((d for d in current if d["doc_id"] == doc_id), current[0] if current else None)
        title = body.get("title") or (prev["title"] if prev else None)
        if not title:
            raise validation("회사에 현행 내규가 없어 title이 필요합니다")
        doc = {**doc, "doc_id": doc_id, "title": title, "grade": doc["grade"] or (prev["grade"] if prev else "사내")}
        note = f"관리자 업로드({body['uploaded_by']})" if body.get("uploaded_by") else "관리자 업로드"
        c.execute(
            "INSERT INTO documents (doc_id, version, company_id, title, grade, collection, indexed, status, effective_from, "
            "content, source_file, note) VALUES (%s,%s,%s,%s,%s,'policies',true,'현행',%s,%s,%s,%s)",
            (doc_id, version, company, title, doc["grade"], doc["effective_from"], doc["content"], doc["source_file"], note))
        for seq, ch in enumerate(chunks, 1):
            c.execute("INSERT INTO document_chunks (doc_id, version, chunk_id, seq, clause_id, label, text) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s)", (doc_id, version, ch["chunk_id"], seq, ch["clause_id"], ch["label"], ch["text"]))
        retired = [f"{d['doc_id']}@{d['version']}" for d in current]
        if current:
            c.execute("UPDATE documents SET status='폐기' WHERE company_id=%s AND collection='policies' AND status='현행' "
                      "AND NOT (doc_id=%s AND version=%s)", (company, doc_id, version))
        diff = self._diff(c, prev, chunks)
        try:
            self._index(doc, chunks, retired)
        except Exception as e:  # Chroma가 안 되면 Postgres도 되돌린다(트랜잭션 예외)
            raise MCPFail("TEMPORARY_FAILURE", f"검색 색인(Chroma) 반영에 실패해 저장하지 않았습니다 ({type(e).__name__})") from e
        return {"doc_id": doc_id, "version": version, "status": "현행", "title": title, **summary,
                "retired_docs": retired, "diff": diff, "duplicate": False}

    @staticmethod
    def _diff(c, prev: dict | None, chunks: list[dict]) -> dict | None:
        """직전 현행 버전과 구간 단위 대조(화면의 '이전 버전 대조'). 직전 버전이 없으면 None."""
        if not prev:
            return None
        old = {r["chunk_id"]: _squash(r["text"]) for r in c.execute(
            "SELECT chunk_id, text FROM document_chunks WHERE doc_id=%s AND version=%s ORDER BY seq", (prev["doc_id"], prev["version"]))}
        new = {ch["chunk_id"]: _squash(ch["text"]) for ch in chunks}
        return {"base": f"{prev['doc_id']}@{prev['version']}",
                "added": [k for k in new if k not in old],
                "removed": [k for k in old if k not in new],
                "changed": [k for k in new if k in old and new[k] != old[k]]}

    def _index(self, doc: dict, chunks: list[dict], retired: list[str]) -> None:
        """새 버전 구간을 넣고 이전 버전 구간의 status를 폐기로 바꾼다. 메타데이터는 적재기(load_chroma)와 같은 칸이다."""
        col = self._collection()
        key = f"{doc['doc_id']}@{doc['version']}"
        base = {"doc_id": doc["doc_id"], "version": doc["version"], "doc_key": key, "title": doc["title"], "grade": doc["grade"],
                "status": "현행", "company_id": doc["company_id"], "effective_from": doc["effective_from"].isoformat()}
        ids, docs, metas = [], [], []
        for seq, ch in enumerate(chunks, 1):
            ids.append(f"{key}#{ch['chunk_id']}")
            docs.append(self.store.masker.mask(ch["text"]))
            metas.append(clean_metadata({**base, "chunk_id": ch["chunk_id"], "seq": seq, "clause_id": ch["clause_id"],
                                         "label": ch["label"]}))
        if retired:
            keys = {"doc_key": retired[0]} if len(retired) == 1 else {"doc_key": {"$in": retired}}
            g = col.get(where={"$and": [{"company_id": doc["company_id"]}, keys]}, include=["documents", "metadatas"])
            for i, d, m in zip(g["ids"], g["documents"], g["metadatas"]):
                ids.append(i)
                docs.append(d)
                metas.append({**m, "status": "폐기"})
        col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embed_many(docs))

    def _reload_rag(self) -> bool:
        """rag-mcp의 키워드 검색 사본·컬렉션 핸들을 다시 읽게 한다. RAG_MCP_URL이 없거나 실패하면 False."""
        if not self.rag_url:
            return False
        req = urllib.request.Request(self.rag_url.rstrip("/") + "/internal/reload", data=b"", method="POST",
                                     headers={"X-Internal-Key": self.internal_key or ""})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError) as e:
            log.warning("rag-mcp reload failed after policy upload: %s", type(e).__name__)
            return False
