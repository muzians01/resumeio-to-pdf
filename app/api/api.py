import json
from typing import Annotated

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.resumeio import ResumeioRenderer

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.post("/download")
def download_resume(
    document: Annotated[str, Form()],
):
    """Download a resume from a document JSON payload and return it as a PDF."""
    document_dict = json.loads(document)
    rendering_token = document_dict.get("renderingToken", "resume")
    renderer = ResumeioRenderer(document=document_dict)
    return Response(
        renderer.generate_pdf(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{rendering_token}.pdf"'},
    )


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request):
    """Render the main index page."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {"request": request},
    )
