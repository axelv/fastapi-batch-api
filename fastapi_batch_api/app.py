import json
from typing import Annotated, Any, Callable, Coroutine, Iterable, Literal, Sequence
from urllib.parse import ParseResult, urlparse
from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.applications import BaseHTTPMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, ASGIApp
from pydantic import BaseModel, BeforeValidator, JsonValue, ValidationError
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.routing import Match, Route, Router
from starlette.types import Message, Receive, Scope, Send

app = FastAPI()

URL = Annotated[ParseResult, BeforeValidator(urlparse)]

class ReqBundleRequest(BaseModel):
    method: Literal["GET", "POST", "PUT", "DELETE"]
    url:URL
    ifNoneMatch: str|None = None
    ifModifiedSince: str|None = None
    ifMatch: str|None = None
    ifNoneExist: str|None = None


class ReqBundleEntry(BaseModel):
    resource: JsonValue | None = None
    request: ReqBundleRequest

class ReqBundle(BaseModel):
    resourceType: Literal["Bundle"]
    type: Literal["transaction", "batch"]
    entry: list[ReqBundleEntry]

class ResBundleResponse(BaseModel):
    status: str
    location: str | None = None
    etag: str | None = None
    lastModified: str | None = None
    outcome: dict | None = None

class ResBundleEntry(BaseModel):
    response: ResBundleResponse
    resource: JsonValue | None = None

class ResBundle(BaseModel):
    resourceType: Literal["Bundle"]
    type: Literal["transaction-response", "batch-response"]
    entry: list[ResBundleEntry]

class FHIRRoute(APIRoute):
    pass

dependency_value = 0
def fake_dependency():
    global dependency_value
    dependency_value += 1
    yield dependency_value


class TransactionRoute(Route):
    def __init__(self, fhir_router:Router, *,path: str = "/", methods: list[str] | None = None, name: str | None = None, include_in_schema: bool = True, middleware: Sequence[Middleware] | None = None) -> None:
        methods = methods or ["POST"]
        name = name or "execute_transaction"
        self.fhir_routes = [route for route in fhir_router.routes if isinstance(route, FHIRRoute)]

        super().__init__(path, self.get_route_handler(), methods=methods, name=name, include_in_schema=include_in_schema)

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:

        async def handle_request_bundle(request: Request):
            req_bundle = ReqBundle.model_validate(await request.json())
            response_entries = []
            for entry in req_bundle.entry:


                # setup the ASGI scope and receive function

                message:Message = {
                    "type": "http.request",
                }

                headers = {}
                if entry.request.ifNoneMatch is not None:
                    headers["If-None-Match"] = entry.request.ifNoneMatch
                if entry.request.ifModifiedSince is not None:
                    headers["If-Modified-Since"] = entry.request.ifModifiedSince
                if entry.request.ifMatch is not None:
                    headers["If-Match"] = entry.request.ifMatch
                if entry.request.ifNoneExist is not None:
                    headers["If-None-Exist"] = entry.request.ifNoneExist
                if entry.resource:
                    message["body"] = json.dumps(entry.resource).encode()
                    headers["Content-Type"] = "application/fhir+json"
                    headers["Content-Length"] = str(len(message["body"]))

                async def entry_receive():
                    return message

                entry_scope={
                    "type": "http",
                    "scheme": request.scope["scheme"],
                    "http_version": request.scope["http_version"],
                    "method": entry.request.method,
                    "path": "/"+entry.request.url.path,
                    "query_string": entry.request.url.query,
                    "headers": Headers(headers)
                }

                # Find a matching route for the incoming request
                for route in self.fhir_routes:
                    match, child_scope = route.matches(entry_scope)
                    if match == Match.FULL:
                        entry_scope.update(child_scope)
                        entry_request = Request(entry_scope, receive=entry_receive)
                        handler = route.get_route_handler()
                        response = await handler(entry_request)
                        if not (isinstance(response, JSONResponse) and  isinstance(response.body, bytes)):
                            raise ValueError("FHIRRoute must return a JSONResponse with bytes body")
                        response_entries.append(
                            ResBundleEntry(
                                response=ResBundleResponse(
                                    status=str(response.status_code),
                                    etag=response.headers.get("ETag", None),
                                    lastModified=response.headers.get("Last-Modified", None),
                                    location=response.headers.get("Location", None),
                                ),
                                resource=json.loads(response.body.decode()) if response.body else None
                            )
                        )

            return JSONResponse(
                content=ResBundle(
                    resourceType="Bundle",
                    type="batch-response" if req_bundle.type == "batch" else "transaction-response",
                    entry=response_entries
                ).model_dump(mode="json", by_alias=True, exclude_none=True)
            )

        return handle_request_bundle



fhir_router = APIRouter(route_class=FHIRRoute)

@fhir_router.get("/{resource_type}/{resource_id}", status_code=status.HTTP_200_OK)
def get_resource(resource_type: str, resource_id:str, dependancy: Annotated[int, Depends(fake_dependency)], response: Response):
    return {"resourceType": resource_type, "id": resource_id}

@fhir_router.post("/{resource_type}", status_code=status.HTTP_201_CREATED)
def create_resource(resource_type: str, dependancy: Annotated[int, Depends(fake_dependency)], response: Response):
    response.headers["Location"] = f"/{resource_type}/1"
    return {"resourceType": resource_type}

@fhir_router.put("/{resource_type}/{resource_id}", status_code=status.HTTP_200_OK)
def update_resource(resource_type: str, resource_id: str, dependency:Annotated[int, Depends(fake_dependency)], response: Response):
    return {"resourceType": resource_type, "id": resource_id}

app.include_router(fhir_router)
app.routes.append(TransactionRoute(fhir_router))
