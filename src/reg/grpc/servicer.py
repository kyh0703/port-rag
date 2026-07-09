"""gRPC RegService implementation."""

from __future__ import annotations

import grpc
from port.reg.v1 import reg_pb2
from port.reg.v1 import reg_pb2_grpc

from reg.search.service import InvalidSearchRequest
from reg.search.service import SearchService
from reg.search.types import SearchHit


class RegSearchServicer(reg_pb2_grpc.RegServiceServicer):
    def __init__(self, service: SearchService) -> None:
        self._service = service

    async def Search(
        self,
        request: reg_pb2.SearchRequest,
        context: grpc.aio.ServicerContext,
    ) -> reg_pb2.SearchResponse:
        try:
            hits = await self._service.search(
                user_id=request.user_id,
                query=request.query,
                top_k=request.top_k,
            )
        except InvalidSearchRequest as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise

        return reg_pb2.SearchResponse(results=[_to_response_hit(hit) for hit in hits])


def add_reg_search_servicer(server: grpc.aio.Server, service: SearchService) -> None:
    reg_pb2_grpc.add_RegServiceServicer_to_server(RegSearchServicer(service), server)


def _to_response_hit(hit: SearchHit) -> reg_pb2.SearchResult:
    return reg_pb2.SearchResult(
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        document_name=hit.document_name,
        text=hit.text,
        score=hit.score,
        metadata=hit.metadata,
        seq=hit.seq,
    )
