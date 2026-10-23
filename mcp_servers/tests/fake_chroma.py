"""로컬 개발·CI용 ChromaDB 대역 (chromadb 클라이언트의 HttpClient·Collection 중 이 프로젝트가 쓰는 메서드만).

Docker에서는 실제 Chroma 서버와 chromadb 클라이언트를 쓴다. 이 대역은 Chroma를 띄울 수 없는 환경에서
적재기·rag-mcp 로직을 시험하려고 만든 것이며, CHROMA_FAKE_PATH 환경 변수가 있을 때만 쓰인다.
실제 Chroma와 같은 제약을 일부러 걸어 둔다: metadata 값은 str·int·float·bool만, where 최상위는 연산자 하나만,
$and·$or는 조건 2개 이상.
"""
from __future__ import annotations

import json
import math
import os
import threading

_lock = threading.Lock()


def _check_meta(m: dict) -> None:
    for k, v in m.items():
        if v is None or not isinstance(v, (str, int, float, bool)):
            raise ValueError(f"Expected metadata value to be a str, int, float or bool, got {v!r} for key {k}")


def _match(meta: dict, where: dict | None) -> bool:
    if not where:
        return True
    if len(where) != 1:
        raise ValueError(f"Expected where to have exactly one operator, got {where}")
    (k, v), = where.items()
    if k in ("$and", "$or"):
        if not isinstance(v, list) or len(v) < 2:
            raise ValueError(f"Expected where value for {k} to be a list with at least two where expressions")
        res = [_match(meta, x) for x in v]
        return all(res) if k == "$and" else any(res)
    val = meta.get(k)
    if isinstance(v, dict):
        (op, arg), = v.items()
        return {
            "$eq": lambda: val == arg, "$ne": lambda: val != arg, "$in": lambda: val in arg, "$nin": lambda: val not in arg,
            "$gt": lambda: val is not None and val > arg, "$gte": lambda: val is not None and val >= arg,
            "$lt": lambda: val is not None and val < arg, "$lte": lambda: val is not None and val <= arg,
        }[op]()
    return val == v


class FakeCollection:
    def __init__(self, client: "FakeHttpClient", name: str):
        self._client, self.name = client, name

    @property
    def _rows(self) -> dict:
        return self._client._state()["collections"][self.name]["rows"]

    @property
    def metadata(self) -> dict:
        return self._client._state()["collections"][self.name]["metadata"]

    def count(self) -> int:
        return len(self._rows)

    def upsert(self, ids, embeddings, documents=None, metadatas=None):
        with _lock:
            st = self._client._state()
            rows = st["collections"][self.name]["rows"]
            for i, id_ in enumerate(ids):
                m = (metadatas or [{}] * len(ids))[i] or {}
                _check_meta(m)
                rows[id_] = {"embedding": list(embeddings[i]), "document": (documents or [None] * len(ids))[i], "metadata": m}
            self._client._save(st)

    add = upsert

    def delete(self, ids=None, where=None):
        with _lock:
            st = self._client._state()
            rows = st["collections"][self.name]["rows"]
            for k in [k for k, r in rows.items() if (ids is None or k in ids) and _match(r["metadata"], where)]:
                del rows[k]
            self._client._save(st)

    def get(self, ids=None, where=None, limit=None, include=("documents", "metadatas")):
        items = [(k, r) for k, r in self._rows.items() if (ids is None or k in ids) and _match(r["metadata"], where)]
        if limit:
            items = items[:limit]
        out = {"ids": [k for k, _ in items]}
        if "documents" in include:
            out["documents"] = [r["document"] for _, r in items]
        if "metadatas" in include:
            out["metadatas"] = [r["metadata"] for _, r in items]
        return out

    def query(self, query_embeddings, n_results=10, where=None, include=("documents", "metadatas", "distances")):
        res = {"ids": [], "documents": [], "metadatas": [], "distances": []}
        for q in query_embeddings:
            qn = math.sqrt(sum(x * x for x in q)) or 1.0
            scored = []
            for k, r in self._rows.items():
                if not _match(r["metadata"], where):
                    continue
                e = r["embedding"]
                en = math.sqrt(sum(x * x for x in e)) or 1.0
                cos = sum(a * b for a, b in zip(q, e)) / (qn * en)
                scored.append((1 - cos, k, r))
            scored.sort(key=lambda x: x[0])
            top = scored[:n_results]
            res["ids"].append([k for _, k, _ in top])
            res["documents"].append([r["document"] for _, _, r in top])
            res["metadatas"].append([r["metadata"] for _, _, r in top])
            res["distances"].append([d for d, _, _ in top])
        return res


class FakeHttpClient:
    """파일 하나에 상태를 두어 적재기와 서버(다른 프로세스)가 같은 데이터를 본다."""

    def __init__(self, path: str):
        self.path = path
        if not os.path.exists(path):
            self._save({"collections": {}})

    def _state(self) -> dict:
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _save(self, st: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def heartbeat(self) -> int:
        return 1

    def list_collections(self):
        return [FakeCollection(self, n) for n in self._state()["collections"]]

    def get_or_create_collection(self, name, metadata=None, embedding_function=None):
        with _lock:
            st = self._state()
            if name not in st["collections"]:
                st["collections"][name] = {"metadata": metadata or {}, "rows": {}}
                self._save(st)
        return FakeCollection(self, name)

    def get_collection(self, name, embedding_function=None):
        if name not in self._state()["collections"]:
            raise ValueError(f"Collection {name} does not exist.")
        return FakeCollection(self, name)

    def delete_collection(self, name):
        with _lock:
            st = self._state()
            if name not in st["collections"]:
                raise ValueError(f"Collection {name} does not exist.")
            del st["collections"][name]
            self._save(st)
