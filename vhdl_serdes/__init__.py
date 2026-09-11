"""Generate VHDL record serialization / deserialization functions."""

from .generator import GenOptions, generate
from .model import (Field, FieldKind, Naming, RecordDef, VhdlSerdesError, Width)
from .parser import (DEFAULT_EXTENSIONS, VhdlSource, collect_vhdl_files,
                     parse_files)

__version__ = "1.0.0"

__all__ = [
    "GenOptions", "generate", "Field", "FieldKind", "Naming", "RecordDef",
    "VhdlSerdesError", "Width", "VhdlSource", "parse_files",
    "collect_vhdl_files", "DEFAULT_EXTENSIONS", "__version__",
]
