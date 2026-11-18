"""Render invoices to HTML and PDF with Jinja2 + WeasyPrint."""

from __future__ import annotations

from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, select_autoescape

from .config import Config


def _num(value, places: int = 2) -> str:
    return f"{value:,.{places}f}"


class Renderer:
    def __init__(self, cfg: Config):
        loaders = []
        if cfg.template_dir:
            loaders.append(FileSystemLoader(str(cfg.template_dir)))
        loaders.append(PackageLoader("pve_invoicer", "templates"))
        self.env = Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(["html", "j2"]))
        self.env.filters["num"] = _num
        self.base_url = str(cfg.template_dir or Path(__file__).parent / "templates")

    def html(self, **ctx) -> str:
        return self.env.get_template("invoice.html.j2").render(**ctx)

    def pdf(self, path: Path, **ctx) -> bytes:
        from weasyprint import HTML  # imported lazily: heavy, and not needed for tests of HTML

        data = HTML(string=self.html(**ctx), base_url=self.base_url).write_pdf()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def email_body(self, **ctx) -> str:
        return self.env.get_template("invoice_email.txt.j2").render(**ctx)
