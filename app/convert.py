"""转 PDF：Word 系列 Windows 优先用本机 Word（COM），否则用 LibreOffice；图片用 PyMuPDF 直接转。

统一转成 PDF 后，解析、页码、bbox 和前端预览都走同一套坐标。
"""
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf

from . import config

log = logging.getLogger(__name__)


def to_pdf(src: Path, dst: Path):
    # 先写临时文件再改名：转换中途失败不会留下半截 PDF 被"重新解析"当成已转换的结果复用
    tmp = dst.with_name(dst.stem + ".part.pdf")
    try:
        _convert(src, tmp)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def _convert(src: Path, dst: Path):
    if src.suffix.lower() in config.IMAGE_EXTS:
        _image(src, dst)
        return
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


def _image(src: Path, dst: Path):
    """图片 → PDF，每张图一页（多页 TIFF 每帧一页），页面大小按图片分辨率，EXIF 方向自动转正。"""
    with pymupdf.open(src) as img:
        data = img.convert_to_pdf()
    with pymupdf.open("pdf", data) as pdf:
        if not pdf.page_count:
            raise RuntimeError("图片里没有可用的内容")
        pdf.save(dst)


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
        # 独立的用户配置目录：用户自己开着 LibreOffice 时，默认配置会把转换交给已有实例，命令直接返回却不出文件
        profile = (Path(tempfile.gettempdir()) / "docsearch-lo-profile").resolve().as_uri()
        subprocess.run(
            [soffice, f"-env:UserInstallation={profile}", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(src)],
            check=True, timeout=600, capture_output=True,
        )
        out = Path(tmp) / (src.stem + ".pdf")
        if not out.exists():
            raise RuntimeError("LibreOffice 未生成 PDF")
        shutil.move(out, dst)
