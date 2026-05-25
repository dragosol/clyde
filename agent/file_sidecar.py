"""
Clyde — File Sidecar
============================
Lightweight file format handler. Lazy-loaded — only imported when
an attachment arrives or the model calls a create_* tool.

INPUT:  extract_content(data_bytes, mime, ext) → plain text
OUTPUT: create_docx/xlsx/pptx/pdf(structured_data) → file path

All output files go to ~/.clyde/output/
"""

from __future__ import annotations
import base64
import logging
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from datetime import datetime

log = logging.getLogger("file_sidecar")

OUTPUT_DIR = Path.home() / ".clyde" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════
# INPUT: Extract text from file bytes
# ═══════════════════════════════════════════════════════════════

def extract_from_data_url(data_url: str) -> tuple[str, str]:
    """
    Decode a data URL, detect file type, extract text.
    Returns (extracted_text, file_type_label).
    """
    mime = ""
    raw_b64 = data_url

    if data_url.startswith("data:"):
        header, raw_b64 = data_url.split(",", 1) if "," in data_url else ("", data_url)
        mime = header.replace("data:", "").replace(";base64", "")

    try:
        file_bytes = base64.b64decode(raw_b64)
    except Exception as e:
        return f"[could not decode attachment: {e}]", "error"

    return extract_from_bytes(file_bytes, mime)


def extract_from_bytes(file_bytes: bytes, mime: str = "") -> tuple[str, str]:
    """Extract text from raw file bytes. Returns (text, type_label)."""
    ext = mimetypes.guess_extension(mime) if mime else ""

    # Detect from magic bytes if mime didn't help
    if not ext or ext == ".bin":
        if file_bytes[:4] == b'PK\x03\x04':
            ext = ".zip"  # docx/xlsx/pptx are all ZIP
        elif file_bytes[:5] == b'%PDF-':
            ext = ".pdf"
        elif file_bytes[:2] in (b'\xff\xfe', b'\xfe\xff') or all(b < 128 for b in file_bytes[:200]):
            ext = ".txt"

    # Save to temp for library-based extraction
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir="/tmp")
    tmp.write(file_bytes)
    tmp.close()

    try:
        # PDF
        if ext == ".pdf" or "pdf" in mime:
            return _extract_pdf(tmp.name), "pdf"

        # Try DOCX first for ZIP-based files
        if ext in (".zip", ".docx") or "wordprocessing" in mime or "document" in mime:
            try:
                text = _extract_docx(tmp.name)
                if text.strip():
                    return text, "docx"
            except Exception:
                pass

        # Try XLSX
        if ext in (".zip", ".xlsx") or "spreadsheet" in mime or "sheet" in mime:
            try:
                text = _extract_xlsx(tmp.name)
                if text.strip():
                    return text, "xlsx"
            except Exception:
                pass

        # Try PPTX
        if ext in (".zip", ".pptx") or "presentation" in mime:
            try:
                text = _extract_pptx(tmp.name)
                if text.strip():
                    return text, "pptx"
            except Exception:
                pass

        # Plain text / code files
        if ext in (".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".py",
                   ".sh", ".js", ".ts", ".html", ".css", ".xml", ".toml",
                   ".ini", ".cfg", ".log", ".sql", ".r", ".swift"):
            return file_bytes.decode("utf-8", errors="replace")[:15000], "text"

        # Last resort: try all extractors
        for extractor, label in [(_extract_docx, "docx"), (_extract_xlsx, "xlsx"), (_extract_pptx, "pptx")]:
            try:
                text = extractor(tmp.name)
                if text.strip():
                    return text, label
            except Exception:
                continue

        return "[unsupported file format]", "unsupported"

    except Exception as e:
        log.warning(f"Extraction failed: {e}")
        return f"[extraction error: {e}]", "error"
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def _extract_docx(path: str) -> str:
    import docx
    doc = docx.Document(path)
    parts = []
    for p in doc.paragraphs:
        if p.text.strip():
            # Preserve heading style
            if p.style and p.style.name and p.style.name.startswith("Heading"):
                level = p.style.name.replace("Heading ", "").replace("Heading", "1")
                try:
                    parts.append(f"{'#' * int(level)} {p.text}")
                except ValueError:
                    parts.append(f"# {p.text}")
            else:
                parts.append(p.text)
    for table in doc.tables:
        rows = []
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            rows.append(" | ".join(cells))
        if rows:
            parts.append("\n".join(rows))
    text = "\n\n".join(parts)
    return text[:15000]


