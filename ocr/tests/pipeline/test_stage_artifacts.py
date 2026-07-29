from app.pipeline_core import PageSegmentArtifact, PdfTextLayerArtifact


def test_page_segment_control_state_does_not_depend_on_diagnostic_flags():
    artifact = PageSegmentArtifact(
        markdown="text",
        counters=(("chunks", 1),),
        flags=("markdown_lint:fail", "arbitrary:diagnostic"),
        structural_lint_pass=True,
        structural_confirmed=True,
    )

    assert artifact.structural_lint_pass is True
    assert artifact.structural_confirmed is True
    assert artifact.metadata()["runtime_flags"] == [
        "markdown_lint:fail",
        "arbitrary:diagnostic",
    ]


def test_pdf_layout_step_does_not_depend_on_diagnostic_flags():
    artifact = PdfTextLayerArtifact(
        pages=("text",),
        counters=(("tables_found", 0),),
        flags=("pdf_text_layer:fixed_width_markdown",),
        layout_step="pdf_text_layer_layout",
    )

    assert artifact.layout_step == "pdf_text_layer_layout"
