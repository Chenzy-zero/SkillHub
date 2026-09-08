from __future__ import annotations

from skill_batch_review.indexed_html_reporting import write_html_report


def test_report_uses_windows_cjk_first_typography(tmp_path):
    output = write_html_report([], tmp_path / "report.html", batch_id="batch-typography")
    page = output.read_text(encoding="utf-8")

    assert (
        '--sans:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC",'
        '"Noto Sans CJK SC","Source Han Sans SC","Segoe UI",Arial,sans-serif'
        in page
    )
    assert "font:15px/1.65 var(--sans)" in page
    assert ".data-table th{font-size:12px;font-weight:600" in page
    assert ".data-table td{font-size:13px;line-height:1.6}" in page
    assert ".badge{font-size:12px;font-weight:700;padding:4px 9px}" in page
    assert ".finding-card p,.evidence-note{font-size:13px}" in page


def test_report_typography_keeps_offline_single_file_contract(tmp_path):
    output = write_html_report([], tmp_path / "report.html", batch_id="batch-offline")
    page = output.read_text(encoding="utf-8")

    assert "@font-face" not in page
    assert "fonts.googleapis.com" not in page
    assert "fonts.gstatic.com" not in page
    assert "anchor.style.fontSize = '12px'" in page
