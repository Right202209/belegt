# Spec: 02（数据点字典规范）；CONTRACTS.md 跨包接口 `belegt_dictionary.load_dictionary`
"""Belegt open data-point dictionary (``belegt-dictionary``).

>>> from belegt_dictionary import load_dictionary
>>> d = load_dictionary()
>>> d.get("dp:env.ghg.scope1_total").unit
'tCO2e'
"""
from .dictionary import Dictionary, DictionaryError, MappingMatch, default_root, load_dictionary
from .models import Alias, DataPoint, Locator, MappingEntry, TemplateMapping, Transform, Unit
from .periods import AlignmentDecision, EvidencePeriod, Period, align_period, align_point_in_time, validate_period
from .units import UnitError, UnitRegistry, parse_factor, to_dec_str

__version__ = "0.1.0"
DICTIONARY_RELEASE = "v2027.1"

__all__ = [
    "Alias", "AlignmentDecision", "DICTIONARY_RELEASE", "DataPoint", "Dictionary", "DictionaryError", "EvidencePeriod",
    "Locator", "MappingEntry", "MappingMatch", "Period", "TemplateMapping", "Transform", "Unit", "UnitError",
    "UnitRegistry", "align_period", "align_point_in_time", "default_root", "load_dictionary", "parse_factor",
    "to_dec_str", "validate_period",
]