def _extract_xlsx(path: str) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = []
    for sheet_name in wb.sheetnames[:10]:
        ws = wb[sheet_name]
        parts.append(f"## Sheet: {sheet_name}")
        row_count = 0
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) if c is not None else "" for c in row]
            if any(cells):
                parts.append(" | ".join(cells))
            row_count += 1
            if row_count > 200:
                parts.append("[...truncated at 200 rows]")
                break
    wb.close()
    text = "\n".join(parts)
    return text[:15000]


def _extract_pptx(path: str) -> str:
    from pptx import Presentation
    prs = Presentation(path)
    parts = []
    for i, slide in enumerate(prs.slides):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = para.text.strip()
                    if t:
                        texts.append(t)
        if texts:
            parts.append(f"[Slide {i+1}]\n" + "\n".join(texts))
    text = "\n\n".join(parts)
    return text[:15000]


def _extract_pdf(path: str) -> str:
    import pypdf
    reader = pypdf.PdfReader(path)
    pages = []
    for i, page in enumerate(reader.pages[:50]):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"[Page {i+1}]\n{text}")
    text = "\n\n".join(pages)
    return text[:15000]


# ═══════════════════════════════════════════════════════════════
# OUTPUT: Create formatted files from structured text
# ═══════════════════════════════════════════════════════════════

def _timestamp_filename(name: str, ext: str) -> Path:
    """Generate a unique output filename."""
    safe_name = re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')
    if not safe_name:
        safe_name = "output"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"{safe_name}_{ts}{ext}"


def create_docx(args: dict) -> str:
    """
    Create a Word document from markdown-ish content.

    args:
      title: str — document title
      content: str — body text (supports # headings, bullet lines, --- for page breaks)
      filename: str (optional) — output filename stem
    """
    import docx
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    title = args.get("title", "Document")
    content = args.get("content", "")
    fname = args.get("filename", title)

    doc = docx.Document()

    # Title
    doc.add_heading(title, level=0)

    # Parse content line by line
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "---":
            doc.add_page_break()
        elif stripped.startswith("#### "):
            doc.add_heading(stripped[5:], level=4)
        elif stripped.startswith("### "):
            doc.add_heading(stripped[4:], level=3)
        elif stripped.startswith("## "):
            doc.add_heading(stripped[3:], level=2)
        elif stripped.startswith("# "):
            doc.add_heading(stripped[2:], level=1)
        elif stripped.startswith("- ") or stripped.startswith("* "):
            doc.add_paragraph(stripped[2:], style='List Bullet')
        elif re.match(r'^\d+\.\s', stripped):
            text = re.sub(r'^\d+\.\s', '', stripped)
            doc.add_paragraph(text, style='List Number')
        elif stripped.startswith("> "):
            p = doc.add_paragraph(stripped[2:])
            p.style = doc.styles['Intense Quote'] if 'Intense Quote' in [s.name for s in doc.styles] else doc.styles['Normal']
        else:
            doc.add_paragraph(stripped)

    out_path = _timestamp_filename(fname, ".docx")
    doc.save(str(out_path))

    # Also copy to Desktop if the model or user requested it
    # (check args for desktop_copy flag or if filename contains Desktop path)
    desktop = Path.home() / "Desktop"
    desktop_copy_path = None
    if desktop.exists():
        desktop_fname = re.sub(r'[^\w\s-]', '', fname).strip().replace(' ', '_')
        if not desktop_fname:
            desktop_fname = "output"
        desktop_copy_path = desktop / f"{desktop_fname}.docx"
        try:
            import shutil
            shutil.copy2(str(out_path), str(desktop_copy_path))
        except Exception as e:
            desktop_copy_path = None
            logging.getLogger("file_sidecar").warning(f"Could not copy to Desktop: {e}")

    # --- Validation: check how many recorded facts were incorporated ---
    validation_msg = ""
    try:
        from tools import get_recorded_facts
        facts = get_recorded_facts()
        if facts:
            content_lower = content.lower()
            used = 0
            unused_facts = []
            for f in facts:
                # Check if key words from the fact appear in the document content
                fact_words = set(f["fact"].lower().split())
                # Use significant words (>4 chars) to check presence
                sig_words = [w for w in fact_words if len(w) > 4]
                if sig_words:
                    matched = sum(1 for w in sig_words if w in content_lower)
                    if matched >= len(sig_words) * 0.3:  # At least 30% of significant words match
                        used += 1
                    else:
                        unused_facts.append(f["fact"][:80])
                else:
                    used += 1  # Short facts are hard to check, assume used
            total = len(facts)
            pct = int(used / total * 100) if total > 0 else 100
            validation_msg = f"\n\n📊 Fact validation: {used}/{total} facts incorporated ({pct}%)"
            if unused_facts:
                validation_msg += f"\n⚠️  Possibly missing facts:"
                for uf in unused_facts[:5]:
                    validation_msg += f"\n  - {uf}..."
                validation_msg += (
                    "\n\nConsider re-creating the document with these facts included "
                    "for a more complete report."
                )
            elif pct == 100:
                validation_msg += "\n✅ All recorded facts appear to be incorporated."
    except Exception as e:
        validation_msg = f"\n(Fact validation skipped: {e})"

    result_msg = f"Created: {out_path}"
    if desktop_copy_path:
        result_msg += f"\nAlso saved to Desktop: {desktop_copy_path}"
    result_msg += validation_msg
    return result_msg


