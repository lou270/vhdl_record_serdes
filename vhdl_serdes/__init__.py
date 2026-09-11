"""Generate VHDL record serialization / deserialization functions."""

from .generator import GenOptions, generate
from .model import (Field, FieldKind, Naming, RecordDef, VhdlSerdesError, Width)
from .parser import VhdlSource, parse_files

__version__ = "1.0.0"

__all__ = [
    "GenOptions", "generate", "Field", "FieldKind", "Naming", "RecordDef",
    "VhdlSerdesError", "Width", "VhdlSource", "parse_files", "__version__",
]
