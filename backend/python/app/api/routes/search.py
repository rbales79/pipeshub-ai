import asyncio
from typing import TYPE_CHECKING, Any, Optional

from dependency_injector.wiring import inject
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.middlewares.auth import require_scopes
from app.config.configuration_service import ConfigurationService
from app.config.constants.service import OAuthScopes
from app.edition_config import resolve_llm_for_search
from app.modules.retrieval.retrieval_service import RetrievalService
from app.services.graph_db.interface.graph_db_provider import IGraphDBProvider
from app.telemetry.event_buffer import record_event
from app.telemetry.identity import domain_from_email
from app.utils.query_transform import setup_query_transformation

if TYPE_CHECKING:
    from app.containers.query import QueryAppContainer

router = APIRouter()


# Pydantic models
class SearchQuery(BaseModel):
    query: str
    limit: Optional[int] = 5
    filters: Optional[dict[str, Any]] = {}


class SimilarDocumentQuery(BaseModel):
    document_id: str
    limit: Optional[int] = 5
    filters: Optional[dict[str, Any]] = None


class SearchRequest(BaseModel):
    query: str
    topK: int = 20
    filtersV1: list[dict[str, list[str]]]


async def get_retrieval_service(request: Request) -> RetrievalService:
    container: QueryAppContainer = request.app.container
    return await container.retrieval_service()

async def get_graph_provider(request: Request) -> IGraphDBProvider:
    container: QueryAppContainer = request.app.container
    return await container.graph_provider()


async def get_config_service(request: Request) -> ConfigurationService:
    container: QueryAppContainer = request.app.container
    return container.config_service()


@router.post("/search", dependencies=[Depends(require_scopes(OAuthScopes.SEMANTIC_WRITE))])
@inject
async def search(
    request: Request,
    body: SearchQuery,
    retrieval_service: RetrievalService = Depends(get_retrieval_service),
    graph_provider: IGraphDBProvider = Depends(get_graph_provider),
)-> JSONResponse :
    """Perform semantic search across documents"""
    try:
        container = request.app.container
        logger = container.logger()
        llm = await resolve_llm_for_search(request, retrieval_service)

        # Extract KB IDs from filters if present
        updated_filters = body.filters

        # Setup query transformation.
        # Pin to temperature 0: the default is 0.2 (aimodels.py:1000), and 1 for
        # reasoning models, so the rewrite and the expansions differ on every
        # call and the same question returns different documents. Guarded --
        # a provider that rejects the kwarg keeps the unbound model rather than
        # failing the search outright.
        try:
            transform_llm = llm.bind(temperature=0)
        except Exception:
            transform_llm = llm
        rewrite_chain, expansion_chain = setup_query_transformation(transform_llm)

        # Run query transformations in parallel
        # SEARCH_QUERY_TRANSFORM=off searches the user's literal query and
        # nothing else -- no rewrite, no expansion, no LLM round-trip. Default
        # is "on", so upstream behaviour is unchanged unless asked for.
        #
        # The transformation is not merely noisy. Logging the queries actually
        # sent shows the prompt's own label ("Rewritten Query:") embedded as
        # search text, unfilled placeholders ("[insert PO number]",
        # "[Company Name]") embedded beside it, an instruction-shaped sentence
        # that no document chunk resembles, and invented domain terms
        # ("SAP/Oracle/Ariba") absent from the corpus. The query "test" became
        # "How to prepare for a high-school chemistry test: 4-week study
        # plan...". Two identical golden runs flipped 6 of 21 questions.
        #
        # Turning it off is what makes retrieval measurable at all.
        # Two switches, because on this bench the env var is unreachable: it
        # can only be set by recreating the container, and a recreate silently
        # reverts every patch including this one (#25). The flag FILE survives a
        # plain restart, which is the only lever a container-local patch series
        # actually has. Same tell as before -- a one-line env change costs more
        # than a code change.
        import os as _os
        _off = _os.getenv("SEARCH_QUERY_TRANSFORM", "on").strip().lower() in (
            "off", "0", "false", "no", "none"
        ) or _os.path.exists("/app/.search-transform-off")
        if _off:
            logger.info("Query transformation disabled; searching the literal query")
            rewritten_query, expanded_queries = "", ""
        else:
            rewritten_query, expanded_queries = await asyncio.gather(
                rewrite_chain.ainvoke(body.query), expansion_chain.ainvoke(body.query)
            )

        logger.debug(f"Rewritten query: {rewritten_query}")
        logger.debug(f"Expanded queries: {expanded_queries}")

        expanded_queries_list = [
            q.strip() for q in expanded_queries.split("\n") if q.strip()
        ]

        # The user's literal query goes FIRST and always. Upstream includes it
        # only when the rewrite comes back empty, so a verbatim match can lose
        # to the model's paraphrase of it -- and the words the user actually
        # chose are the one signal that is definitely about their intent.
        queries = [body.query.strip()] if body.query.strip() else []
        if rewritten_query.strip() and rewritten_query.strip() not in queries:
            queries.append(rewritten_query.strip())
        queries.extend([q for q in expanded_queries_list if q not in queries])
        results = await retrieval_service.search_with_filters(
            queries=queries,
            org_id=request.state.user.get("orgId"),
            user_id=request.state.user.get("userId"),
            limit=body.limit,
            filter_groups=updated_filters,
            knowledge_search=True,
        )
        custom_status_code = results.get("status_code", 500)
        logger.info(f"Custom status code: {custom_status_code}")

        _su_email = request.state.user.get("email")
        record_event("search_performed", {
            "orgId": request.state.user.get("orgId"),
            "userId": request.state.user.get("userId"),
            "email": _su_email,
            "domain": domain_from_email(_su_email),
            "status_code": custom_status_code,
            "num_queries": len(queries),
            "search_type": "search",
        })

        return JSONResponse(status_code=custom_status_code, content=results)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint"""
    return {"status": "healthy"}
