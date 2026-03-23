from memory.scoped_chroma_store import ChromaMemory


def _matches_where(metadata, where):
    if where is None:
        return True
    if "$and" in where:
        return all(_matches_where(metadata, item) for item in where["$and"])
    for key, value in where.items():
        if metadata.get(key) != value:
            return False
    return True


class _FakeCollection:
    def __init__(self):
        self.records = {}

    def add(self, ids, documents, metadatas):
        for memory_id, document, metadata in zip(ids, documents, metadatas):
            if memory_id in self.records:
                raise ValueError("duplicate id")
            self.records[memory_id] = {
                "document": document,
                "metadata": dict(metadata),
            }

    def upsert(self, ids, documents, metadatas):
        for memory_id, document, metadata in zip(ids, documents, metadatas):
            self.records[memory_id] = {
                "document": document,
                "metadata": dict(metadata),
            }

    def query(self, query_texts, n_results, where=None):
        query = str(query_texts[0] or "").lower()
        ranked = []
        for memory_id, record in self.records.items():
            if not _matches_where(record["metadata"], where):
                continue
            haystack = f"{record['document']} {record['metadata']}".lower()
            score = sum(1 for token in query.split() if token and token in haystack)
            ranked.append((score, memory_id, record))
        ranked.sort(key=lambda item: (item[0], item[2]["metadata"].get("updated_at", "")), reverse=True)
        selected = ranked[:n_results]
        return {
            "ids": [[item[1] for item in selected]],
            "documents": [[item[2]["document"] for item in selected]],
            "metadatas": [[dict(item[2]["metadata"]) for item in selected]],
            "distances": [[float(max(0, 10 - item[0])) for item in selected]],
        }

    def get(self, ids=None, where=None):
        selected = []
        if ids is not None:
            for memory_id in ids:
                if memory_id in self.records:
                    selected.append((memory_id, self.records[memory_id]))
        else:
            for memory_id, record in self.records.items():
                if _matches_where(record["metadata"], where):
                    selected.append((memory_id, record))
        return {
            "ids": [item[0] for item in selected],
            "documents": [item[1]["document"] for item in selected],
            "metadatas": [dict(item[1]["metadata"]) for item in selected],
        }

    def delete(self, ids):
        for memory_id in ids:
            self.records.pop(memory_id, None)

    def count(self):
        return len(self.records)


class _FakeClient:
    def __init__(self, collection):
        self.collection = collection

    def delete_collection(self, _name):
        self.collection.records.clear()

    def get_or_create_collection(self, name, metadata=None):
        del name, metadata
        return self.collection


def _make_store():
    collection = _FakeCollection()
    store = ChromaMemory.__new__(ChromaMemory)
    store.name = "ChromaMemory"
    store.collection_name = "test_memory"
    store._ChromaMemory__collection = collection  # backs the lazy _collection property
    store._client = _FakeClient(collection)
    return store


def test_scoped_search_does_not_cross_sessions():
    store = _make_store()
    scope_a = {"session_id": "session_a"}
    scope_b = {"session_id": "session_b"}

    store.save_task_result("summarize alpha note", "alpha result", True, scope=scope_a)
    store.save_task_result("summarize beta note", "beta result", True, scope=scope_b)

    results = store.search_memory(
        "alpha note",
        scope=scope_a,
        include_global_fallback=False,
    )

    assert len(results) == 1
    assert results[0]["metadata"]["scope_session_id"] == "session_a"
    assert "alpha result" in results[0]["content"]


def test_preference_upsert_reuses_stable_memory_id():
    store = _make_store()
    scope = {"session_id": "session_a"}

    first_id = store.save_user_preference("preferred_tools", "file.read_write", scope=scope)
    second_id = store.save_user_preference("preferred_tools", "browser.interact", scope=scope)

    stats = store.get_stats(scope=scope)
    record = store._get_single_record(first_id)

    assert first_id == second_id
    assert stats["total_memories"] == 1
    assert "browser.interact" in record["content"]
    assert record["metadata"]["revision"] == 2


def test_clear_scope_only_removes_matching_memories():
    store = _make_store()
    scoped = {"session_id": "session_a"}

    store.save_task_result("task a", "result a", True, scope=scoped)
    store.save_task_result("task global", "global result", True, scope={})

    cleared = store.clear_scope(scoped)
    global_stats = store.get_stats()

    assert cleared == 1
    assert global_stats["total_memories"] == 1
    remaining = store.get_recent_memories()
    assert remaining[0]["metadata"]["scope_level"] == "global"


def test_scoped_search_falls_back_to_session_preferences():
    store = _make_store()
    store.save_user_preference(
        "preferred_tools",
        "browser.interact",
        scope={"session_id": "session_a"},
    )

    results = store.search_memory(
        "preferred tools",
        scope={
            "session_id": "session_a",
            "goal_id": "goal_1",
            "project_id": "project_1",
        },
        include_global_fallback=True,
    )

    assert results
    assert results[0]["scope_match"] == "session"
    assert results[0]["metadata"]["scope_session_id"] == "session_a"
