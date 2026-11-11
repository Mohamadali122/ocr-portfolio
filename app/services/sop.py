"""Literal SOP thresholds shared by flags.py and tag_rules.toml."""

BCBS_HMO_PREFIXES = ("ZGZ", "ZGN", "ZGI", "T2U", "T2G", "JEA", "VER")  # billing SOP
BARIATRIC_POUNDS = 300.0  # billing SOP
BARIATRIC_KILOGRAMS = 136.0  # billing SOP
MILEAGE_DISCREPANCY_MINIMUM = 5.0  # billing SOP
ALIAS_LAST_NAME = "doe"  # billing SOP
ALIAS_FIRST_NAMES = frozenset({"jane", "jhon", "john"})
