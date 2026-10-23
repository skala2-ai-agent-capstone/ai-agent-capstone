"""평가 기준(루브릭) 저장·승인. 관리자 화면(운영 설정 > 평가 기준 관리)의 초안 → 승인 흐름을 따른다.

- 평가 기준 생성 Agent는 save_rubric(rubric 실행 토큰)으로 초안만 만든다. 현행은 바꾸지 않는다.
  회사·업무 유형마다 초안은 하나다. 초안이 있으면 같은 버전 번호로 덮어쓰고(화면도 열린 초안에 새 설계를 반영한다),
  없으면 새 버전(RUB-{work_type} vN+1)을 만든다.
- 관리자가 고치는 경로도 초안이다(백엔드 내부 API). '수정 초안 만들기'는 /internal/rubrics/drafts(현행 복사),
  '항목 저장'은 /internal/rubrics/drafts/items(항목 하나의 칸 일부). Agent가 다시 save_rubric을 부르면 관리자 수정도 덮어쓴다.
- 관리자가 승인하면 백엔드가 /internal/rubrics/approve를 부른다. 초안이 현행이 되고 이전 현행은 폐기가 된다.
  /internal/rubrics/discard는 초안을 지운다. 초안은 어떤 평가 건도 가리키지 않으므로 남기지 않는다.
  기존 평가 건은 자기 rubric_version을 그대로 쓰므로 승인에 영향받지 않는다.
- 현행 기준을 고르는 곳(rag get_current_rubric, 중간 피드백 요청, 백엔드 평가 건 생성)은 status='현행'만 보므로
  초안은 승인 전까지 평가에 쓰이지 않는다. 초안 내용은 search_rubric(work_type, 초안 버전)으로 볼 수 있다.
- Postgres(documents·rubric_items)와 Chroma(rubrics 컬렉션, rag-mcp가 읽음)를 함께 바꾼다.
  Chroma 반영이 실패하면 Postgres 트랜잭션도 되돌린다. 같은 회사의 기준 쓰기는 한 줄로 세운다.
- based_on_version을 주면 그 버전이 아직 현행일 때만 저장한다(동시 수정 방지).
- 항목은 5개(문제 정의·지시 품질·결과 검증·보안 준수·결과물 품질) 순서 그대로이고 item_id는 서버가 붙인다.
  related_clauses는 현행 내규에 있는 조항이어야 한다(저장할 때와 승인할 때 모두 확인).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from common.chroma import RUBRIC_ITEM_FIELDS, clean_metadata, collection_name, connect
from common.embedding import EMBEDDING_ID, embed_many
from common.runtime import MCPFail, not_found, validation, version_conflict, version_number

from .idempotency import need_key, remember, replay
from .pg import Db
from .store import Store

KST = timezone(timedelta(hours=9))
ITEM_NAMES = ["문제 정의", "지시 품질", "결과 검증", "보안 준수", "결과물 품질"]
WORK_TYPES = ("rfp_analysis", "proposal", "quote", "inquiry_reply")
REQUIRED_TEXT = ("criterion", "score_2", "score_1", "score_0")
OPTIONAL_TEXT = ("unobserved", "needs_review")
SECURITY_SCORE_1 = "해당 없음"     # 보안 준수는 0 또는 2점만
EDITABLE = REQUIRED_TEXT + OPTIONAL_TEXT + ("evidence_sources", "related_clauses")   # 관리자가 초안에서 고칠 수 있는 칸
ITEM_COLS = ", ".join(RUBRIC_ITEM_FIELDS)


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

    def _check_item(self, work_type: str, n: int, it, clauses: set[str], errors: list[str]) -> dict | None:
        """n번째(1부터) 항목 하나를 검사해 저장할 모양으로 돌려준다. 문제는 errors에 더한다."""
        name = ITEM_NAMES[n - 1]
        if not isinstance(it, dict):
            errors.append(f"#{n}: 객체가 아님")
            return None
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
        return {"item_id": f"{work_type}.{n}", "seq": n, "item": name,
                **{f: it.get(f) for f in REQUIRED_TEXT + OPTIONAL_TEXT},
                "evidence_sources": list(it.get("evidence_sources") or []),
                "related_clauses": list(it.get("related_clauses") or [])}

    def _check_items(self, work_type: str, items: list, clauses: set[str]) -> list[dict]:
        if not isinstance(items, list) or len(items) != len(ITEM_NAMES):
            raise validation(f"items는 {len(ITEM_NAMES)}개({', '.join(ITEM_NAMES)})여야 합니다")
        errors: list[str] = []
        out = [self._check_item(work_type, n, it, clauses, errors) for n, it in enumerate(items, 1)]
        if errors:
            raise validation("; ".join(errors))
        return out

    @staticmethod
    def _lock(c, company: str) -> list[dict]:
        """회사의 기준 쓰기를 한 줄로 세우고(첫 저장처럼 잠글 행이 없어도) 회사의 기준 행을 모두 돌려준다."""
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"rubrics:{company}",))
        return c.execute("SELECT doc_id, version, status, work_type FROM documents WHERE company_id=%s AND collection='rubrics' "
                         "ORDER BY doc_id, version", (company,)).fetchall()

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
            if (dup := replay(c, "save_rubric", company, idempotency_key)) is not None:
                return dup
            rows = [r for r in self._lock(c, company) if r["work_type"] == work_type]
            current = max((r for r in rows if r["status"] == "현행"), key=lambda r: version_number(r["version"]), default=None)
            if based_on_version is not None and (current is None or current["version"] != based_on_version):
                now = current["version"] if current else "없음"
                raise version_conflict(f"현행 기준은 {now}입니다 (요청 기준 {based_on_version}). 최신 기준을 읽고 다시 만드세요")
            draft = next((r for r in rows if r["status"] == "초안"), None)
            title = f"평가 기준표 — {work_type}"
            if draft:
                doc_id, version = draft["doc_id"], draft["version"]
                c.execute("DELETE FROM rubric_items WHERE doc_id=%s AND version=%s", (doc_id, version))
                c.execute("UPDATE documents SET note=%s WHERE doc_id=%s AND version=%s", (note or "평가 기준 생성 Agent", doc_id, version))
            else:
                doc_id = current["doc_id"] if current else (rows[0]["doc_id"] if rows else f"RUB-{work_type}")
                taken = c.execute("SELECT 1 FROM documents WHERE doc_id=%s AND company_id<>%s LIMIT 1", (doc_id, company)).fetchone()
                if taken and not rows:
                    doc_id = f"RUB-{company}-{work_type}"
                last = c.execute("SELECT version FROM documents WHERE doc_id=%s", (doc_id,)).fetchall()
                version = f"v{max((version_number(r['version']) for r in last), default=0) + 1}"
                c.execute(
                    "INSERT INTO documents (doc_id, version, company_id, title, grade, collection, indexed, status, work_type, note) "
                    "VALUES (%s,%s,%s,%s,'사내','rubrics',true,'초안',%s,%s)",
                    (doc_id, version, company, title, work_type, note or "평가 기준 생성 Agent"))
            self._insert_items(c, doc_id, version, clean_items)
            try:
                self._index(doc_id, version, company, title, work_type, clean_items)
            except Exception as e:  # Chroma가 안 되면 Postgres도 되돌린다(트랜잭션 예외)
                raise MCPFail("TEMPORARY_FAILURE", f"검색 색인(Chroma) 반영에 실패해 저장하지 않았습니다 ({type(e).__name__})") from e
            result = {"doc_id": doc_id, "version": version, "work_type": work_type, "status": "초안",
                      "based_on_version": current["version"] if current else None, "replaced_draft": draft is not None,
                      "items_saved": len(clean_items)}
            remember(c, "save_rubric", company, idempotency_key, result)
        self.store.reload()   # 최종 평가 초안 검증(save_evaluation_draft)이 버전 목록을 최신으로 보도록
        return result

    @staticmethod
    def _insert_items(c, doc_id: str, version: str, items: list[dict]) -> None:
        for it in items:
            c.execute(f"INSERT INTO rubric_items (doc_id, version, {ITEM_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (doc_id, version, *(it[k] for k in RUBRIC_ITEM_FIELDS)))

    def _index(self, doc_id: str, version: str, company: str, title: str, work_type: str, items: list[dict]) -> None:
        """초안 항목을 넣는다(덮어쓸 때도 item_id가 같으므로 그대로 바뀐다). 항목 하나만 넘겨도 된다."""
        col = self._collection()
        base = {"doc_id": doc_id, "version": version, "doc_key": f"{doc_id}@{version}", "title": title, "grade": "사내",
                "status": "초안", "company_id": company, "work_type": work_type}
        ids, docs, metas = [], [], []
        for it in items:
            ids.append(f"{doc_id}@{version}#{it['item_id']}")
            docs.append(f"{it['item']}: {it['criterion']}")
            metas.append(clean_metadata({**base, **{k: it[k] for k in RUBRIC_ITEM_FIELDS}}))
        col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embed_many(docs))

    # ── 관리자 승인·폐기 (백엔드가 내부 API로 부름) ──
    @staticmethod
    def _work_types(body: dict) -> list[str] | None:
        wanted = body.get("work_types")
        if wanted is not None and (not isinstance(wanted, list) or not wanted or any(w not in WORK_TYPES for w in wanted)):
            raise validation(f"work_types는 {', '.join(WORK_TYPES)} 중에서 고른 목록입니다")
        return wanted

    def _drafts(self, body: dict, rows: list[dict]) -> list[dict]:
        """body.work_types(생략하면 회사의 모든 초안)에 해당하는 초안 행. 요청한 유형에 초안이 없으면 NOT_FOUND."""
        wanted = self._work_types(body)
        drafts = [r for r in rows if r["status"] == "초안" and (wanted is None or r["work_type"] in wanted)]
        missing = [w for w in wanted or [] if w not in {r["work_type"] for r in drafts}]
        if missing:
            raise not_found(f"평가 기준 초안({', '.join(missing)})")
        if not drafts:
            raise not_found("평가 기준 초안")
        return drafts

    def _company(self, body: dict) -> str:
        company = body.get("company_id")
        if company not in {c["company_id"] for c in self.store.org["companies"]}:
            raise not_found(f"회사 {company}")
        return company

    def approve(self, body: dict) -> dict:
        """초안을 현행으로 바꾸고 같은 기준의 이전 현행을 폐기로 바꾼다. 화면의 '승인 후 적용' 하나가 이 호출 하나다."""
        company = self._company(body)
        who = body.get("approved_by")
        today = datetime.now(KST).date()
        clauses = self._clauses(company)
        approved, changes = [], []
        with self.db.tx() as c:
            rows = self._lock(c, company)
            drafts = self._drafts(body, rows)
            for d in drafts:
                # 초안을 만든 뒤 내규가 바뀌었을 수 있다. 현행 내규에 없는 조항을 인용한 기준은 현행으로 올리지 않는다
                cited = c.execute("SELECT DISTINCT unnest(related_clauses) AS cl FROM rubric_items WHERE doc_id=%s AND version=%s",
                                  (d["doc_id"], d["version"])).fetchall()
                unknown = sorted(r["cl"] for r in cited if r["cl"] not in clauses)
                if unknown:
                    raise validation(f"{d['work_type']} 초안 {d['version']}이 현행 내규에 없는 조항을 인용합니다: {', '.join(unknown)}")
                retired = sorted(r["version"] for r in rows if r["status"] == "현행" and r["doc_id"] == d["doc_id"])
                c.execute("UPDATE documents SET status='현행', effective_from=%s, note=concat_ws(' · ', note, %s::text) "
                          "WHERE doc_id=%s AND version=%s", (today, f"승인 {who}" if who else "승인", d["doc_id"], d["version"]))
                if retired:
                    c.execute("UPDATE documents SET status='폐기' WHERE doc_id=%s AND version = ANY(%s)", (d["doc_id"], retired))
                changes.append((f"{d['doc_id']}@{d['version']}", {"status": "현행", "effective_from": today.isoformat()}))
                changes += [(f"{d['doc_id']}@{old}", {"status": "폐기"}) for old in retired]
                approved.append({"work_type": d["work_type"], "doc_id": d["doc_id"], "version": d["version"], "retired_versions": retired})
            try:
                self._restatus(company, changes)
            except Exception as e:
                raise MCPFail("TEMPORARY_FAILURE", f"검색 색인(Chroma) 반영에 실패해 승인하지 않았습니다 ({type(e).__name__})") from e
        self.store.reload()
        return {"approved": approved}

    def discard(self, body: dict) -> dict:
        """초안을 지운다(rubric_items는 CASCADE). 현행은 그대로다."""
        company = self._company(body)
        with self.db.tx() as c:
            drafts = self._drafts(body, self._lock(c, company))
            for d in drafts:
                c.execute("DELETE FROM documents WHERE doc_id=%s AND version=%s", (d["doc_id"], d["version"]))
            try:
                col = self._collection()
                for d in drafts:
                    g = col.get(where={"$and": [{"company_id": company}, {"doc_key": f"{d['doc_id']}@{d['version']}"}]},
                                include=["metadatas"])
                    if g["ids"]:
                        col.delete(ids=g["ids"])
            except Exception as e:
                raise MCPFail("TEMPORARY_FAILURE", f"검색 색인(Chroma) 반영에 실패해 폐기하지 않았습니다 ({type(e).__name__})") from e
        self.store.reload()
        return {"discarded": [{"work_type": d["work_type"], "doc_id": d["doc_id"], "version": d["version"]} for d in drafts]}

    def create_draft(self, body: dict) -> dict:
        """관리자 '수정 초안 만들기': 현행 기준을 그대로 복사해 초안을 만든다(AI 없이). 고치는 것은 update_item으로 한다.
        요청한 유형 중 하나라도 초안이 이미 있으면 VERSION_CONFLICT, 현행이 없으면 NOT_FOUND이고 아무것도 만들지 않는다."""
        company = self._company(body)
        wanted = self._work_types(body) or list(WORK_TYPES)
        who = body.get("created_by")
        created, copies = [], []
        with self.db.tx() as c:
            rows = self._lock(c, company)
            busy = [wt for wt in wanted if any(r["status"] == "초안" and r["work_type"] == wt for r in rows)]
            if busy:
                raise version_conflict(f"평가 기준 초안이 이미 있습니다({', '.join(busy)}). 그 초안을 고치거나 폐기한 뒤 만드세요")
            for wt in wanted:
                cur = max((r for r in rows if r["status"] == "현행" and r["work_type"] == wt),
                          key=lambda r: version_number(r["version"]), default=None)
                if cur is None:
                    raise not_found(f"현행 평가 기준({wt})")
                title = c.execute("SELECT title FROM documents WHERE doc_id=%s AND version=%s",
                                  (cur["doc_id"], cur["version"])).fetchone()["title"]
                items = c.execute(f"SELECT {ITEM_COLS} FROM rubric_items WHERE doc_id=%s AND version=%s ORDER BY seq",
                                  (cur["doc_id"], cur["version"])).fetchall()
                last = c.execute("SELECT version FROM documents WHERE doc_id=%s", (cur["doc_id"],)).fetchall()
                version = f"v{max(version_number(r['version']) for r in last) + 1}"
                c.execute(
                    "INSERT INTO documents (doc_id, version, company_id, title, grade, collection, indexed, status, work_type, note) "
                    "VALUES (%s,%s,%s,%s,'사내','rubrics',true,'초안',%s,%s)",
                    (cur["doc_id"], version, company, title, wt, f"관리자 수정 초안({who})" if who else "관리자 수정 초안"))
                self._insert_items(c, cur["doc_id"], version, items)
                copies.append((cur["doc_id"], version, title, wt, items))
                created.append({"work_type": wt, "doc_id": cur["doc_id"], "version": version, "based_on_version": cur["version"]})
            try:
                for doc_id, version, title, wt, items in copies:
                    self._index(doc_id, version, company, title, wt, items)
            except Exception as e:
                raise MCPFail("TEMPORARY_FAILURE", f"검색 색인(Chroma) 반영에 실패해 초안을 만들지 않았습니다 ({type(e).__name__})") from e
        self.store.reload()
        return {"created": created}

    def update_item(self, body: dict) -> dict:
        """관리자 '항목 저장': 초안의 기준 항목 하나에서 fields로 보낸 칸만 바꾼다. 바뀐 항목은 save_rubric과 같은 검사를 다시 거친다.
        '이전 기준으로 되돌리기'도 이전 버전 항목의 값을 fields로 보내면 된다."""
        company = self._company(body)
        wt, name, fields = body.get("work_type"), body.get("item"), body.get("fields")
        if wt not in WORK_TYPES:
            raise validation(f"work_type은 {', '.join(WORK_TYPES)} 중 하나입니다")
        if name not in ITEM_NAMES:
            raise validation(f"item은 {', '.join(ITEM_NAMES)} 중 하나입니다")
        if not isinstance(fields, dict) or not fields:
            raise validation(f"fields에 바꿀 칸을 넣으세요({', '.join(EDITABLE)})")
        extra = sorted(set(fields) - set(EDITABLE))
        if extra:
            raise validation(f"고칠 수 없는 칸 {extra}. 고칠 수 있는 칸: {', '.join(EDITABLE)}")
        with self.db.tx() as c:
            draft = next((r for r in self._lock(c, company) if r["status"] == "초안" and r["work_type"] == wt), None)
            if draft is None:
                raise not_found(f"평가 기준 초안({wt})")
            key = (draft["doc_id"], draft["version"])
            old = c.execute(f"SELECT {ITEM_COLS} FROM rubric_items WHERE doc_id=%s AND version=%s AND item=%s", (*key, name)).fetchone()
            if old is None:
                raise not_found(f"초안 {key[0]} {key[1]}의 항목 {name}")
            errors: list[str] = []
            checked = self._check_item(wt, ITEM_NAMES.index(name) + 1, {**old, **fields}, self._clauses(company), errors)
            if errors:
                raise validation("; ".join(errors))
            new = {**checked, "item_id": old["item_id"], "seq": old["seq"]}
            c.execute("UPDATE rubric_items SET " + ", ".join(f"{f}=%s" for f in EDITABLE) + " WHERE doc_id=%s AND version=%s AND item_id=%s",
                      (*(new[f] for f in EDITABLE), *key, old["item_id"]))
            title = c.execute("SELECT title FROM documents WHERE doc_id=%s AND version=%s", key).fetchone()["title"]
            try:
                self._index(*key, company, title, wt, [new])
            except Exception as e:
                raise MCPFail("TEMPORARY_FAILURE", f"검색 색인(Chroma) 반영에 실패해 저장하지 않았습니다 ({type(e).__name__})") from e
        self.store.reload()
        return {"work_type": wt, "doc_id": key[0], "version": key[1], "item": new,
                "changed_fields": [f for f in EDITABLE if old[f] != new[f]]}

    def _restatus(self, company: str, changes: list[tuple[str, dict]]) -> None:
        """기준 버전(doc_key)별로 Chroma 메타데이터의 status 등을 바꾼다. 한 번의 upsert로 넣는다."""
        col = self._collection()
        ids, docs, metas = [], [], []
        for key, fields in changes:
            g = col.get(where={"$and": [{"company_id": company}, {"doc_key": key}]}, include=["documents", "metadatas"])
            for i, d, m in zip(g["ids"], g["documents"], g["metadatas"]):
                ids.append(i)
                docs.append(d)
                metas.append({**m, **fields})
        if ids:
            col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embed_many(docs))
