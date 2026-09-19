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
        # Knowledge Forge patch 22 (#97): retrieve WIDE, rerank, trim.
        #
        # Golden v7 on this index: recall@10 85%, recall@20 94% -- three of the
        # five k=10 misses are retrieved but mis-ordered (ros-sow-total rank 19,
        # ros-discounts 11, paychex-ssp-nonprod 11). Reranking only the top
        # `limit` cannot reach them: the first cut of this patch reranked 9-14
        # candidates for limit=10 and moved nothing. A reranker is worth having
        # only over a candidate pool WIDER than the answer set, so when the flag
        # is on the retrieve is widened to RERANK_CANDIDATES (default 40) and the
        # reranked list is trimmed back to the caller's limit.
        #
        # Uses the CrossEncoder PipesHub already ships (containers/query.py ->
        # RerankerService, BAAI/bge-reranker-base); the agent path consumes it,
        # the search path never had a call site. Two switches because setting an
        # env var needs a container recreate, and a recreate reverts this patch
        # series (#25). `records` is untouched: it is built from a SET of record
        # ids and carries no relevance order. Fails OPEN -- on any error the
        # retriever's own order and limit stand.
        import os as _os
        _rerank_on = _os.getenv("SEARCH_RERANK", "off").strip().lower() in (
            "on", "1", "true", "yes"
        ) or _os.path.exists("/app/.search-rerank-on")
        _limit = body.limit
        _pool = _limit
        if _rerank_on:
            try:
                _pool = max(int(_limit or 0), int(_os.getenv("RERANK_CANDIDATES", "40")))
            except (TypeError, ValueError):
                _pool = _limit

        results = await retrieval_service.search_with_filters(
            queries=queries,
            org_id=request.state.user.get("orgId"),
            user_id=request.state.user.get("userId"),
            limit=_pool,
            filter_groups=updated_filters,
            knowledge_search=True,
        )

        if _rerank_on and isinstance(results, dict) and results.get("searchResults"):
            try:
                import asyncio as _asyncio
                import json as _json
                import urllib.request as _urlreq

                from app.config.constants.service import config_node_constants as _cnc

                _cfg_service = request.app.container.config_service()
                _ai = await _cfg_service.get_config(_cnc.AI_MODELS.value, use_cache=False)
                _key = ""
                for _entry in ((_ai or {}).get("llm") or []) + ((_ai or {}).get("embedding") or []):
                    if (_entry.get("provider") or "").lower() == "openrouter":
                        _key = ((_entry.get("configuration") or {}).get("apiKey") or "").strip()
                        if _key:
                            break
                if not _key:
                    raise RuntimeError("no openRouter apiKey in /services/aiModels")

                _docs, _keep = [], []
                for _i, _r in enumerate(results["searchResults"]):
                    _c = _r.get("content")
                    if isinstance(_c, list) and _c:
                        _c = _c[0]
                    if isinstance(_c, str) and _c.strip():
                        _docs.append(_c[:4000])
                        _keep.append(_i)
                if not _docs:
                    raise RuntimeError("no rerankable content in searchResults")

                _model = _os.getenv("RERANK_MODEL", "voyageai/rerank-2.5-lite")
                _payload = _json.dumps({
                    "model": _model, "query": body.query,
                    "documents": _docs, "top_n": _limit or len(_docs),
                }).encode()

                def _call() -> dict:
                    _req = _urlreq.Request(
                        "https://openrouter.ai/api/v1/rerank", data=_payload,
                        headers={"Authorization": "Bearer " + _key,
                                 "Content-Type": "application/json"},
                    )
                    with _urlreq.urlopen(_req, timeout=20) as _resp:
                        return _json.loads(_resp.read())

                _rr = await _asyncio.to_thread(_call)
                _ranked = []
                for _hit in (_rr.get("results") or []):
                    _idx = _hit.get("index")
                    if isinstance(_idx, int) and 0 <= _idx < len(_keep):
                        _doc = results["searchResults"][_keep[_idx]]
                        _doc["reranker_score"] = _hit.get("relevance_score")
                        _ranked.append(_doc)
                if not _ranked:
                    raise RuntimeError("rerank returned no usable indices")

                _before = len(results["searchResults"])
                results["searchResults"] = _ranked[: _limit or None]
                logger.info(
                    "Reranked %d candidates -> %d via %s (pool=%s limit=%s cost=%s)",
                    _before, len(results["searchResults"]), _model, _pool, _limit,
                    (_rr.get("usage") or {}).get("cost"),
                )
            except Exception as _rerank_error:
                logger.warning(
                    "Rerank skipped (%s: %s); keeping retriever order",
                    type(_rerank_error).__name__, _rerank_error,
                )
                if isinstance(results, dict) and isinstance(results.get("searchResults"), list):
                    results["searchResults"] = results["searchResults"][: _limit or None]

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
