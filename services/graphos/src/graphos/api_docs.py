"""The API documentation pages, served from graphos's own files.

Swagger UI (``/docs``) and ReDoc (``/redoc``) as FastAPI renders them, but
with their scripts, stylesheet and icon served by graphos from
``graphos/static/api-docs`` (``tools/docs/api_docs_assets.py`` puts the
pinned releases there), so a browser without internet access shows them
too, and ReDoc without Google Fonts. Swagger UI's online validator is off:
it would send the address of the schema to validator.swagger.io. Like
``/openapi.json``, the pages need no token: they describe the API, never
data.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

ASSETS = Path(__file__).parent / "static" / "api-docs"
ASSETS_PATH = "/api-docs-assets"
DOCS_PATH = "/docs"
REDOC_PATH = "/redoc"
OAUTH2_REDIRECT_PATH = "/docs/oauth2-redirect"


def install(app: FastAPI) -> None:
    """Serve ``/docs`` and ``/redoc`` from local files (create the app with
    ``docs_url=None, redoc_url=None``)."""
    app.mount(ASSETS_PATH, StaticFiles(directory=ASSETS, check_dir=False), name="api-docs-assets")
    favicon = f"{ASSETS_PATH}/favicon-32x32.png"

    @app.get(DOCS_PATH, include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{app.title} – API",
            oauth2_redirect_url=OAUTH2_REDIRECT_PATH,
            swagger_js_url=f"{ASSETS_PATH}/swagger-ui-bundle.js",
            swagger_css_url=f"{ASSETS_PATH}/swagger-ui.css",
            swagger_favicon_url=favicon,
            swagger_ui_parameters={"validatorUrl": None},
        )

    @app.get(OAUTH2_REDIRECT_PATH, include_in_schema=False)
    async def swagger_ui_redirect() -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    @app.get(REDOC_PATH, include_in_schema=False)
    async def redoc() -> HTMLResponse:
        return get_redoc_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{app.title} – API",
            redoc_js_url=f"{ASSETS_PATH}/redoc.standalone.js",
            redoc_favicon_url=favicon,
            with_google_fonts=False,
        )
