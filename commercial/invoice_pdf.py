"""Render the customer-facing PDF from an issued invoice's frozen snapshot."""

from pathlib import Path

from django.template.loader import render_to_string

TEMPLATE = "commercial/pdf/invoice.html"
STYLESHEET = Path(__file__).resolve().parent / "static/commercial/invoice.css"
MAX_PDF_BYTES = 10 * 1024 * 1024


class InvoiceRenderError(Exception):
    pass


def render_html(snapshot):
    # Django escapes all text from the invoice; the template references no external assets.
    return render_to_string(TEMPLATE, {"invoice": snapshot})


def render_pdf(snapshot):
    try:
        from weasyprint import CSS, HTML
        from weasyprint.urls import URLFetcher

        class NoFetch(URLFetcher):
            def fetch(self, url, headers=None):
                raise ValueError("Invoices cannot load external resources.")

        fetcher = NoFetch()
        html = HTML(string=render_html(snapshot), url_fetcher=fetcher)
        css = CSS(string=STYLESHEET.read_text(encoding="utf-8"), url_fetcher=fetcher)
        pdf = html.write_pdf(stylesheets=[css])
    except Exception as exc:
        raise InvoiceRenderError(
            "The invoice PDF could not be rendered. Try again or contact an administrator."
        ) from exc
    if not pdf or len(pdf) > MAX_PDF_BYTES:
        raise InvoiceRenderError("The invoice PDF is empty or larger than 10 MiB.")
    return pdf
