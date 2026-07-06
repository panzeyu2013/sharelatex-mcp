from __future__ import annotations

from sharelatex_mcp.projects import ProjectClient


def test_parse_compile_logs_extracts_structured_diagnostics() -> None:
    output_log = r"""
! LaTeX Error: File `missing-figure.pdf' not found.
<to be read again>
l.18 \includegraphics{missing-figure}
LaTeX Warning: Citation `smith2024' on page 1 undefined on input line 25.
LaTeX Warning: Reference `sec:method' on page 2 undefined on input line 42.
Package hyperref Warning: Token not allowed in a PDF string.
Overfull \hbox (12.0pt too wide) in paragraph at lines 58--60
LaTeX Font Warning: Font shape `OT1/cmr/m/n' undefined.
LaTeX Warning: Label(s) may have changed. Rerun to get cross-references right.
! Undefined control sequence.
l.73 \unknownmacro
""".strip()

    bib_log = """
Warning--I didn't find a database entry for "smith2024"
I couldn't open database file refs.bib
""".strip()

    parsed = ProjectClient._parse_compile_logs(
        {
            "output_log": output_log,
            "bib_logs": [{"path": "output.blg", "content": bib_log}],
        }
    )

    by_kind = {item["kind"]: item for item in parsed}
    assert by_kind["missing-file"]["line"] == 18
    assert by_kind["undefined-control-sequence"]["line"] == 73
    assert by_kind["citation-warning"]["line"] == 25
    assert "reference-warning" in by_kind
    assert "package-warning" in by_kind
    assert by_kind["box-warning"]["line"] == 58
    assert by_kind["box-warning"]["line_end"] == 60
    assert "font-warning" in by_kind
    assert "rerun-needed" in by_kind
    assert sum(1 for item in parsed if item["kind"] == "bibtex-warning") == 2