def create_xlsx(args: dict) -> str:
    """
    Create an Excel workbook from structured data.

    args:
      title: str — workbook name
      sheets: list of {name: str, headers: [str], rows: [[any]]}
      filename: str (optional)
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    title = args.get("title", "Workbook")
    sheets = args.get("sheets", [])
    fname = args.get("filename", title)

    if not sheets:
        return "ERROR: No sheet data provided. Provide sheets=[{name, headers, rows}]"

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    for sheet_def in sheets:
        ws = wb.create_sheet(title=sheet_def.get("name", "Sheet"))
        headers = sheet_def.get("headers", [])
        rows = sheet_def.get("rows", [])

        # Write headers with styling
        if headers:
            for col, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=h)
                cell.font = Font(bold=True, size=11)
                cell.fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
                cell.font = Font(bold=True, color="FFFFFF", size=11)
                cell.alignment = Alignment(horizontal="center")

        # Write data rows
        start_row = 2 if headers else 1
        for r_idx, row in enumerate(rows):
            for c_idx, val in enumerate(row):
                ws.cell(row=start_row + r_idx, column=c_idx + 1, value=val)

        # Auto-width columns
        for col in ws.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            ws.column_dimensions[col_letter].width = min(max_len + 4, 50)

    out_path = _timestamp_filename(fname, ".xlsx")
    wb.save(str(out_path))
    return f"Created: {out_path}"


def create_pptx(args: dict) -> str:
    """
    Create a PowerPoint presentation from slide data.

    args:
      title: str — presentation title
      slides: list of {title: str, content: str, layout: str (optional)}
      filename: str (optional)

    layout options: "title", "content", "section", "blank"
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt

    title = args.get("title", "Presentation")
    slides = args.get("slides", [])
    fname = args.get("filename", title)

    if not slides:
        return "ERROR: No slide data. Provide slides=[{title, content}]"

    prs = Presentation()

    for slide_def in slides:
        slide_title = slide_def.get("title", "")
        content = slide_def.get("content", "")
        layout_name = slide_def.get("layout", "content")

        # Pick layout
        if layout_name == "title":
            layout = prs.slide_layouts[0]  # Title Slide
        elif layout_name == "section":
            layout = prs.slide_layouts[2]  # Section Header
        elif layout_name == "blank":
            layout = prs.slide_layouts[6]  # Blank
        else:
            layout = prs.slide_layouts[1]  # Title and Content

        slide = prs.slides.add_slide(layout)

        # Set title
        if slide.shapes.title and slide_title:
            slide.shapes.title.text = slide_title

        # Set content in the body placeholder
        if content:
            for shape in slide.placeholders:
                if shape.placeholder_format.idx == 1:  # Body
                    tf = shape.text_frame
                    tf.clear()
                    for i, line in enumerate(content.split("\n")):
                        line = line.strip()
                        if not line:
                            continue
                        if i == 0:
                            tf.paragraphs[0].text = line
                        else:
                            p = tf.add_paragraph()
                            p.text = line
                            # Indent bullet points
                            if line.startswith("- ") or line.startswith("* "):
                                p.text = line[2:]
                                p.level = 1
                    break

    out_path = _timestamp_filename(fname, ".pptx")
    prs.save(str(out_path))
    return f"Created: {out_path}"


def create_pdf(args: dict) -> str:
    """
    Create a PDF from text content using fpdf2 or reportlab.
    Falls back to docx→pdf if neither is available.

    args:
      title: str
      content: str — plain text or markdown-ish
      filename: str (optional)
    """
    title = args.get("title", "Document")
    content = args.get("content", "")
    fname = args.get("filename", title)

    # Try fpdf2 first
    try:
        from fpdf import FPDF
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, title, ln=True, align="C")
        pdf.ln(5)
        pdf.set_font("Helvetica", "", 11)
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("# "):
                pdf.set_font("Helvetica", "B", 14)
                pdf.cell(0, 8, stripped[2:], ln=True)
                pdf.set_font("Helvetica", "", 11)
            elif stripped.startswith("## "):
                pdf.set_font("Helvetica", "B", 12)
                pdf.cell(0, 7, stripped[3:], ln=True)
                pdf.set_font("Helvetica", "", 11)
            elif stripped:
                pdf.multi_cell(0, 6, stripped)
            else:
                pdf.ln(3)
        out_path = _timestamp_filename(fname, ".pdf")
        pdf.output(str(out_path))
        return f"Created: {out_path}"
    except ImportError:
        pass

    # Fallback: create a DOCX (user can export to PDF)
    result = create_docx({"title": title, "content": content, "filename": fname})
    return result + " (saved as .docx — fpdf2 not installed for direct PDF output)"


