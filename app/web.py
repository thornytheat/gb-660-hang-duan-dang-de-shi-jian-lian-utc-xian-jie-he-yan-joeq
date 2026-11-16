from fastapi import Request

def page(request: Request, template: str, title: str, endpoint: str, rows: list[dict], context: dict | None = None):
    return request.app.state.templates.TemplateResponse(
        request,
        template,
        {"title": title, "endpoint": endpoint, "rows": rows, "version": request.app.state.version, **(context or {})},
    )
