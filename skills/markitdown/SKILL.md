---
name: markitdown
description: Use Microsoft MarkItDown to convert local files, Office documents, PDFs, images, audio, HTML, archives, URLs, and other supported inputs into Markdown for LLM-friendly analysis.
---

# Using MarkItDown

MarkItDown is a lightweight Python utility for converting files and office documents to Markdown for LLM and text-analysis pipelines. It focuses on preserving useful document structure such as headings, lists, tables, links, and extracted metadata rather than producing high-fidelity visual document conversions.

Use this skill when the user wants to:

- convert a PDF, Word, PowerPoint, Excel, HTML, CSV, JSON, XML, ZIP, EPUB, image, audio file, YouTube URL, or similar input to Markdown;
- prepare documents for LLM ingestion, summarization, search, RAG, or memory extraction;
- choose between MarkItDown CLI and Python API usage;
- decide which optional MarkItDown extras or plugins are needed;
- handle security concerns around local files, URLs, plugins, or cloud conversion services.

## Safety First

MarkItDown performs I/O with the privileges of the current process. Treat it like `open()` or `requests.get()`.

Rules:

- Do not pass untrusted paths or URLs directly to MarkItDown.
- Validate and normalize local paths before converting.
- Restrict URI schemes and network destinations for untrusted URLs.
- Block private, loopback, link-local, and metadata-service addresses in hosted/server contexts.
- Prefer the narrowest conversion API that fits the job.
- Use `convert_local()` when only local files should be read.
- Use `convert_stream()` when the caller already controls the input stream.
- Fetch URLs yourself and pass a controlled response to `convert_response()` when network access needs policy checks.
- Enable plugins only when the user explicitly needs them and trusts the installed plugin.
- Treat Azure Document Intelligence and Azure Content Understanding as billable cloud calls.

## Supported Inputs

MarkItDown can convert many input types, including:

- PDF
- PowerPoint
- Word
- Excel
- images, including EXIF metadata and OCR-capable paths when configured
- audio, including EXIF metadata and speech transcription-capable paths when configured
- HTML
- text-based formats such as CSV, JSON, and XML
- ZIP files, iterating over contents
- YouTube URLs
- EPUB
- other formats supported by installed converters or plugins

## Installation

The installed system may already have MarkItDown. When installation is needed:

```powershell
python -m pip install "markitdown[all]"
```

For narrower installs, use optional extras:

```powershell
python -m pip install "markitdown[pdf,docx,pptx]"
```

Common optional extras:

- `all`
- `pptx`
- `docx`
- `xlsx`
- `xls`
- `pdf`
- `outlook`
- `az-doc-intel`
- `az-content-understanding`
- `audio-transcription`
- `youtube-transcription`

MarkItDown currently supports Python 3.10 through 3.14. Prefer a virtual environment.

## Command Line

Convert a file to stdout:

```powershell
markitdown path-to-file.pdf
```

Write to a Markdown file:

```powershell
markitdown path-to-file.pdf -o document.md
```

Pipe content:

```powershell
Get-Content path-to-file.pdf -Raw | markitdown
```

On Windows, prefer explicit paths and quoted filenames:

```powershell
markitdown ".\input files\report.pdf" -o ".\output\report.md"
```

## Python API

Basic usage:

```python
from markitdown import MarkItDown

md = MarkItDown(enable_plugins=False)
result = md.convert("test.xlsx")
print(result.markdown)
```

Prefer narrower APIs where possible:

```python
from markitdown import MarkItDown

md = MarkItDown(enable_plugins=False)

# Prefer for local-only file conversion when available in the installed version.
result = md.convert_local("report.pdf")
print(result.markdown)
```

For controlled stream conversion:

```python
from markitdown import MarkItDown

md = MarkItDown(enable_plugins=False)

with open("report.pdf", "rb") as stream:
    result = md.convert_stream(stream)

print(result.markdown)
```

## Choosing CLI vs Python API

Use the CLI when:

- the user asks for a one-off conversion;
- input and output are local files;
- shell redirection or `-o` is enough;
- no custom security, filtering, or client configuration is needed.

Use the Python API when:

- conversion is part of application code;
- paths or URLs need validation;
- the caller needs streams, responses, or custom fetching;
- plugins, LLM clients, Azure integrations, or retry behavior must be configured;
- results need post-processing before writing.

## Plugins

Plugins are disabled by default.

List installed plugins:

```powershell
markitdown --list-plugins
```

Enable plugins for a CLI conversion:

```powershell
markitdown --use-plugins path-to-file.pdf
```

Use plugins carefully:

- Verify the plugin source before enabling it.
- Confirm whether the plugin calls external services.
- Document any API keys, network access, or billing implications.
- Keep plugin-specific behavior out of generic MarkItDown assumptions.

## OCR and LLM-Assisted Image Descriptions

MarkItDown can use LLM clients for image descriptions in supported paths such as image and PowerPoint conversion. Configure the client explicitly:

```python
from markitdown import MarkItDown
from openai import OpenAI

client = OpenAI(max_retries=5)
md = MarkItDown(
    llm_client=client,
    llm_model="gpt-4o",
    llm_prompt="Describe the image content for Markdown extraction.",
)

result = md.convert("example.jpg")
print(result.markdown)
```

Use LLM-assisted conversion only when the user expects possible remote model calls. Be explicit about cost, privacy, and retry behavior.

## Azure Integrations

### Azure Document Intelligence

CLI:

```powershell
markitdown path-to-file.pdf -o document.md -d -e "<document_intelligence_endpoint>"
```

Or set the endpoint once:

```powershell
$env:MARKITDOWN_DOCINTEL_ENDPOINT = "<document_intelligence_endpoint>"
markitdown path-to-file.pdf -o document.md -d
```

Python:

```python
from markitdown import MarkItDown

md = MarkItDown(docintel_endpoint="<document_intelligence_endpoint>")
result = md.convert("test.pdf")
print(result.markdown)
```

### Azure Content Understanding

Use this when higher-quality multimodal conversion, audio/video support, or structured field extraction is needed.

CLI:

```powershell
markitdown path-to-file.pdf --use-cu --cu-endpoint "<content_understanding_endpoint>"
```

Python:

```python
from markitdown import MarkItDown

md = MarkItDown(cu_endpoint="<content_understanding_endpoint>")
result = md.convert("report.pdf")
print(result.markdown)
```

With a custom analyzer:

```python
from markitdown import MarkItDown

md = MarkItDown(
    cu_endpoint="<content_understanding_endpoint>",
    cu_analyzer_id="my-invoice-analyzer",
)

result = md.convert("invoice.pdf")
print(result.markdown)
```

Remember that Content Understanding conversions are billable cloud API calls. Restrict routed file types when needed.

## Output Handling

When converting for this bot or other LLM workflows:

- Save output as `.md`.
- Preserve the source path or URL in metadata outside the Markdown if traceability matters.
- Inspect the beginning and end of generated Markdown after conversion.
- For ZIP files, review how contents were ordered and separated.
- For large documents, chunk after conversion rather than before conversion when structure matters.
- For scanned or image-heavy documents, expect lower quality unless OCR or cloud analysis is enabled.

## Common Workflows

### Convert a local Office document

```powershell
markitdown ".\proposal.docx" -o ".\proposal.md"
```

### Convert a PDF for LLM ingestion

```powershell
markitdown ".\report.pdf" -o ".\report.md"
```

Then inspect:

```powershell
Get-Content ".\report.md" -TotalCount 80
```

### Convert in Python and post-process

```python
from pathlib import Path
from markitdown import MarkItDown

source = Path("report.pdf")
target = source.with_suffix(".md")

md = MarkItDown(enable_plugins=False)
result = md.convert_local(str(source))
target.write_text(result.markdown, encoding="utf-8")
```

### Convert a controlled URL

Prefer validating and fetching the URL yourself, then pass the response into MarkItDown if supported by the installed version.

```python
import requests
from markitdown import MarkItDown

url = "https://example.com/report.html"
response = requests.get(url, timeout=20)
response.raise_for_status()

md = MarkItDown(enable_plugins=False)
result = md.convert_response(response)
print(result.markdown)
```

## Troubleshooting

| Symptom | Likely Cause | Fix |
| --- | --- | --- |
| Converter cannot read a format | Optional extra not installed | Install the relevant extra such as `markitdown[pdf]` or `markitdown[docx]` |
| Output misses scanned text | No OCR path configured | Use OCR plugin, LLM-assisted image handling, Document Intelligence, or Content Understanding |
| Tables look imperfect | Markdown is structural, not layout-perfect | Inspect output and post-process tables if downstream code depends on exact shape |
| Images only show metadata | Image description client not configured | Provide `llm_client` and `llm_model` when appropriate |
| Remote URL conversion is unsafe | `convert()` can access URIs | Validate/fetch URL yourself and use a narrower conversion method |
| Plugin behavior is surprising | Plugins are third-party code | Disable plugins or inspect plugin documentation |
| Conversion is expensive | Cloud integrations or LLM calls are enabled | Restrict file types and disable cloud/LLM paths unless needed |

## Decision Checklist

- [ ] Is the input trusted?
- [ ] Is the path or URL validated?
- [ ] Is the narrowest conversion API being used?
- [ ] Are optional dependencies installed for the source format?
- [ ] Are plugins intentionally enabled or disabled?
- [ ] Are cloud, OCR, or LLM-assisted features expected by the user?
- [ ] Is billing/cost acceptable for cloud conversion?
- [ ] Is the Markdown output saved with UTF-8 encoding?
- [ ] Has the output been spot-checked before downstream ingestion?

## Sources

- https://github.com/microsoft/markitdown
- https://raw.githubusercontent.com/microsoft/markitdown/main/README.md