# ═══════════════════════════════════════════════════════════════
# VISION: On-demand VL model for image/video understanding
# ═══════════════════════════════════════════════════════════════

# Lazy-loaded singleton — only loaded when first image arrives
_vl_model = None
_vl_processor = None
_vl_config = None

VL_MODEL_PATH = Path.home() / "models" / "qwen2.5-vl-3b-mlx"


def _load_vl():
    """Load Qwen2.5-VL-3B on demand. ~2s cold start, 3.2GB memory."""
    global _vl_model, _vl_processor, _vl_config
    if _vl_model is not None:
        return

    import json
    from mlx_vlm import load
    log.info("Loading vision model (Qwen2.5-VL-3B)...")
    _vl_model, _vl_processor = load(str(VL_MODEL_PATH))
    with open(VL_MODEL_PATH / "config.json") as f:
        _vl_config = json.load(f)
    log.info("Vision model ready.")


def _unload_vl():
    """Free VL model memory when not needed."""
    global _vl_model, _vl_processor, _vl_config
    if _vl_model is not None:
        log.info("Unloading vision model...")
        _vl_model = None
        _vl_processor = None
        _vl_config = None
        try:
            import gc
            gc.collect()
        except Exception:
            pass


def extract_image(image_bytes: bytes, prompt: str = "Describe this image in detail.") -> str:
    """
    Run VL model on an image and return text description.
    Loads VL model on first call, keeps it cached for subsequent calls.

    Args:
        image_bytes: Raw image bytes (PNG, JPEG, etc.)
        prompt: Question/instruction about the image

    Returns:
        Text description from the VL model
    """
    from PIL import Image
    import io

    _load_vl()

    from mlx_vlm import generate
    from mlx_vlm.prompt_utils import apply_chat_template

    # Save to temp file (mlx_vlm expects file path)
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir="/tmp")
    tmp.write(image_bytes)
    tmp.close()

    try:
        formatted_prompt = apply_chat_template(
            _vl_processor, _vl_config, prompt, num_images=1
        )
        result = generate(
            _vl_model, _vl_processor,
            formatted_prompt, tmp.name,
            max_tokens=512, verbose=False
        )
        # Result is a GenerationResult namedtuple
        text = result.text if hasattr(result, 'text') else str(result)
        return text.strip()
    except Exception as e:
        log.error(f"Vision inference failed: {e}")
        return f"[vision error: {e}]"
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def extract_image_from_data_url(data_url: str, prompt: str = "Describe this image in detail.") -> str:
    """
    Extract image from a data URL and run VL model on it.
    Called from agent.py when Msty sends image attachments.
    """
    raw_b64 = data_url
    if data_url.startswith("data:"):
        if "," in data_url:
            _, raw_b64 = data_url.split(",", 1)

    try:
        image_bytes = base64.b64decode(raw_b64)
    except Exception as e:
        return f"[could not decode image: {e}]"

    return extract_image(image_bytes, prompt)


def extract_video(video_bytes: bytes, prompt: str = "Describe what happens in this video.") -> str:
    """
    Run VL model on a video and return text description.
    Qwen2.5-VL supports video understanding via frame extraction.

    Args:
        video_bytes: Raw video bytes (MP4, MOV, etc.)
        prompt: Question/instruction about the video

    Returns:
        Text description from the VL model
    """
    _load_vl()

    from mlx_vlm import generate
    from mlx_vlm.prompt_utils import apply_chat_template

    # Save to temp file
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False, dir="/tmp")
    tmp.write(video_bytes)
    tmp.close()

    try:
        formatted_prompt = apply_chat_template(
            _vl_processor, _vl_config, prompt, num_images=1
        )
        result = generate(
            _vl_model, _vl_processor,
            formatted_prompt, tmp.name,
            max_tokens=512, verbose=False
        )
        text = result.text if hasattr(result, 'text') else str(result)
        return text.strip()
    except Exception as e:
        log.error(f"Video inference failed: {e}")
        return f"[video analysis error: {e}]"
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
