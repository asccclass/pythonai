"""Parse attachments in a cancellable native worker, never as truncated Base64."""
import json
from pathlib import Path
import sys
import tempfile
from ..process import run_process

SUPPORTED={'.pdf','.docx','.pptx','.xlsx'}


def parse_document(data,filename):
    suffix=Path(filename).suffix.lower()
    if suffix not in SUPPORTED:raise ValueError('Supported documents: PDF/DOCX/PPTX/XLSX')
    if not isinstance(data,bytes) or not 0<len(data)<=10*1024*1024:raise ValueError('Document size must be 1 byte..10 MiB')
    with tempfile.TemporaryDirectory(prefix='pythonai-document-') as directory:
        source=Path(directory)/('input'+suffix);result=Path(directory)/'result.json';source.write_bytes(data)
        completed=run_process([sys.executable,str(Path(__file__).with_name('document_worker.py')),str(source),str(result)],timeout=45,max_output_bytes=65536)
        if completed.returncode or not result.is_file() or result.stat().st_size>1024*1024:raise RuntimeError('Document worker stopped: '+str(completed.returncode))
        return json.loads(result.read_text(encoding='utf-8'))
