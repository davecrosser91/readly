"""parse_epub: front-matter skipping and heading normalization."""
import io
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import parse_epub  # noqa: E402

FILLER = "Palabras suficientes para superar el filtro de longitud. " * 6  # > 200 chars


def make_epub(spine, guide=()):
    """Build a minimal EPUB. spine = [(href, html_body)], guide = [(type, href)]."""
    items = "".join(
        f'<item id="it{i}" href="{href}" media-type="application/xhtml+xml"/>'
        for i, (href, _) in enumerate(spine))
    itemrefs = "".join(f'<itemref idref="it{i}"/>' for i in range(len(spine)))
    refs = "".join(f'<reference type="{t}" href="{h}"/>' for t, h in guide)
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Test</dc:title><dc:creator>Autor</dc:creator><dc:language>es</dc:language>
  </metadata>
  <manifest>{items}</manifest>
  <spine>{itemrefs}</spine>
  <guide>{refs}</guide>
</package>"""
    container = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("content.opf", opf)
        for href, body in spine:
            zf.writestr(href, f"<html><body>{body}</body></html>")
    return buf.getvalue()


class ParseEpubTest(unittest.TestCase):
    def test_skips_guide_declared_front_matter(self):
        raw = make_epub(
            spine=[
                ("cubierta.xhtml", f"<p>{FILLER}</p>"),
                ("titulo.xhtml", f"<p>{FILLER}</p>"),
                ("info.xhtml", f"<p>{FILLER}</p>"),
                ("miindex.xhtml", f"<p>{FILLER}</p>"),
                ("cap1.xhtml", f"<h1>Uno</h1><p>{FILLER}</p>"),
            ],
            guide=[("cover", "cubierta.xhtml"), ("title-page", "titulo.xhtml"),
                   ("copyright-page", "info.xhtml"), ("toc", "miindex.xhtml")],
        )
        _, chapters = parse_epub(raw)
        self.assertEqual([t for t, _ in chapters], ["Uno"])

    def test_skips_front_matter_by_filename(self):
        raw = make_epub(spine=[
            ("Text/sinopsis.xhtml", f"<p>{FILLER}</p>"),
            ("Text/TOC.xhtml", f"<p>{FILLER}</p>"),
            ("Text/Cap_1.xhtml", f"<h1>Uno</h1><p>{FILLER}</p>"),
        ])
        _, chapters = parse_epub(raw)
        self.assertEqual([t for t, _ in chapters], ["Uno"])

    def test_normalizes_number_glued_to_heading(self):
        raw = make_epub(spine=[
            ("c1.xhtml", f"<h1>1DESTINO: LA FELICIDAD</h1><p>{FILLER}</p>"),
            ("c2.xhtml", f"<h1>3 EL CORTISOL</h1><p>{FILLER}</p>"),
        ])
        _, chapters = parse_epub(raw)
        self.assertEqual([t for t, _ in chapters],
                         ["1 DESTINO: LA FELICIDAD", "3 EL CORTISOL"])

    def test_keeps_real_chapters(self):
        raw = make_epub(spine=[
            ("capitulo_9.xhtml", f"<h1>9TU MEJOR VERSIÓN</h1><p>{FILLER}</p>"),
        ])
        _, chapters = parse_epub(raw)
        self.assertEqual([t for t, _ in chapters], ["9 TU MEJOR VERSIÓN"])


if __name__ == "__main__":
    unittest.main()
