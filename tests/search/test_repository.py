from __future__ import annotations

from sqlalchemy.dialects import postgresql

from reg.search.repository import SearchRepository


class FakeResult:
    def mappings(self) -> "FakeResult":
        return self

    def all(self) -> list[dict[str, object]]:
        return []


class FakeSession:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return FakeResult()


class FakeSessionContext:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeSessionFactory:
    def __init__(self) -> None:
        self.session = FakeSession()

    def __call__(self) -> FakeSessionContext:
        return FakeSessionContext(self.session)


async def test_repository_query_is_scoped_by_user_id() -> None:
    session_factory = FakeSessionFactory()
    repository = SearchRepository(session_factory)

    await repository.search(user_id="user-a", embedding=[0.1, 0.2, 0.3], top_k=7)

    assert session_factory.session.statement is not None
    compiled = str(session_factory.session.statement.compile(dialect=postgresql.dialect()))
    assert "JOIN documents" in compiled
    assert "documents.user_id = %(user_id_1)s" in compiled
    assert "documents.status = %(status_1)s" in compiled
    assert compiled.count("documents.user_id") == 1
    assert "LIMIT" in compiled
