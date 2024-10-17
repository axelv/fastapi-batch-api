from datetime import datetime
import json
import contextvars
from typing import Annotated, Any, Awaitable, Callable, Coroutine, Literal, Sequence
from urllib.parse import ParseResult, urlparse
from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, BeforeValidator, JsonValue, ValidationError
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import RequestResponseEndpoint, BaseHTTPMiddleware
from starlette.routing import Match, Route, Router
from starlette.types import Message

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

Session = list[str]

class Transaction:
    TRANSACTION_LOG = []
    def __init__(self):
        self.session = []
        self.t:contextvars.Token[Session]

    def __enter__(self):
        self.session.append(f"start transaction {datetime.now()}")
        #self.t = self.CTX.set(self.session)
        return self.session

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.session.append(f"commit transaction {datetime.now()}")
        else:
            self.session.append(f"rollback transaction {datetime.now()} {exc_value}")
        self.TRANSACTION_LOG.append(self.session)

class SessionMiddleware(BaseHTTPMiddleware):
    transaction_class = Transaction

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        with self.transaction_class() as session:
            request.state.session = session
            response = await call_next(request)
            return response

def get_session(request:Request):
    if not hasattr(request.state, "session"):
        with Transaction() as session:
            yield session
    else:
        yield request.state.session

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
                        # Handle the request
                        entry_response = await self.handle_match(request, route, entry_scope, entry_receive, is_transaction=req_bundle.type == "transaction")
                        # Add the response to the bundle
                        response_entries.append(entry_response)

            return JSONResponse(
                content=ResBundle(
                    resourceType="Bundle",
                    type="batch-response" if req_bundle.type == "batch" else "transaction-response",
                    entry=response_entries
                ).model_dump(mode="json", by_alias=True, exclude_none=True)
            )

        return handle_request_bundle

    async def handle_match(self, request:Request, route:FHIRRoute, scope:dict, receive:Callable[[], Awaitable[Message]], is_transaction:bool=False):
        entry_request = Request(scope, receive=receive)
        handler = route.get_route_handler()

        if is_transaction:
            entry_request.state.session = request.state.session
        try:
            response = await handler(entry_request)
            if not (isinstance(response, JSONResponse) and  isinstance(response.body, bytes)):
                raise ValueError("FHIRRoute must return a JSONResponse with bytes body")
        except ValidationError as exc:
            return ResBundleEntry(
                    response=ResBundleResponse(
                        status="400",
                        outcome={
                            "resourceType": "OperationOutcome",
                            "issue": [
                                {
                                    "severity": "error",
                                    "code": "exception",
                                    "diagnostics": str(error)
                                } for error in exc.errors()
                            ]
                        }
                    )
                )
        except HTTPException as exc:
            return ResBundleEntry(
                    response=ResBundleResponse(
                        status=str(exc.status_code),
                        outcome={
                            "resourceType": "OperationOutcome",
                            "issue": [
                                {
                                    "severity": "error",
                                    "code": "exception",
                                    "diagnostics": str(exc.detail)
                                }
                            ]
                        }
                    ),
                )
        except Exception:
            return ResBundleEntry(
                    response=ResBundleResponse(
                        status="500",
                    )
                )
        else:
            return ResBundleEntry(
                    response=ResBundleResponse(
                        status=str(response.status_code),
                        etag=response.headers.get("ETag", None),
                        lastModified=response.headers.get("Last-Modified", None),
                        location=response.headers.get("Location", None),
                    ),
                    resource=json.loads(response.body.decode()) if response.body else None
                )

fhir_router = APIRouter(route_class=FHIRRoute)

@fhir_router.get("/{resource_type}/{resource_id}", status_code=status.HTTP_200_OK)
def get_resource(resource_type: str, resource_id:str, session: Annotated[list[str], Depends(get_session)], response: Response):
    session.append(f"get {resource_type}/{resource_id}")
    return {"resourceType": resource_type, "id": resource_id}

@fhir_router.post("/{resource_type}", status_code=status.HTTP_201_CREATED)
def create_resource(resource_type: str, session: Annotated[list[str], Depends(get_session)], response: Response):
    session.append(f"create {resource_type}")
    response.headers["Location"] = f"/{resource_type}/1"
    return {"resourceType": resource_type}

@fhir_router.put("/{resource_type}/{resource_id}", status_code=status.HTTP_200_OK)
def update_resource(resource_type: str, resource_id: str, session:Annotated[list[str], Depends(get_session)], response: Response):
    session.append(f"update {resource_type}/{resource_id}")
    if resource_type == "Error":
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")
    return {"resourceType": resource_type, "id": resource_id}

app.include_router(fhir_router)
app.add_middleware(SessionMiddleware)
app.routes.append(TransactionRoute(fhir_router))
