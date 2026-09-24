"""Word 系列文档转 PDF：Windows 优先用本机 Word（COM），否则用 LibreOffice。

统一转成 PDF 后，解析、页码、bbox 和前端预览都走同一套坐标。
"""
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def to_pdf(src: Path, dst: Path):
    errors = []
    if sys.platform == "win32":
        try:
            _word(src, dst)
            return
        except Exception as e:  # Word 未安装或打开失败时回退 LibreOffice
            log.warning("Word 转换失败，尝试 LibreOffice：%s", e)
            errors.append(f"Word: {e}")
    soffice = _soffice_path()
    if soffice:
        _libreoffice(soffice, src, dst)
        return
    errors.append("未找到 LibreOffice")
    raise RuntimeError("无法转换为 PDF（" + "；".join(errors) + "）")


def _word(src: Path, dst: Path):
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    try:
        # DispatchEx 起独立进程，不影响用户已打开的 Word
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        # 传一个假密码：带密码的文档直接报错，而不是弹框卡住
        doc = word.Documents.Open(
            str(src), ConfirmConversions=False, ReadOnly=True,
            AddToRecentFiles=False, PasswordDocument="__nopass__",
        )
        try:
            doc.ExportAsFixedFormat(str(dst), 17)  # wdExportFormatPDF
        finally:
            doc.Close(0)
    finally:
        if word is not None:
            word.Quit()
        pythoncom.CoUninitialize()


def _soffice_path() -> str | None:
    for name in ("soffice", "libreoffice"):
        if p := shutil.which(name):
            return p
    for p in (r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        if Path(p).exists():
            return p
    return None


def _libreoffice(soffice: str, src: Path, dst: Path):
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(src)],
            check=True, timeout=600, capture_output=True,
        )
        out = Path(tmp) / (src.stem + ".pdf")
        if not out.exists():
            raise RuntimeError("LibreOffice 未生成 PDF")
        shutil.move(out, dst)
