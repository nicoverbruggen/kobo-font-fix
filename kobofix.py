#!/usr/bin/env python3
"""
Font processing utility for Kobo e-readers.

Processes TrueType fonts to improve compatibility with Kobo e-readers:
- Converting OTF (CFF) inputs to TTF (glyf) first, so all downstream steps
  operate on a true TrueType font with no CFF metadata left behind
- Renaming fonts with a configurable prefix and updating internal metadata
  (name table, CFF, post table, PS name)
- Validating and correcting PANOSE metadata based on font style
- Updating font weight metadata (OS/2 usWeightClass)
- Adjusting line spacing via font-line
- Optional glyph and advance scaling for fonts that render optically small
- Simplifying outlines w/ skia-pathops
- Kerning: extracting GPOS pairs (Format 1, Format 2, and Extension lookups)
  into a legacy kern table, prioritized by Unicode range to fit within
  format 0 size constraints
- Flattening TrueType composite glyphs to avoid Kobo renderer artifacts
- Adding glyphs for common Unicode space characters the font is missing (thin
  space, narrow no-break space, en/em spaces, zero-width spaces, etc.) so Kobo
  does not render a .notdef box where one is used (always applied)
- Adding hyphen/dash characters the font is missing (soft hyphen, non-breaking
  hyphen, horizontal bar) by cloning an existing glyph's shape and width, and a
  figure dash by re-spacing the en dash to the digit width, so they render
  correctly instead of as a .notdef box (always applied)
- Hinting: automatically re-running ttfautohint after outline rewriting for
  non-KF outputs when the source font had meaningful glyph-level TrueType hints
- Wobble fix (KF preset): stripping hinting and giving every outline glyph the
  same no-op TrueType instruction, so Kobo's iType rasterizer runs its
  interpreter instead of its auto-grid-fit (which snaps the same letter to
  different pixel heights, a vertical "wobble"). The outline is left unchanged

Supports --dry-run to preview what would change without modifying files.
Includes NV and KF presets for common workflows, or can be fully
configured via individual flags. Run with -h for usage details.

Requirements:
- fontTools (pip install fonttools)
- FontForge (required for OTF/CFF inputs)
- font-line (pip install font-line)
- skia-pathops (pip install skia-pathops)
- ttfautohint
- ots-sanitize
"""

import sys
import os
import re
import copy
import shutil
import subprocess
import argparse
import logging
import string
import tempfile
from datetime import date
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Tuple, Optional, List

from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import ttProgram
from fontTools.ttLib.tables._k_e_r_n import KernTable_format_0
from fontTools.ttLib.tables._g_l_y_f import Glyph

# -------------
# PRESETS
# -------------
#
PRESETS = {
    "nv": {
        "prefix": "NV",
        "line_percent": 20,
        "kern": "skip",
        "outline": "skip",
    },
    "kf": {
        "prefix": "KF",
        "line_percent": 0,
        "kern": "add-legacy-kern",
        "outline": "apply",
        "remove_prefix": "NV",
    },
}

# Known prefixes are automatically detected and stripped before applying
# the preset's prefix. This ensures idempotent processing.
KNOWN_PREFIXES = sorted(
    {p["prefix"] for p in PRESETS.values() if "prefix" in p},
    key=len, reverse=True  # longest first to avoid partial matches
)

# -------------
# STYLE MAPPING
# -------------
# Style mapping for filenames and internal font data.
# If a particular style string is found in the font file name, it can be mapped.
# The values are a tuple of (human-readable_style_name, usWeightClass).
#
STYLE_MAP = {
    "BoldItalic": ("Bold Italic", 700),
    "Bold": ("Bold", 700),
    "Italic": ("Italic", 400),
    "Regular": ("Regular", 400),
}


# Configure logging for clear output
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

ASCII_PRIORITY_CODEPOINTS = {
    ord(ch) for ch in (string.ascii_letters + string.digits + string.punctuation)
}
TYPOGRAPHIC_PRIORITY_CODEPOINTS = {
    0x00AB,  # LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
    0x00BB,  # RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK
    0x2013,  # EN DASH
    0x2014,  # EM DASH
    0x2018,  # LEFT SINGLE QUOTATION MARK
    0x2019,  # RIGHT SINGLE QUOTATION MARK
    0x201A,  # SINGLE LOW-9 QUOTATION MARK
    0x201C,  # LEFT DOUBLE QUOTATION MARK
    0x201D,  # RIGHT DOUBLE QUOTATION MARK
    0x201E,  # DOUBLE LOW-9 QUOTATION MARK
    0x2026,  # HORIZONTAL ELLIPSIS
    0x2039,  # SINGLE LEFT-POINTING ANGLE QUOTATION MARK
    0x203A,  # SINGLE RIGHT-POINTING ANGLE QUOTATION MARK
}


# Common Unicode space characters that are frequently absent from fonts. When
# one is missing, a Kobo renders a .notdef box in its place, so we add an
# (invisible) glyph for it. Each entry is (codepoint, glyph name, width spec);
# the width spec is resolved by _resolve_space_width():
#   ("em", frac)  -> round(unitsPerEm * frac): the canonical Unicode advance
#   ("space", f)  -> f * the font's own U+0020 width (so the thin/narrow spaces
#                    harmonise with the typeface); falls back to 1/5 em
#   ("digit", _)  -> width of a digit glyph (figure space aligns with numerals)
#   ("period", _) -> width of the period (punctuation space)
#   ("zero", _)   -> 0, for zero-width format characters
SPACE_GLYPHS = [
    (0x2000, "uni2000", ("em", 1 / 2)),   # EN QUAD
    (0x2001, "uni2001", ("em", 1)),       # EM QUAD
    (0x2002, "uni2002", ("em", 1 / 2)),   # EN SPACE
    (0x2003, "uni2003", ("em", 1)),       # EM SPACE
    (0x2004, "uni2004", ("em", 1 / 3)),   # THREE-PER-EM SPACE
    (0x2005, "uni2005", ("em", 1 / 4)),   # FOUR-PER-EM SPACE
    (0x2006, "uni2006", ("em", 1 / 6)),   # SIX-PER-EM SPACE
    (0x2007, "uni2007", ("digit", None)), # FIGURE SPACE
    (0x2008, "uni2008", ("period", None)),# PUNCTUATION SPACE
    (0x2009, "uni2009", ("space", 1 / 2)),# THIN SPACE
    (0x200A, "uni200A", ("em", 1 / 10)),  # HAIR SPACE
    (0x202F, "uni202F", ("space", 1 / 2)),# NARROW NO-BREAK SPACE
    (0x205F, "uni205F", ("em", 4 / 18)),  # MEDIUM MATHEMATICAL SPACE
    (0x3000, "uni3000", ("em", 1)),       # IDEOGRAPHIC SPACE (full-width)
    (0x200B, "uni200B", ("zero", None)),  # ZERO WIDTH SPACE
    (0x200C, "uni200C", ("zero", None)),  # ZERO WIDTH NON-JOINER
    (0x200D, "uni200D", ("zero", None)),  # ZERO WIDTH JOINER
    (0x200E, "uni200E", ("zero", None)),  # LEFT-TO-RIGHT MARK
    (0x200F, "uni200F", ("zero", None)),  # RIGHT-TO-LEFT MARK
    (0x2060, "uni2060", ("zero", None)),  # WORD JOINER
    (0xFEFF, "uniFEFF", ("zero", None)),  # ZERO WIDTH NO-BREAK SPACE (BOM)
]
# Characters that must render a *visible* shape identical to a glyph the font
# already has, so they can't be blanked like a space. Instead we clone the
# existing glyph's contours and advance width. The classic case is the soft
# hyphen: when it surfaces at a line break it should look exactly like the
# regular hyphen (the "only visible when wrapped" behaviour is the layout
# engine's job, not the font's). Each entry is (codepoint, glyph name, source
# codepoints in preference order).
CLONE_GLYPHS = [
    (0x00AD, "uni00AD", (0x002D,)),         # SOFT HYPHEN         <- hyphen
    (0x2010, "uni2010", (0x002D,)),         # HYPHEN              <- hyphen
    (0x2011, "uni2011", (0x002D, 0x2010)),  # NON-BREAKING HYPHEN <- hyphen
    (0x2015, "uni2015", (0x2014,)),         # HORIZONTAL BAR      <- em dash
]

CLONE_GLYPH_NAMES = {
    0x00AD: "soft hyphen", 0x2010: "hyphen",
    0x2011: "non-breaking hyphen", 0x2015: "horizontal bar",
}

# FIGURE DASH (U+2012) is a dash whose defining property is a digit-width
# advance, so it is not a plain clone: we take the en dash's bar shape and
# re-space it to the figure width, centred (see add_missing_figure_dash).
FIGURE_DASH_CODEPOINT = 0x2012
FIGURE_DASH_GLYPH_NAME = "uni2012"
FIGURE_DASH_SOURCE_CODEPOINTS = (0x2013, 0x002D)  # en dash preferred, else hyphen

# Still deliberately NOT synthesised: MINUS SIGN (U+2212) is a distinct, wider
# glyph aligned with the plus sign; and LINE/PARAGRAPH SEPARATOR (U+2028/U+2029)
# are line breaks, not printable glyphs.

# Human-readable names for logging / dry-run output.
SPACE_GLYPH_NAMES = {
    0x2000: "en quad", 0x2001: "em quad", 0x2002: "en space",
    0x2003: "em space", 0x2004: "three-per-em space",
    0x2005: "four-per-em space", 0x2006: "six-per-em space",
    0x2007: "figure space", 0x2008: "punctuation space", 0x2009: "thin space",
    0x200A: "hair space", 0x202F: "narrow no-break space",
    0x205F: "medium mathematical space", 0x3000: "ideographic space",
    0x200B: "zero width space", 0x200C: "zero width non-joiner",
    0x200D: "zero width joiner", 0x200E: "left-to-right mark",
    0x200F: "right-to-left mark", 0x2060: "word joiner",
    0xFEFF: "zero width no-break space",
}


STAMP_URL = "https://github.com/nicoverbruggen/kobo-font-fix"
STAMP_RE = re.compile(
    r"\s*Patched with `kobo-font-fix` \([^)]*\) for improved compatibility with Kobo devices on \d{4}-\d{2}-\d{2}\.",
)


def _build_stamped_copyright(current: str) -> str:
    """Return copyright text with a fresh kobo-font-fix stamp appended.

    Any prior stamp (same tool, any date/url) is removed first so repeated
    runs don't accumulate lines.
    """
    cleaned = STAMP_RE.sub("", current or "").rstrip()
    today = date.today().isoformat()
    suffix = f"Patched with `kobo-font-fix` ({STAMP_URL}) for improved compatibility with Kobo devices on {today}."
    return f"{cleaned}\n\n{suffix}" if cleaned else suffix


@dataclass
class FontMetadata:
    """
    A simple data class to hold consistent font naming and metadata.
    """
    family_name: str
    style_name: str
    full_name: str
    ps_name: str


class FontProcessor:
    """
    Main font processing class.
    """
    
    def __init__(self,
        prefix: str,
        line_percent: int,
    ):
        """
        Initialize the font processor with configurable values.
        If the user has not supplied custom arguments, the default values are used.
        
        Args:
            prefix: Prefix to add to font names
            line_percent: Percentage for baseline adjustment
        """
        self.prefix = prefix
        self.line_percent = line_percent

    @staticmethod
    def _find_available_ots() -> Optional[Path]:
        """Return a usable ots-sanitize path if already installed or cached."""
        import shutil as sh

        binary = "ots-sanitize.exe" if os.name == "nt" else "ots-sanitize"
        system_binary = sh.which(binary)
        if system_binary:
            return Path(system_binary)

        tools_dir = Path(__file__).resolve().parent / ".tools"
        if not tools_dir.exists():
            return None

        for path in tools_dir.rglob(binary):
            if path.is_file():
                return path
        return None

    @staticmethod
    def _validate_font(ots: Path, font_path: Path) -> Tuple[bool, str]:
        """Run ots-sanitize against a font. Returns (ok, combined_output)."""
        import subprocess as sp

        result = sp.run(
            [str(ots), str(font_path), os.devnull],
            capture_output=True,
            text=True,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output

    @staticmethod
    def _validate_output_font(output_path: str) -> bool:
        """Validate the final output font with ots-sanitize."""
        ots = FontProcessor._find_available_ots()
        if not ots:
            logger.error("  ots-sanitize is required to validate processed fonts.")
            return False

        ok, output = FontProcessor._validate_font(ots, Path(output_path))
        status = "OK" if ok else "FAIL"
        logger.info(f"  ots-sanitize: {status}")
        if output:
            for line in output.splitlines():
                logger.info(f"    {line}")
        return ok
    
    # ============================================================
    # Helper methods
    # ============================================================
    
    @staticmethod
    def _get_style_from_filename(filename: str) -> Tuple[str, int]:
        """
        Determine font style and weight from filename.
        This function centralizes a critical piece of logic that is used in
        multiple places to ensure consistency across the script.
        
        Args:
            filename: The font file name.
            
        Returns:
            A tuple of (style_name, usWeightClass).
        """
        base_filename = os.path.basename(filename)
        for key, (style_name, weight) in STYLE_MAP.items():
            if key.lower() in base_filename.lower():
                return style_name, weight
        return "Regular", 400  # Default if no style found
    
    @staticmethod
    def _set_name_records(font: TTFont, name_id: int, new_name: str) -> None:
        """
        Update a font's name table record for all relevant platforms,
        encodings, and languages.

        This method has been updated to iterate through all existing records
        for the given name_id and update them, ensuring consistent naming
        across different platforms like Windows and Macintosh.
        """
        name_table = font["name"]
        
        # Find all existing records for the given nameID
        names_to_update = [
            n for n in name_table.names if n.nameID == name_id
        ]
        
        # If no records exist, add new ones for standard platforms.
        if not names_to_update:
            logger.debug(f"  Name ID {name_id} not found; adding new records.")
            try:
                name_table.setName(new_name, name_id, 3, 1, 0x0409)  # Windows record
                name_table.setName(new_name, name_id, 1, 0, 0) # Macintosh record
                logger.debug(f"  Name ID {name_id} added as '{new_name}'.")
            except Exception as e:
                logger.warning(f"  Failed to add new name ID {name_id}: {e}")
            return

        updated_count = 0
        for name_record in names_to_update:
            try:
                # Determine the appropriate encoding for the platform
                if name_record.platformID == 1:  # Macintosh
                    encoded_name = new_name.encode('mac-roman', 'ignore')
                    if name_record.string != encoded_name:
                        name_record.string = encoded_name
                        updated_count += 1
                elif name_record.platformID in (0, 3):  # Unicode and Windows
                    encoded_name = new_name.encode('utf-16-be', 'ignore')
                    if name_record.string != encoded_name:
                        name_record.string = encoded_name
                        updated_count += 1
                else:
                    # Fallback for other platforms to ensure consistency
                    encoded_name = new_name.encode('utf-8', 'ignore')
                    if name_record.string != encoded_name:
                        name_record.string = encoded_name
                        updated_count += 1
                        
            except Exception as e:
                logger.warning(f"  Failed to update record for Name ID {name_id} "
                              f"(Platform {name_record.platformID}, "
                              f"Encoding {name_record.platEncID}): {e}")

        if updated_count > 0:
            logger.debug(f"  Name ID {name_id} updated for {updated_count} record(s).")
        else:
            logger.debug(f"  Name ID {name_id} is already correct across all records.")
            
    # ============================================================
    # Metadata extraction
    # ============================================================
    
    def _get_font_metadata(
        self, 
        font: TTFont, 
        font_path: str, 
        new_family_name: Optional[str]
    ) -> Optional[FontMetadata]:
        """
        Extract or infer font metadata from the font and arguments.
        This function acts as a single point of truth for font metadata,
        ensuring consistency throughout the processing pipeline.
        """
        if "name" in font:
            # Determine family name from user input or best available name from font.
            family_name = new_family_name if new_family_name else font["name"].getBestFamilyName()
        else:
            family_name = new_family_name
        
        if not family_name:
            logger.warning("  Could not determine font family name.")
            return None
        
        # Centralized logic: Determine style name from filename.
        style_name, _ = self._get_style_from_filename(font_path)
        
        # Construct the full name and PS name based on style name logic
        full_name = f"{family_name}"
        if style_name != "Regular":
            full_name += f" {style_name}"

        # If prefix is empty, don't add it to the PS name
        if self.prefix:
            ps_name = f"{self.prefix}_{family_name.replace(' ', '-')}"
        else:
            ps_name = family_name.replace(' ', '-')

        if style_name != "Regular":
            ps_name += f"-{style_name.replace(' ', '')}"
        
        logger.debug(f"  Constructed metadata: family='{family_name}', style='{style_name}', full='{full_name}', ps='{ps_name}'")
        
        return FontMetadata(
            family_name=family_name,
            style_name=style_name,
            full_name=full_name,
            ps_name=ps_name
        )
        
    # ============================================================
    # Kerning extraction methods
    # ============================================================
    
    @staticmethod
    def _pair_value_to_kern(value1, value2) -> int:
        """
        Compute a legacy kerning value from GPOS PairValue records.

        This logic is specific to converting GPOS (OpenType) kerning to
        the older 'kern' (TrueType) table format.

        Note: Only XAdvance values are used, as they directly map to kern table semantics
        (adjusting inter-character spacing). XPlacement values shift glyphs without
        affecting spacing and cannot be represented in the legacy kern table. To avoid
        potential issues, XPlacement values are now being ignored.
        """
        kern_value = 0
        if value1 is not None:
            kern_value += getattr(value1, "XAdvance", 0) or 0
        if value2 is not None:
            kern_value += getattr(value2, "XAdvance", 0) or 0

        return int(kern_value)
    
    def _extract_format1_pairs(self, subtable) -> Dict[Tuple[str, str], int]:
        """Extract kerning pairs from PairPos Format 1 (per-glyph PairSets)."""
        pairs = {}
        coverage = getattr(subtable, "Coverage", None)
        pair_sets = getattr(subtable, "PairSet", [])

        if not coverage or not hasattr(coverage, "glyphs"):
            return pairs

        for idx, left_glyph in enumerate(coverage.glyphs):
            if idx >= len(pair_sets):
                break

            for record in getattr(pair_sets[idx], "PairValueRecord", []):
                right_glyph = record.SecondGlyph
                kern_value = self._pair_value_to_kern(record.Value1, record.Value2)
                if kern_value:
                    # Only set if not already present (first value wins)
                    key = (left_glyph, right_glyph)
                    if key not in pairs:
                        pairs[key] = kern_value
        return pairs
    
    def _extract_format2_pairs(self, font: TTFont, subtable) -> Dict[Tuple[str, str], int]:
        """Extract kerning pairs from PairPos Format 2 (class-based)."""
        pairs = {}
        coverage = getattr(subtable, "Coverage", None)
        class_def1 = getattr(subtable, "ClassDef1", None)
        class_def2 = getattr(subtable, "ClassDef2", None)
        class1_records = getattr(subtable, "Class1Record", [])

        if not coverage or not hasattr(coverage, "glyphs"):
            return pairs

        class1_map = getattr(class_def1, "classDefs", {}) if class_def1 else {}
        left_by_class = defaultdict(list)
        for glyph in coverage.glyphs:
            class_idx = class1_map.get(glyph, 0)
            left_by_class[class_idx].append(glyph)

        class2_map = getattr(class_def2, "classDefs", {}) if class_def2 else {}
        right_by_class = defaultdict(list)
        for glyph, class_idx in class2_map.items():
            right_by_class[class_idx].append(glyph)
        for glyph in font.getGlyphOrder():
            if glyph not in class2_map:
                right_by_class[0].append(glyph)

        for class1_idx, class1_record in enumerate(class1_records):
            left_glyphs = left_by_class.get(class1_idx, [])
            if not left_glyphs:
                continue

            for class2_idx, class2_record in enumerate(class1_record.Class2Record):
                right_glyphs = right_by_class.get(class2_idx, [])
                if not right_glyphs:
                    continue

                kern_value = self._pair_value_to_kern(class2_record.Value1, class2_record.Value2)
                if not kern_value:
                    continue

                for left in left_glyphs:
                    for right in right_glyphs:
                        # Only set if not already present (first value wins)
                        key = (left, right)
                        if key not in pairs:
                            pairs[key] = kern_value
        return pairs
    
    def extract_kern_pairs(self, font: TTFont) -> Dict[Tuple[str, str], int]:
        """
        Extract kerning pairs from the font.
        Prioritizes existing 'kern' table over GPOS data if present.
        GPOS (Glyph Positioning) is the modern standard for kerning in OpenType fonts.
        """
        pairs = {}

        # If a kern table already exists, use it instead of GPOS
        if "kern" in font:
            kern_table = font["kern"]
            for subtable in getattr(kern_table, "kernTables", []):
                if hasattr(subtable, "kernTable"):
                    pairs.update(subtable.kernTable)
            return pairs

        # Otherwise, extract from GPOS
        if "GPOS" in font:
            gpos = font["GPOS"].table
            lookup_list = getattr(gpos, "LookupList", None)
            if lookup_list and lookup_list.Lookup:
                for lookup in lookup_list.Lookup:
                    lookup_type = getattr(lookup, "LookupType", None)
                    subtables = getattr(lookup, "SubTable", [])

                    # Unwrap Extension lookups (type 9) to get the inner subtables
                    if lookup_type == 9:
                        unwrapped = []
                        for ext_subtable in subtables:
                            ext_type = getattr(ext_subtable, "ExtensionLookupType", None)
                            inner = getattr(ext_subtable, "ExtSubTable", None)
                            if ext_type == 2 and inner is not None:
                                unwrapped.append(inner)
                        subtables = unwrapped
                        lookup_type = 2 if unwrapped else None

                    if lookup_type != 2:
                        continue

                    for subtable in subtables:
                        fmt = getattr(subtable, "Format", None)
                        if fmt == 1:
                            extracted = self._extract_format1_pairs(subtable)
                        elif fmt == 2:
                            extracted = self._extract_format2_pairs(font, subtable)
                        else:
                            continue
                        for key, value in extracted.items():
                            if key not in pairs:
                                pairs[key] = value
        return pairs
    
    @staticmethod
    def _glyph_priority(glyph_name: str, cmap_reverse: Dict[str, int]) -> int:
        """
        Assign a priority to a glyph for kern pair sorting.
        Lower values = higher priority. Pairs involving common glyphs
        are prioritized so they fit within the subtable size limit.
        """
        cp = cmap_reverse.get(glyph_name)
        if cp is None:
            return 5  # unmapped glyphs (ligatures, alternates, etc.)
        if cp in ASCII_PRIORITY_CODEPOINTS:
            return 0  # ASCII letters, digits, and punctuation
        if cp in TYPOGRAPHIC_PRIORITY_CODEPOINTS:
            return 1  # smart quotes, dashes, ellipsis, guillemets
        if cp <= 0x007F:
            return 2  # other Basic Latin codepoints
        if cp <= 0x00FF:
            return 3  # Latin-1 Supplement (accented chars, common symbols)
        if cp <= 0x024F:
            return 4  # Latin Extended-A and B
        return 5      # everything else

    @staticmethod
    def add_legacy_kern(font: TTFont, kern_pairs: Dict[Tuple[str, str], int]) -> int:
        """
        Create or replace a legacy 'kern' table with the supplied pairs.

        The legacy kern table format has strict size constraints:
        - Most renderers (including Kobo's WebKit-based engine) only read the
          first subtable, so we write exactly one.
        - Format 0 subtables have a uint16 length field (max 65,535 bytes).
          With a 14-byte header and 6 bytes per pair, this allows at most
          (65,535 - 14) / 6 = 10,920 pairs before the length overflows.

        When a font has more pairs than this (common with class-based GPOS
        kerning, which can expand to 100k+ individual pairs), we prioritize
        by Unicode range so the most commonly encountered pairs are kept:
          - ASCII letters, digits, and punctuation
          - Common typography punctuation (smart quotes, dashes, ellipsis, guillemets)
          - Other Basic Latin codepoints (for example control-space block)
          - Latin-1 Supplement (accented chars, common symbols)
          - Latin Extended-A/B (Central/Eastern European chars)
          - Everything else and unmapped glyphs (ligatures, alternates)

        This means all English kerning is preserved, most Western European
        kerning (French, German, Spanish, etc.) is preserved, and only less
        common extended Latin pairings are dropped when truncation is needed.
        """
        if not kern_pairs:
            return 0

        MAX_PAIRS = 10920
        items = [(tuple(k), int(v)) for k, v in kern_pairs.items() if v]

        if len(items) > MAX_PAIRS:
            # Build reverse cmap (glyph name -> codepoint) for prioritization
            cmap_reverse = {}
            if "cmap" in font:
                for table in font["cmap"].tables:
                    if hasattr(table, "cmap"):
                        for cp, glyph_name in table.cmap.items():
                            if glyph_name not in cmap_reverse:
                                cmap_reverse[glyph_name] = cp

            # Sort by priority of both glyphs (lower = more common)
            items.sort(key=lambda pair: (
                FontProcessor._glyph_priority(pair[0][0], cmap_reverse) +
                FontProcessor._glyph_priority(pair[0][1], cmap_reverse)
            ))

            logger.warning(f"  Kerning: {len(items)} pairs exceed the subtable limit of {MAX_PAIRS}. "
                           f"Keeping the {MAX_PAIRS} most common pairs.")
            items = items[:MAX_PAIRS]

        kern_table = newTable("kern")
        kern_table.version = 0
        kern_table.kernTables = []

        subtable = KernTable_format_0()
        subtable.version = 0
        subtable.length = None
        subtable.coverage = 1
        subtable.kernTable = dict(items)
        kern_table.kernTables.append(subtable)

        # Additional subtables are not created because most renderers
        # (including Kobo's WebKit-based engine) only read the first one.

        font["kern"] = kern_table

        return len(items)

    @staticmethod
    def _scale_value_record(record, scale: float) -> int:
        """Scale numeric GPOS ValueRecord positioning fields in place."""
        if record is None:
            return 0

        changed = 0
        for field in ("XPlacement", "YPlacement", "XAdvance", "YAdvance"):
            value = getattr(record, field, None)
            if value:
                setattr(record, field, int(round(value * scale)))
                changed += 1
        return changed

    @classmethod
    def _scale_gpos_pairpos(cls, font: TTFont, scale: float) -> int:
        """Scale PairPos GPOS value records so kerning tracks scaled advances."""
        if "GPOS" not in font:
            return 0

        changed = 0
        lookup_list = getattr(font["GPOS"].table, "LookupList", None)
        if lookup_list is None:
            return 0

        for lookup in getattr(lookup_list, "Lookup", []):
            lookup_type = getattr(lookup, "LookupType", None)
            subtables = getattr(lookup, "SubTable", [])

            if lookup_type == 9:
                unwrapped = []
                for ext_subtable in subtables:
                    ext_type = getattr(ext_subtable, "ExtensionLookupType", None)
                    inner = getattr(ext_subtable, "ExtSubTable", None)
                    if ext_type == 2 and inner is not None:
                        unwrapped.append(inner)
                subtables = unwrapped
                lookup_type = 2 if unwrapped else None

            if lookup_type != 2:
                continue

            for subtable in subtables:
                fmt = getattr(subtable, "Format", None)
                if fmt == 1:
                    for pair_set in getattr(subtable, "PairSet", []):
                        for record in getattr(pair_set, "PairValueRecord", []):
                            changed += cls._scale_value_record(getattr(record, "Value1", None), scale)
                            changed += cls._scale_value_record(getattr(record, "Value2", None), scale)
                elif fmt == 2:
                    for class1 in getattr(subtable, "Class1Record", []):
                        for class2 in getattr(class1, "Class2Record", []):
                            changed += cls._scale_value_record(getattr(class2, "Value1", None), scale)
                            changed += cls._scale_value_record(getattr(class2, "Value2", None), scale)
        return changed

    @staticmethod
    def _scale_optional_metric(obj, attr: str, scale: float) -> None:
        if hasattr(obj, attr):
            value = getattr(obj, attr)
            if value is not None:
                setattr(obj, attr, int(round(value * scale)))

    @classmethod
    def scale_font(cls, font: TTFont, scale: float) -> None:
        """Scale glyph outlines, advances, and kerning inside the current UPM.

        This changes optical glyph size without changing unitsPerEm or line
        metrics, keeping Kobo line spacing stable while making a small-looking
        face read larger.
        """
        if scale <= 0:
            raise ValueError("scale must be greater than zero")

        if "glyf" in font:
            glyf = font["glyf"]
            for name in font.getGlyphOrder():
                glyph = glyf[name]
                if glyph.isComposite():
                    for component in getattr(glyph, "components", []):
                        if hasattr(component, "x"):
                            component.x = int(round(component.x * scale))
                        if hasattr(component, "y"):
                            component.y = int(round(component.y * scale))
                elif getattr(glyph, "numberOfContours", 0) > 0 and hasattr(glyph, "coordinates"):
                    glyph.coordinates.scale((scale, scale))
                    glyph.coordinates.toInt()
                glyph.recalcBounds(glyf)

        if "hmtx" in font:
            for name, (advance, lsb) in list(font["hmtx"].metrics.items()):
                font["hmtx"].metrics[name] = (
                    int(round(advance * scale)),
                    int(round(lsb * scale)),
                )

        if "vmtx" in font:
            for name, (advance, tsb) in list(font["vmtx"].metrics.items()):
                font["vmtx"].metrics[name] = (
                    int(round(advance * scale)),
                    int(round(tsb * scale)),
                )

        if "kern" in font:
            for subtable in getattr(font["kern"], "kernTables", []):
                if hasattr(subtable, "kernTable"):
                    subtable.kernTable = {
                        pair: int(round(value * scale))
                        for pair, value in subtable.kernTable.items()
                    }

        cls._scale_gpos_pairpos(font, scale)

        if "OS/2" in font:
            os2 = font["OS/2"]
            for attr in (
                "xAvgCharWidth",
                "sxHeight",
                "sCapHeight",
                "ySubscriptXSize",
                "ySubscriptYSize",
                "ySubscriptXOffset",
                "ySubscriptYOffset",
                "ySuperscriptXSize",
                "ySuperscriptYSize",
                "ySuperscriptXOffset",
                "ySuperscriptYOffset",
                "yStrikeoutSize",
                "yStrikeoutPosition",
            ):
                cls._scale_optional_metric(os2, attr, scale)

        if "post" in font:
            cls._scale_optional_metric(font["post"], "underlinePosition", scale)
            cls._scale_optional_metric(font["post"], "underlineThickness", scale)

        if "hhea" in font and "hmtx" in font:
            advances = [advance for advance, _lsb in font["hmtx"].metrics.values()]
            if advances:
                font["hhea"].advanceWidthMax = max(advances)

    # ============================================================
    # OT-layout normalization
    # ============================================================

    # Map GSUB/GPOS subtable types (LookupType, Format) to the attribute
    # name of the array that runs parallel to Coverage.glyphs.  Subtables
    # not listed here either have no parallel array (e.g. Single Format 1,
    # where every covered glyph shares the same value) or use class-based
    # indexing that is independent of Coverage order (e.g. Pair Format 2).
    _PARALLEL_ARRAYS = {
        ("GSUB", 1, 2): "Substitute",
        ("GSUB", 2, 1): "Sequence",
        ("GSUB", 3, 1): "AlternateSet",
        ("GSUB", 4, 1): "LigatureSet",
        ("GPOS", 1, 2): "Value",
        ("GPOS", 2, 1): "PairSet",
        ("GPOS", 3, 1): "EntryExitRecord",
    }

    @classmethod
    def _sort_coverage_subtable(cls, sub, lookup_type: int, table_tag: str, font: TTFont) -> bool:
        """Sort Coverage.glyphs on one subtable, permuting its parallel array.

        Returns True if the subtable was modified.  Extension lookups
        (GSUB Type 7, GPOS Type 9) are unwrapped so the inner subtable is
        handled instead.
        """
        # Unwrap Extension lookups
        if lookup_type in (7, 9) and hasattr(sub, "ExtSubTable"):
            inner_type = getattr(sub, "ExtensionLookupType", None)
            if inner_type:
                return cls._sort_coverage_subtable(sub.ExtSubTable, inner_type, table_tag, font)
            return False

        cov = getattr(sub, "Coverage", None)
        if cov is None or not hasattr(cov, "glyphs"):
            return False

        glyph_ids = [font.getGlyphID(g) for g in cov.glyphs]
        if glyph_ids == sorted(glyph_ids):
            return False

        fmt = getattr(sub, "Format", None)
        parallel_attr = cls._PARALLEL_ARRAYS.get((table_tag, lookup_type, fmt))

        # Compute permutation that sorts glyph IDs ascending
        order = sorted(range(len(cov.glyphs)), key=glyph_ids.__getitem__)
        cov.glyphs = [cov.glyphs[i] for i in order]

        if parallel_attr and hasattr(sub, parallel_attr):
            arr = getattr(sub, parallel_attr)
            if arr is not None and len(arr) == len(order):
                setattr(sub, parallel_attr, [arr[i] for i in order])

        return True

    @classmethod
    def normalize_coverage(cls, font: TTFont) -> int:
        """Sort every Coverage table in GSUB/GPOS (and permute its parallel
        array), so renderers that binary-search Coverage get correct results.

        Returns the number of subtables that were fixed.  Lenient shapers
        (HarfBuzz, WebKit) accept unsorted Coverage, but strict consumers
        on desktop or legacy platforms can silently drop kern pairs or
        ligatures without this.
        """
        fixed = 0
        for tag in ("GSUB", "GPOS"):
            if tag not in font:
                continue
            table = font[tag].table
            lookup_list = getattr(table, "LookupList", None)
            if lookup_list is None:
                continue
            for lookup in lookup_list.Lookup:
                lt = lookup.LookupType
                for sub in lookup.SubTable:
                    if cls._sort_coverage_subtable(sub, lt, tag, font):
                        fixed += 1
        return fixed

    # ============================================================
    # Name table methods
    # ============================================================
    
    def rename_font(self, font: TTFont, metadata: FontMetadata) -> None:
        """
        Update the font's name-related metadata.
        This method uses the centralized `_set_name_records` helper to update
        all relevant name fields.
        """
        if "name" not in font:
            logger.warning("  No 'name' table found; skipping all name changes")
            return

        if self.prefix:
            logger.info("  Renaming the font to: " + f"{self.prefix} {metadata.full_name}")
            adjusted_family_name = f"{self.prefix} {metadata.family_name}"
            adjusted_full_name = f"{self.prefix} {metadata.full_name}"
        else:
            logger.info("  Updating font metadata (no prefix)")
            adjusted_family_name = metadata.family_name
            adjusted_full_name = metadata.full_name

        # Update Family Name
        self._set_name_records(font, 1, adjusted_family_name)
        # Update Subfamily
        self._set_name_records(font, 2, metadata.style_name)
        # Update Full Name
        self._set_name_records(font, 4, adjusted_full_name)

        # Update Typographic Family
        self._set_name_records(font, 16, adjusted_family_name)
        # Update Typographic Subfamily
        self._set_name_records(font, 17, metadata.style_name)
        # Update Compatible Full Name
        self._set_name_records(font, 18, adjusted_full_name)

        # Update Unique ID (ID 3)
        try:
            current_unique = font["name"].getName(3, 3, 1).toUnicode()
            parts = current_unique.split("Version")
            version_info = f"Version{parts[1]}" if len(parts) == 2 else "Version 1.000"
            if self.prefix:
                new_unique_id = f"{self.prefix} {metadata.family_name.strip()}:{version_info}"
            else:
                new_unique_id = f"{metadata.family_name.strip()}:{version_info}"
            if current_unique != new_unique_id:
                self._set_name_records(font, 3, new_unique_id)
        except Exception as e:
            logger.warning(f"  Failed to update Unique ID: {e}")

        # Update PostScript Name (ID 6)
        new_ps_name = metadata.ps_name
        self._set_name_records(font, 6, new_ps_name)

        # Update PostScript data in CFF (if applicable)
        if "CFF " in font:
            cff = font["CFF "].cff
            cff_topdict = cff.topDictIndex[0]

            if self.prefix:
                cff_full_name = f"{self.prefix} {metadata.full_name}"
                cff_family_name = f"{self.prefix} {metadata.family_name.replace(' ', '_')}"
            else:
                cff_full_name = metadata.full_name
                cff_family_name = metadata.family_name.replace(' ', '_')

            name_mapping = {
                "FullName": cff_full_name,
                "FamilyName": cff_family_name
            }

            for key, new_value in name_mapping.items():
                if key in cff_topdict.rawDict:
                    current_value = cff_topdict.rawDict[key]
                    if current_value != new_value:
                        cff_topdict.rawDict[key] = new_value
                        logger.debug(f"  CFF table '{key}' updated to '{new_value}'.")
                    else:
                        logger.debug(f"  CFF table '{key}' is already correct.")

            logger.warning("  CFF table found. The original font name may persist as part of an indexed `Name INDEX`. (This cannot be easily fixed with this script. If you are encountering issues, I recommend using FontForge.)")
        else:
            logger.debug("  No 'CFF' table in this font.")

        # Update PostScript data if relevant
        if "post" in font:
            if hasattr(font["post"], "fontName"):
                new_ps_name = metadata.ps_name
                if font["post"].fontName != new_ps_name:
                    font["post"].fontName = new_ps_name
                    logger.debug(f"  'post' table updated with new fontName '{new_ps_name}'.")
                else:
                    logger.debug("  'post' table fontName is already correct.")
        else:
            logger.debug("  No 'post' table in this font.")

    # ============================================================
    # Copyright stamp
    # ============================================================

    def stamp_copyright(self, font: TTFont) -> None:
        """Append a kobo-font-fix patch stamp to the copyright (name ID 0)."""
        if "name" not in font:
            return
        record = font["name"].getName(0, 3, 1, 0x0409) or font["name"].getName(0, 1, 0, 0)
        current = record.toUnicode() if record else ""
        new_value = _build_stamped_copyright(current)
        if new_value != current:
            self._set_name_records(font, 0, new_value)
            logger.info("  Notice stamp applied in copyright section")

    # ============================================================
    # Weight metadata methods
    # ============================================================

    def update_weight_metadata(self, font: TTFont, filename: str) -> None:
        """
        Update font weight metadata based on filename suffix.
        This function uses the centralized style lookup, which simplifies
        the logic significantly.
        """
        style_name, os2_weight = self._get_style_from_filename(filename)
        ps_weight = style_name.replace(" ", "")
        
        if "OS/2" in font and hasattr(font["OS/2"], "usWeightClass"):
            if font["OS/2"].usWeightClass != os2_weight:
                font["OS/2"].usWeightClass = os2_weight
                logger.debug(f"  OS/2 usWeightClass updated to {os2_weight}.")
            else:
                logger.debug("  OS/2 usWeightClass is already correct.")
        
        if "CFF " in font and hasattr(font["CFF "].cff.topDictIndex[0], "Weight"):
            if getattr(font["CFF "].cff.topDictIndex[0], "Weight", "") != ps_weight:
                font["CFF "].cff.topDictIndex[0].Weight = ps_weight
                logger.debug(f"  PostScript CFF weight updated to '{ps_weight}'.")
        elif "post" in font and hasattr(font["post"], "Weight"):
            if getattr(font["post"], "Weight", "") != ps_weight:
                font["post"].Weight = ps_weight
                logger.debug(f"  PostScript 'post' weight updated to '{ps_weight}'.")

    # ============================================================
    # Style flag methods
    # ============================================================

    @staticmethod
    def _style_flags(style_name: str) -> Tuple[bool, bool, bool]:
        """Return (is_bold, is_italic, is_regular) derived from style_name."""
        is_italic = "Italic" in style_name
        is_bold = "Bold" in style_name
        is_regular = style_name == "Regular"
        return is_bold, is_italic, is_regular

    @staticmethod
    def _expected_style_flags(style_name: str) -> Tuple[int, int]:
        """Return the expected (fsSelection, macStyle) bit masks for a style.

        Only the style-related bits are returned; callers must merge these
        into existing values while clearing the style bits they manage.
        """
        is_bold, is_italic, is_regular = FontProcessor._style_flags(style_name)
        fs = 0
        if is_italic: fs |= 0x01     # ITALIC
        if is_bold:   fs |= 0x20     # BOLD
        if is_regular: fs |= 0x40    # REGULAR

        ms = 0
        if is_bold:   ms |= 0x01
        if is_italic: ms |= 0x02
        return fs, ms

    def update_style_flags(self, font: TTFont, filename: str) -> None:
        """Synchronize OS/2.fsSelection and head.macStyle with the style_name.

        Some source fonts ship with style-related bits that disagree with
        the name table (for example an italic weight with the italic bits
        cleared and the regular bit set).  Once the name table is rewritten
        to reflect the filename-derived style, the binary flags must be
        brought into agreement — otherwise renderers that trust fsSelection
        or macStyle instead of the name table will treat the font wrongly.
        """
        style_name, _ = self._get_style_from_filename(filename)
        expected_fs, expected_ms = self._expected_style_flags(style_name)
        style_fs_mask = 0x01 | 0x20 | 0x40  # italic | bold | regular
        style_ms_mask = 0x01 | 0x02          # bold | italic

        if "OS/2" in font and hasattr(font["OS/2"], "fsSelection"):
            current = font["OS/2"].fsSelection
            new_value = (current & ~style_fs_mask) | expected_fs
            if current != new_value:
                font["OS/2"].fsSelection = new_value
                logger.debug(f"  OS/2 fsSelection: 0x{current:04x} -> 0x{new_value:04x}")

        if "head" in font and hasattr(font["head"], "macStyle"):
            current = font["head"].macStyle
            new_value = (current & ~style_ms_mask) | expected_ms
            if current != new_value:
                font["head"].macStyle = new_value
                logger.debug(f"  head macStyle: 0x{current:04x} -> 0x{new_value:04x}")

    # ============================================================
    # PANOSE methods
    # ============================================================
    
    def check_and_fix_panose(self, font: TTFont, filename: str) -> None:
        """
        Check and adjust PANOSE values based on filename suffix.
        PANOSE is an older classification system for fonts. Correcting these
        values ensures better compatibility with legacy systems and font menus.
        """
        style_name, _ = self._get_style_from_filename(filename)
        
        # PANOSE expected values for each style
        style_specs = {
            "Bold Italic": {"weight": 8, "letterform": 3},
            "Bold": {"weight": 8, "letterform": 2},
            "Italic": {"weight": 5, "letterform": 3},
            "Regular": {"weight": 5, "letterform": 2},
        }
        
        if "OS/2" not in font or not hasattr(font["OS/2"], "panose") or font["OS/2"].panose is None:
            logger.warning("  No OS/2 table or PANOSE information found; skipping check.")
            return

        panose = font["OS/2"].panose
        # bFamilyType 0 means "Any" — the font has no meaningful PANOSE data
        if panose.bFamilyType == 0:
            logger.info("  No PANOSE classification set (bFamilyType=0); skipping check.")
            return
        expected = style_specs.get(style_name)
        if not expected:
            logger.warning(f"  No PANOSE specification for style '{style_name}'; skipping.")
            return
        
        changes = []
        if panose.bWeight != expected["weight"]:
            old_weight = panose.bWeight
            panose.bWeight = expected["weight"]
            changes.append(f"bWeight {old_weight}->{expected['weight']}")

        if panose.bLetterForm != expected["letterform"]:
            old_letterform = panose.bLetterForm
            panose.bLetterForm = expected["letterform"]
            changes.append(f"bLetterForm {old_letterform}->{expected['letterform']}")
        
        if changes:
            logger.info(f"  PANOSE corrected: {', '.join(changes)}")
        else:
            logger.info("  PANOSE check passed, no modifications required.")
    
    # ============================================================
    # Hinting methods
    # ============================================================

    # A single SVTCA[Y] (opcode 0x00): it sets the projection and freedom
    # vectors to the y-axis and moves no points. Giving an otherwise
    # uninstructed glyph this one-byte program is the whole Kobo wobble fix
    # (see below) — the outline is emitted byte-for-byte unchanged.
    _NOOP_BYTECODE = b"\x00"
    _HINTING_TABLES = (
        "cvt ",
        "cvar",
        "fpgm",
        "prep",
        "hdmx",
        "LTSH",
        "VDMX",
        "gasp",
        "TSI0",
        "TSI1",
        "TSI2",
        "TSI3",
        "TSI5",
    )

    @classmethod
    def _font_has_meaningful_hints(cls, font: TTFont) -> bool:
        """Check whether real glyphs contain TrueType bytecode.

        Global tables such as prep/fpgm/cvt are not enough: some fonts carry
        only scan-conversion setup or .notdef bytecode. The no-op program we
        inject for the Kobo wobble fix is likewise not "meaningful" hinting, so
        a re-run still recognises a previously processed font as unhinted and
        leaves the no-op in place instead of trying to ttfautohint it.
        """
        if "glyf" in font:
            for glyph_name in font.getGlyphOrder():
                if glyph_name == ".notdef":
                    continue
                glyph = font["glyf"][glyph_name]
                if not (hasattr(glyph, 'program') and glyph.program):
                    continue
                bytecode = glyph.program.getBytecode()
                if bytecode and bytecode != cls._NOOP_BYTECODE:
                    return True
        return False

    @classmethod
    def _font_needs_noop_hints(cls, font: TTFont) -> bool:
        """Whether any outline glyph lacks the standard no-op program.

        KF output deliberately uses one uniform no-op program for every outline
        glyph. That removes source hinting while still routing iType through
        its interpreter instead of the automatic grid-fitting path that wobbles.
        """
        if "glyf" not in font:
            return False
        glyf = font["glyf"]
        for name in font.getGlyphOrder():
            glyph = glyf[name]
            if glyph.numberOfContours == 0:  # empty glyph (space, etc.)
                continue
            if (hasattr(glyph, "program") and glyph.program
                    and glyph.program.getBytecode() == cls._NOOP_BYTECODE):
                continue
            return True
        return False

    @classmethod
    def _add_noop_hints(cls, font: TTFont) -> int:
        """Give every outline glyph the same no-op program.

        Returns the number of glyphs changed. Composite glyphs are handled too:
        fontTools sets each composite's WE_HAVE_INSTRUCTIONS component flag when
        a program is present at compile time. maxp hinting fields are reset for
        the no-op-only programs.
        """
        if "glyf" not in font:
            return 0
        glyf = font["glyf"]
        count = 0
        for name in font.getGlyphOrder():
            glyph = glyf[name]
            if glyph.numberOfContours == 0:
                continue
            if (hasattr(glyph, "program") and glyph.program
                    and glyph.program.getBytecode() == cls._NOOP_BYTECODE):
                continue
            prog = ttProgram.Program()
            prog.fromBytecode(cls._NOOP_BYTECODE)
            glyph.program = prog
            count += 1
        cls._reset_noop_hinting_limits(font)
        return count

    @classmethod
    def _strip_hinting_tables(cls, font: TTFont) -> int:
        """Remove global TrueType hinting and hint-derived metric tables."""
        removed = 0
        for tag in cls._HINTING_TABLES:
            if tag in font:
                del font[tag]
                removed += 1
        return removed

    @classmethod
    def _reset_noop_hinting_limits(cls, font: TTFont) -> None:
        if "maxp" not in font:
            return
        maxp = font["maxp"]
        maxp.maxZones = 1
        maxp.maxTwilightPoints = 0
        maxp.maxStorage = 0
        maxp.maxFunctionDefs = 0
        maxp.maxInstructionDefs = 0
        maxp.maxStackElements = 0
        maxp.maxSizeOfInstructions = len(cls._NOOP_BYTECODE)

    def apply_ttfautohint(self, font_path: str) -> bool:
        """Run ttfautohint on a saved font file, replacing it in-place.

        Uses natural stem widths (--stem-width-mode=nss) for Kobo's FreeType
        grayscale renderer, which produces less distortion than the default
        strong grid-fitting.
        """
        if shutil.which("ttfautohint") is None:
            logger.error("  ttfautohint is required to rehint this font after outline processing.")
            return False
        try:
            hinted_path = font_path + ".hinted"
            subprocess.run(
                [
                    "ttfautohint",
                    "--stem-width-mode=nss",
                    font_path,
                    hinted_path,
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            os.replace(hinted_path, font_path)
            logger.info("  Applied ttfautohint (natural stem widths).")
            return True
        except subprocess.CalledProcessError as e:
            logger.warning(f"  ttfautohint failed: {e}")
            # Clean up temp file if it exists
            hinted_path = font_path + ".hinted"
            if os.path.exists(hinted_path):
                os.remove(hinted_path)
            return False

    # ============================================================
    # Line adjustment methods
    # ============================================================
    
    def apply_line_adjustment(self, font_path: str) -> bool:
        """
        Apply font-line baseline adjustment to the font.
        This external tool fixes an issue with line spacing on some e-readers.
        The function handles the necessary file operations (renaming and cleanup)
        after the external utility has run.
        """
        try:
            subprocess.run(["font-line", "percent", str(self.line_percent), font_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            
            base, ext = os.path.splitext(font_path)
            linegap_file = f"{base}-linegap{self.line_percent}{ext}"
            
            if os.path.exists(linegap_file):
                os.remove(font_path)
                os.rename(linegap_file, font_path)
                logger.info(f"  Line spacing adjusted ({self.line_percent}% baseline shift).")
                return True
            else:
                logger.warning(f"  Expected font-line output '{linegap_file}' not found.")
                return False
        except subprocess.CalledProcessError as e:
            logger.warning(f"  font-line failed: {e}")
            return False
        except Exception as e:
            logger.warning(f"  Unexpected error during line adjustment: {e}")
            return False
    
    # ============================================================
    # Main processing method
    # ============================================================
    
    def _resolve_family_name(self, font: TTFont, new_name: Optional[str], remove_prefix: Optional[str]) -> Optional[str]:
        """
        Determine the effective family name for the font.
        Strips known prefixes (from presets) automatically, then applies
        --remove-prefix and --name overrides.
        """
        if new_name is not None:
            return new_name

        current_family_name = font["name"].getBestFamilyName()
        if not current_family_name:
            return None

        # Auto-strip known prefixes (NV, KF, etc.)
        family_name = current_family_name
        for known in KNOWN_PREFIXES:
            if family_name.startswith(known + " "):
                family_name = family_name[len(known + " "):]
                break

        # Also handle --remove-prefix for custom prefixes
        if remove_prefix and current_family_name.startswith(remove_prefix + " "):
            family_name = current_family_name[len(remove_prefix + " "):]

        # Return None if nothing changed (let _get_font_metadata use the stripped name)
        return family_name if family_name != current_family_name else None

    def _analyze_changes(self,
        font: TTFont,
        font_path: str,
        kern_mode: str,
        metadata: FontMetadata,
        is_otf: bool = False,
        stamp: bool = False,
        outline_mode: str = "apply",
        scale: float = 1.0,
    ) -> List[str]:
        """
        Analyze what changes would be made to the font.
        Returns a list of human-readable change descriptions.
        This is the single source of truth for both dry-run and processing.
        """
        changes = []

        if is_otf:
            changes.append("Convert OTF (CFF) to TTF with FontForge")

        # Check Coverage sort order in GSUB/GPOS
        unsorted_cov = 0
        for tag in ("GSUB", "GPOS"):
            if tag not in font:
                continue
            lookup_list = getattr(font[tag].table, "LookupList", None)
            if lookup_list is None:
                continue
            for lookup in lookup_list.Lookup:
                for sub in lookup.SubTable:
                    target = sub
                    if lookup.LookupType in (7, 9) and hasattr(sub, "ExtSubTable"):
                        target = sub.ExtSubTable
                    cov = getattr(target, "Coverage", None)
                    if cov is None or not hasattr(cov, "glyphs"):
                        continue
                    ids = [font.getGlyphID(g) for g in cov.glyphs]
                    if ids != sorted(ids):
                        unsorted_cov += 1
        if unsorted_cov:
            changes.append(f"Sort {unsorted_cov} unsorted Coverage table(s) in GSUB/GPOS")

        # Check WWS names
        if "name" in font and font["name"]:
            has_wws = any(n.nameID in (21, 22) for n in font["name"].names)
            if has_wws:
                changes.append("Remove WWS Family/Subfamily names (ID 21, 22)")

        # Check rename
        if self.prefix:
            target_full = f"{self.prefix} {metadata.full_name}"
        else:
            target_full = metadata.full_name
        current_full = font["name"].getBestFullName() if "name" in font else None
        if current_full != target_full:
            changes.append(f"Rename font to '{target_full}'")

        # Check PANOSE
        style_name, _ = self._get_style_from_filename(font_path)
        style_specs = {
            "Bold Italic": {"weight": 8, "letterform": 3},
            "Bold": {"weight": 8, "letterform": 2},
            "Italic": {"weight": 5, "letterform": 3},
            "Regular": {"weight": 5, "letterform": 2},
        }
        if "OS/2" in font and hasattr(font["OS/2"], "panose") and font["OS/2"].panose:
            panose = font["OS/2"].panose
            # Skip fonts with no meaningful PANOSE data (bFamilyType 0 = "Any")
            if panose.bFamilyType != 0:
                expected = style_specs.get(style_name, {})
                if expected:
                    if panose.bWeight != expected["weight"]:
                        changes.append(f"Fix PANOSE bWeight: {panose.bWeight} -> {expected['weight']}")
                    if panose.bLetterForm != expected["letterform"]:
                        changes.append(f"Fix PANOSE bLetterForm: {panose.bLetterForm} -> {expected['letterform']}")

        # Check weight metadata
        _, os2_weight = self._get_style_from_filename(font_path)
        if "OS/2" in font and hasattr(font["OS/2"], "usWeightClass"):
            if font["OS/2"].usWeightClass != os2_weight:
                changes.append(f"Update usWeightClass: {font['OS/2'].usWeightClass} -> {os2_weight}")

        # Check style flags (fsSelection / macStyle)
        style_name, _ = self._get_style_from_filename(font_path)
        expected_fs, expected_ms = self._expected_style_flags(style_name)
        style_fs_mask = 0x01 | 0x20 | 0x40
        style_ms_mask = 0x01 | 0x02
        if "OS/2" in font and hasattr(font["OS/2"], "fsSelection"):
            current_fs = font["OS/2"].fsSelection
            new_fs = (current_fs & ~style_fs_mask) | expected_fs
            if current_fs != new_fs:
                changes.append(f"Update fsSelection: 0x{current_fs:04x} -> 0x{new_fs:04x} (style bits)")
        if "head" in font and hasattr(font["head"], "macStyle"):
            current_ms = font["head"].macStyle
            new_ms = (current_ms & ~style_ms_mask) | expected_ms
            if current_ms != new_ms:
                changes.append(f"Update macStyle: 0x{current_ms:04x} -> 0x{new_ms:04x} (style bits)")

        if scale != 1.0:
            changes.append(f"Scale glyphs and advances to {scale:.3g}x")

        # Check kerning
        # Note: As of firmware 4.45, Kobo reads GPOS kerning data correctly,
        # but only when webkitTextRendering=optimizeLegibility is enabled.
        # Since this setting is disabled by default, a legacy kern table is
        # still needed for most users.
        if kern_mode in ("add-legacy-kern", "legacy-kern-only"):
            has_kern = "kern" in font
            has_gpos = "GPOS" in font

            new_pairs = self.extract_kern_pairs(font)
            if new_pairs:
                new_items = [(tuple(k), int(v)) for k, v in new_pairs.items() if v]
                if len(new_items) > 10920:
                    cmap_reverse = {}
                    if "cmap" in font:
                        for table in font["cmap"].tables:
                            if hasattr(table, "cmap"):
                                for cp, glyph_name in table.cmap.items():
                                    if glyph_name not in cmap_reverse:
                                        cmap_reverse[glyph_name] = cp
                    new_items.sort(key=lambda pair: (
                        self._glyph_priority(pair[0][0], cmap_reverse) +
                        self._glyph_priority(pair[0][1], cmap_reverse)
                    ))
                    total = len(new_items)
                    new_items = new_items[:10920]
                else:
                    total = len(new_items)
                new_table = dict(new_items)

                if has_kern:
                    existing_pairs = {}
                    for st in font["kern"].kernTables:
                        if hasattr(st, "kernTable"):
                            existing_pairs.update(st.kernTable)
                    if existing_pairs != new_table:
                        changes.append(f"Update kern table ({len(new_table)} pairs)")
                else:
                    changes.append(f"Create legacy kern table from GPOS ({len(new_table)} pairs)")
                if total > 10920:
                    changes.append(f"  Truncate from {total} to 10920 pairs (format 0 limit)")
            else:
                if not has_kern and has_gpos:
                    changes.append("GPOS table found but contained no kern pairs")
                elif not has_kern:
                    changes.append("No kerning data found (no GPOS or kern table)")

            if kern_mode == "legacy-kern-only" and has_gpos:
                changes.append("Remove GPOS table")

        # Check composite outlines
        if outline_mode == "apply" and "glyf" in font:
            composite_count = sum(
                1 for name in font.getGlyphOrder()
                if font["glyf"][name].isComposite()
            )
            if composite_count:
                changes.append(f"Flatten {composite_count} composite glyph(s)")
            if self.prefix != "KF" and self._font_has_meaningful_hints(font):
                changes.append("Rehint after outline processing")

        # KF output deliberately removes TrueType hinting and uses a uniform
        # no-op per-glyph program. This routes Kobo iType through its
        # interpreter without grid-fitting the outlines.
        if self.prefix == "KF":
            has_hint_tables = any(tag in font for tag in self._HINTING_TABLES)
            if has_hint_tables or self._font_needs_noop_hints(font):
                changes.append("Strip hinting and set no-op glyph instructions (fix iType wobble)")

        # Spaces: always ensure common Unicode space characters exist so Kobo
        # does not render a .notdef box in their place. Independent of preset.
        if "cmap" in font:
            best_cmap = font.getBestCmap() or {}
            missing = [cp for cp, _name, _spec in SPACE_GLYPHS if cp not in best_cmap]
            if missing:
                joined = ", ".join(f"U+{cp:04X}" for cp in missing)
                changes.append(f"Add {len(missing)} missing space glyph(s): {joined}")

            # Hyphen/dash characters cloned from an existing glyph's shape.
            clone_missing = [
                cp for cp, _name, sources in CLONE_GLYPHS
                if cp not in best_cmap and any(best_cmap.get(s) for s in sources)
            ]
            if clone_missing:
                joined = ", ".join(f"U+{cp:04X}" for cp in clone_missing)
                changes.append(f"Add {len(clone_missing)} missing hyphen/dash glyph(s) cloned from existing shapes: {joined}")

            # Figure dash (U+2012): en dash bar re-spaced to the digit width.
            if (FIGURE_DASH_CODEPOINT not in best_cmap
                    and any(best_cmap.get(s) for s in FIGURE_DASH_SOURCE_CODEPOINTS)):
                changes.append("Add missing figure dash (U+2012) at digit width")

        # Check line adjustment
        if self.line_percent != 0:
            if "OS/2" in font and "head" in font:
                upm = font["head"].unitsPerEm
                asc = font["OS/2"].sTypoAscender
                desc = font["OS/2"].sTypoDescender
                gap = font["OS/2"].sTypoLineGap
                current_pct = round(((asc - desc + gap) / upm - 1) * 100)
                if current_pct != self.line_percent:
                    changes.append(f"Adjust line spacing ({self.line_percent}% baseline shift, currently {current_pct}%)")
            else:
                changes.append(f"Adjust line spacing ({self.line_percent}% baseline shift)")

        # Check copyright stamp
        if stamp and self.prefix == "KF" and "name" in font:
            record = font["name"].getName(0, 3, 1, 0x0409) or font["name"].getName(0, 1, 0, 0)
            current = record.toUnicode() if record else ""
            if _build_stamped_copyright(current) != current:
                changes.append("Stamp copyright with kobo-font-fix patch note")

        # Check output path
        output_path = self._generate_output_path(font_path, metadata)
        if output_path != font_path:
            changes.append(f"Save as: {output_path}")

        return changes

    @staticmethod
    def simplify_outlines(font: TTFont) -> bool:
        """Remove overlapping contours and correct path direction.

        Uses fontTools + skia-pathops for overlap removal.  Returns True
        if the outlines were modified, False if skia-pathops is not
        available or no changes were needed.

        Important: removeOverlaps rewrites the glyf outlines and removes
        per-glyph TrueType bytecode. Global hint tables such as fpgm, prep,
        and cvt may remain, but the usable glyph-level programs are gone.
        Any meaningful source hints must therefore be considered invalid
        after this step.
        """
        from fontTools.ttLib.removeOverlaps import removeOverlaps
        removeOverlaps(font)
        return True

    @staticmethod
    def clean_degenerate_contours(font: TTFont) -> int:
        """Remove zero-area contours (<=2 points) from a font's glyf table.

        These degenerate contours can cause rendering artifacts on some
        engines.  Returns the number of contours removed.
        """
        from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates

        glyf = font["glyf"]
        removed_total = 0
        modified = set()

        for name in font.getGlyphOrder():
            glyph = glyf[name]
            if glyph.isComposite():
                continue
            end_pts = getattr(glyph, "endPtsOfContours", None)
            if not end_pts:
                continue

            coords = glyph.coordinates
            flags = glyph.flags

            new_coords = []
            new_flags = []
            new_end_pts = []

            start = 0
            removed = 0
            for end in end_pts:
                count = end - start + 1
                if count <= 2:
                    removed += 1
                else:
                    new_coords.extend(coords[start:end + 1])
                    new_flags.extend(flags[start:end + 1])
                    new_end_pts.append(len(new_coords) - 1)
                start = end + 1

            if removed:
                removed_total += removed
                modified.add(name)
                glyph.coordinates = GlyphCoordinates(new_coords)
                glyph.flags = new_flags
                glyph.endPtsOfContours = new_end_pts
                glyph.numberOfContours = len(new_end_pts)

        if removed_total:
            glyph_set = font.getGlyphSet()
            for name in modified:
                glyph = glyf[name]
                if hasattr(glyph, "recalcBounds"):
                    glyph.recalcBounds(glyph_set)

        return removed_total

    @staticmethod
    def flatten_composites(font: TTFont) -> int:
        """Convert TrueType composite glyphs to simple outlines.

        Some Kobo firmware text paths appear to mishandle composite and nested
        composite glyphs. Flattening preserves the final glyph shapes while
        removing component references from the saved glyf table.
        """
        if "glyf" not in font:
            return 0

        from fontTools.pens.basePen import DecomposingPen
        from fontTools.pens.ttGlyphPen import TTGlyphPen

        class DecomposingTTGlyphPen(DecomposingPen, TTGlyphPen):
            pass

        glyf = font["glyf"]
        glyph_set = font.getGlyphSet()
        composite_names = [
            name for name in font.getGlyphOrder()
            if glyf[name].isComposite()
        ]

        for name in composite_names:
            pen = DecomposingTTGlyphPen(glyph_set)
            glyph_set[name].draw(pen)
            glyph = pen.glyph()
            if hasattr(glyph, "recalcBounds"):
                glyph.recalcBounds(glyf)
            glyf[name] = glyph

        if composite_names and "maxp" in font:
            maxp = font["maxp"]
            if hasattr(maxp, "maxComponentDepth"):
                maxp.maxComponentDepth = 0
            if hasattr(maxp, "maxComponentElements"):
                maxp.maxComponentElements = 0

        return len(composite_names)

    @staticmethod
    def _digit_width(font: TTFont, cmap: dict) -> Optional[int]:
        """Advance width of the font's '0' — the conventional figure width.

        Tabular figures are all the width of the zero, and '0' is essentially
        never the proportional outlier, so it is the standard reference for
        figure-width characters (figure space, figure dash). Falls back to the
        first other available digit; None only when the font has no digits.
        """
        hmtx = font["hmtx"].metrics if "hmtx" in font else {}
        for cp in range(0x0030, 0x003A):  # '0' first, then 1-9 as a fallback
            name = cmap.get(cp)
            if name and name in hmtx:
                return hmtx[name][0]
        return None

    @classmethod
    def _resolve_space_width(cls, font: TTFont, cmap: dict, upm: int, spec) -> int:
        """Resolve a SPACE_GLYPHS width spec to an advance width in font units."""
        mode, value = spec
        hmtx = font["hmtx"].metrics if "hmtx" in font else {}

        def glyph_width(codepoint: int) -> Optional[int]:
            name = cmap.get(codepoint)
            if name and name in hmtx:
                return hmtx[name][0]
            return None

        if mode == "em":
            return round(upm * value)
        if mode == "zero":
            return 0
        if mode == "space":
            base = glyph_width(0x0020)
            return round(base * value) if base is not None else round(upm / 5)
        if mode == "digit":
            w = cls._digit_width(font, cmap)
            return w if w is not None else round(upm / 2)
        if mode == "period":
            w = glyph_width(0x002E)
            return w if w is not None else round(upm / 4)
        return round(upm / 5)

    @staticmethod
    def _vmtx_default(font: TTFont) -> Tuple[int, int]:
        if "vmtx" not in font:
            return (0, 0)
        vmtx = font["vmtx"].metrics
        for name in ("space", ".notdef"):
            if name in vmtx:
                return vmtx[name]
        if vmtx:
            return next(iter(vmtx.values()))
        return (0, 0)

    @classmethod
    def add_missing_spaces(cls, font: TTFont) -> List[Tuple[int, int]]:
        """Add glyphs for common Unicode space characters the font is missing.

        Many fonts omit fixed-width and format spaces (thin space, narrow
        no-break space, en/em spaces, etc.), so Kobo renders a .notdef box
        wherever one is used. For each codepoint in SPACE_GLYPHS not already in
        the cmap, an empty glyph is added and mapped in every Unicode cmap
        subtable, with an advance width resolved from the typeface (see
        SPACE_GLYPHS / _resolve_space_width). Glyph order, hmtx and maxp are
        updated. Returns a list of (codepoint, advance width) added; empty when
        nothing was missing or the font lacks glyf/cmap.
        """
        if "glyf" not in font or "cmap" not in font:
            return []
        cmap = font.getBestCmap() or {}
        upm = font["head"].unitsPerEm if "head" in font else 1000

        added: List[Tuple[int, int]] = []
        for codepoint, name, spec in SPACE_GLYPHS:
            if codepoint in cmap:
                continue  # already present
            width = cls._resolve_space_width(font, cmap, upm, spec)

            if name not in font.getGlyphOrder():
                glyph = Glyph()
                glyph.numberOfContours = 0
                font["glyf"][name] = glyph  # also appends to glyf.glyphOrder
                order = font.getGlyphOrder()
                if name not in order:
                    font.setGlyphOrder(order + [name])
                font["hmtx"][name] = (width, 0)
                if "vmtx" in font:
                    font["vmtx"][name] = cls._vmtx_default(font)

            for table in font["cmap"].tables:
                if table.isUnicode():
                    table.cmap[codepoint] = name

            added.append((codepoint, width))

        if added and "maxp" in font:
            font["maxp"].numGlyphs = len(font.getGlyphOrder())

        return added

    @classmethod
    def add_missing_clones(cls, font: TTFont) -> List[Tuple[int, str]]:
        """Add glyphs that should share an existing glyph's shape (see CLONE_GLYPHS).

        For each codepoint not already in the cmap, the first available source
        glyph (by preference order) is deep-copied — contours and advance width
        — under a new name and mapped in every Unicode cmap subtable. This gives
        characters like the soft hyphen or non-breaking hyphen the correct
        visible shape instead of a .notdef box. A codepoint is skipped when the
        font has none of its source glyphs. Returns a list of (codepoint,
        source glyph name) added.
        """
        if "glyf" not in font or "cmap" not in font:
            return []
        cmap = font.getBestCmap() or {}
        glyf = font["glyf"]

        added: List[Tuple[int, str]] = []
        for codepoint, name, sources in CLONE_GLYPHS:
            if codepoint in cmap:
                continue  # already present
            source_name = None
            for src_cp in sources:
                candidate = cmap.get(src_cp)
                if candidate and candidate in glyf:
                    source_name = candidate
                    break
            if source_name is None:
                continue  # no source glyph to clone from

            if name not in font.getGlyphOrder():
                glyf[name] = copy.deepcopy(glyf[source_name])
                order = font.getGlyphOrder()
                if name not in order:
                    font.setGlyphOrder(order + [name])
                font["hmtx"][name] = font["hmtx"][source_name]
                if "vmtx" in font:
                    vmtx = font["vmtx"].metrics
                    if source_name in vmtx:
                        vmtx[name] = vmtx[source_name]
                    else:
                        vmtx[name] = cls._vmtx_default(font)

            for table in font["cmap"].tables:
                if table.isUnicode():
                    table.cmap[codepoint] = name

            added.append((codepoint, source_name))

        if added and "maxp" in font:
            font["maxp"].numGlyphs = len(font.getGlyphOrder())

        return added

    @classmethod
    def add_missing_figure_dash(cls, font: TTFont) -> Optional[int]:
        """Add FIGURE DASH (U+2012) when missing.

        A figure dash is a dash whose advance equals the figure (digit) width,
        used to align with numerals. It is not a plain clone: we take the en
        dash's bar (its stroke, height and thickness are already correct),
        re-space it to the width of the '0', and centre the ink in that advance.
        Falls back to the hyphen if there is no en dash, and to half the em if
        the font has no digits. Returns the advance width used, or None when the
        font already has U+2012 or has no dash to base it on.
        """
        if "glyf" not in font or "cmap" not in font:
            return None
        cmap = font.getBestCmap() or {}
        if FIGURE_DASH_CODEPOINT in cmap:
            return None

        source_name = None
        for src_cp in FIGURE_DASH_SOURCE_CODEPOINTS:
            candidate = cmap.get(src_cp)
            if candidate and candidate in font["glyf"]:
                source_name = candidate
                break
        if source_name is None:
            return None

        upm = font["head"].unitsPerEm if "head" in font else 1000
        width = cls._digit_width(font, cmap)
        if width is None:
            width = round(upm / 2)

        glyf = font["glyf"]
        name = FIGURE_DASH_GLYPH_NAME
        if name not in font.getGlyphOrder():
            # Decompose the source into a simple outline so we can translate it,
            # then centre its ink horizontally within the digit-width advance.
            from fontTools.pens.basePen import DecomposingPen
            from fontTools.pens.ttGlyphPen import TTGlyphPen

            class DecomposingTTGlyphPen(DecomposingPen, TTGlyphPen):
                pass

            pen = DecomposingTTGlyphPen(font.getGlyphSet())
            font.getGlyphSet()[source_name].draw(pen)
            glyph = pen.glyph()
            glyph.recalcBounds(glyf)

            lsb = 0
            if getattr(glyph, "numberOfContours", 0) > 0 and hasattr(glyph, "coordinates"):
                ink = glyph.xMax - glyph.xMin
                lsb = round((width - ink) / 2)
                dx = lsb - glyph.xMin
                if dx:
                    glyph.coordinates.translate((dx, 0))
                    glyph.recalcBounds(glyf)
                    lsb = glyph.xMin

            glyf[name] = glyph
            order = font.getGlyphOrder()
            if name not in order:
                font.setGlyphOrder(order + [name])
            font["hmtx"][name] = (width, lsb)
            if "vmtx" in font:
                font["vmtx"][name] = cls._vmtx_default(font)

        for table in font["cmap"].tables:
            if table.isUnicode():
                table.cmap[FIGURE_DASH_CODEPOINT] = name

        if "maxp" in font:
            font["maxp"].numGlyphs = len(font.getGlyphOrder())

        return width

    def process_font(self,
        kern_mode: str,
        font_path: str,
        new_name: Optional[str] = None,
        remove_prefix: Optional[str] = None,
        dry_run: bool = False,
        outline_mode: str = "apply",
        stamp: bool = False,
        scale: float = 1.0,
    ) -> bool:
        """
        Process a single font file, or report what would change in dry-run mode.
        """
        label = "Dry run" if dry_run else "Processing"
        logger.info(f"\n{label}: {font_path}")

        try:
            font = TTFont(font_path)
        except Exception as e:
            logger.error(f"  Failed to open font: {e}")
            return False

        is_otf = "CFF " in font
        if is_otf:
            logger.info("  Source: OTF (CFF outlines, will be converted to TTF via FontForge)")
            font.close()
            try:
                font = fontforge_otf_to_ttf(font_path)
            except Exception as e:
                logger.error(f"  FontForge OTF->TTF conversion failed: {e}")
                return False

        # Report hinting status
        has_meaningful_hints = self._font_has_meaningful_hints(font)
        hint_tables = [t for t in ("fpgm", "prep", "cvt ") if t in font]
        if has_meaningful_hints:
            logger.info(f"  Hinting: glyph-level present (tables: {', '.join(hint_tables) if hint_tables else 'glyph-level only'})")
        elif hint_tables:
            logger.info(f"  Hinting: global tables only ({', '.join(hint_tables)})")
        else:
            logger.info(f"  Hinting: none")

        effective_name = self._resolve_family_name(font, new_name, remove_prefix)
        metadata = self._get_font_metadata(font, font_path, effective_name)
        if not metadata:
            return False

        changes = self._analyze_changes(
            font,
            font_path,
            kern_mode,
            metadata,
            is_otf,
            stamp,
            outline_mode,
            scale,
        )

        if not changes:
            logger.info("  No changes needed.")
            return True

        # Report changes
        for change in changes:
            logger.info(f"  {change}")

        if dry_run:
            return True

        # Apply changes
        try:
            # Remove WWS names
            if "name" in font and font["name"]:
                old_names_list = font["name"].names
                new_names_list = [n for n in old_names_list if n.nameID not in (21, 22)]
                font["name"].names = new_names_list

            self.rename_font(font, metadata)
            if stamp and self.prefix == "KF":
                self.stamp_copyright(font)
            self.check_and_fix_panose(font, font_path)
            self.update_weight_metadata(font, font_path)
            self.update_style_flags(font, font_path)

            if scale != 1.0:
                self.scale_font(font, scale)
                logger.info(f"  Scaled glyphs and advances to {scale:.3g}x")

            if kern_mode in ("add-legacy-kern", "legacy-kern-only"):
                kern_pairs = self.extract_kern_pairs(font)
                if kern_pairs:
                    self.add_legacy_kern(font, kern_pairs)

                if kern_mode == "legacy-kern-only" and "GPOS" in font:
                    del font["GPOS"]

            if outline_mode == "apply":
                if self.simplify_outlines(font):
                    logger.info("  Simplified outlines (removed overlaps)")

                cleaned = self.clean_degenerate_contours(font)
                if cleaned:
                    logger.info(f"  Cleaned {cleaned} zero-area contour(s)")

                flattened = self.flatten_composites(font)
                if flattened:
                    logger.info(f"  Flattened {flattened} composite glyph(s)")

            # Add common Unicode space characters the font is missing, so Kobo
            # does not render a .notdef box where one is used. Always on,
            # independent of preset. Runs before hinting; the empty glyphs have
            # no outline, so the no-op instrumentation below skips them.
            added_spaces = self.add_missing_spaces(font)
            for codepoint, width in added_spaces:
                label = SPACE_GLYPH_NAMES.get(codepoint, "space")
                logger.info(f"  Added {label} (U+{codepoint:04X}), advance width {width}")

            # Add hyphen/dash characters the font is missing by cloning an
            # existing glyph's shape (e.g. the soft hyphen from the hyphen).
            # Runs before hinting so KF no-op instrumentation covers the clones.
            added_clones = self.add_missing_clones(font)
            for codepoint, source_name in added_clones:
                label = CLONE_GLYPH_NAMES.get(codepoint, "glyph")
                logger.info(f"  Added {label} (U+{codepoint:04X}) cloned from '{source_name}'")

            figure_dash_width = self.add_missing_figure_dash(font)
            if figure_dash_width is not None:
                logger.info(f"  Added figure dash (U+2012), advance width {figure_dash_width} (digit width)")

            # KF output strips hinting completely, then adds one uniform no-op
            # program to every outline glyph so iType avoids its auto-grid-fit.
            # Runs after outline processing so flattened glyphs are covered too.
            if self.prefix == "KF":
                stripped = self._strip_hinting_tables(font)
                if stripped:
                    logger.info(f"  Removed {stripped} hinting table(s)")
                instrumented = self._add_noop_hints(font)
                if instrumented:
                    logger.info(f"  Set no-op instructions on {instrumented} glyph(s) (fix iType wobble)")

            output_path = self._generate_output_path(font_path, metadata)
            font.save(output_path)
            logger.info(f"  Saved: {output_path}")

            if self.prefix != "KF" and outline_mode == "apply" and has_meaningful_hints:
                # simplify_outlines() rewrites glyphs and strips per-glyph
                # bytecode, while global hint tables can remain. Rehint the
                # saved final outlines when the source had real glyph hints.
                if not self.apply_ttfautohint(output_path):
                    return False

            if self.line_percent != 0:
                # Check if line spacing already matches target
                needs_adjustment = True
                if "OS/2" in font and "head" in font:
                    upm = font["head"].unitsPerEm
                    asc = font["OS/2"].sTypoAscender
                    desc = font["OS/2"].sTypoDescender
                    gap = font["OS/2"].sTypoLineGap
                    current_pct = round(((asc - desc + gap) / upm - 1) * 100)
                    needs_adjustment = current_pct != self.line_percent
                if needs_adjustment:
                    if not self.apply_line_adjustment(output_path):
                        return False

            if not self._validate_output_font(output_path):
                logger.error("  ots-sanitize validation failed.")
                return False

            return True
        except Exception as e:
            logger.error(f"  Processing failed: {e}")
            return False
    
    def _generate_output_path(self, original_path: str, metadata: FontMetadata) -> str:
        """
        Generate the output path for the processed font.
        This function now uses the centralized `STYLE_MAP` to ensure filename
        suffixes are consistent with the styles found in the font's internal metadata.
        """
        dirname = os.path.dirname(original_path)
        original_name, ext = os.path.splitext(os.path.basename(original_path))

        style_suffix = ""
        for key in STYLE_MAP:
            if key.lower() in original_name.lower():
                style_suffix = key
                break

        style_part = f"-{style_suffix}" if style_suffix else ""

        if self.prefix:
            base_name = f"{self.prefix}_{metadata.family_name.replace(' ', '_')}{style_part}"
        else:
            base_name = f"{metadata.family_name.replace(' ', '_')}{style_part}"

        out_ext = ".ttf" if ext.lower() == ".otf" else ext.lower()
        return os.path.join(dirname, f"{base_name}{out_ext}")


def otf_to_ttf(font: TTFont, max_err: float = 1.0) -> None:
    """Convert a CFF (OTF) font to TrueType (glyf) in place.

    Cubic Bézier outlines from the CFF table are approximated as quadratic
    curves via Cu2QuPen (max_err is in font units, 1.0 is imperceptible at
    reading sizes).  The CFF table is then removed, leaving a genuine TTF
    with no residual Name INDEX metadata.
    """
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.pens.cu2quPen import Cu2QuPen

    assert "CFF " in font, "Font does not have a CFF table"

    glyph_order = font.getGlyphOrder()
    glyph_set = font.getGlyphSet()

    glyphs = {}
    for name in glyph_order:
        tt_pen = TTGlyphPen(None)
        cu2qu_pen = Cu2QuPen(tt_pen, max_err=max_err, reverse_direction=True)
        glyph_set[name].draw(cu2qu_pen)
        glyphs[name] = tt_pen.glyph()

    glyf_table = newTable("glyf")
    glyf_table.glyphs = glyphs
    glyf_table.glyphOrder = glyph_order
    font["glyf"] = glyf_table
    font["loca"] = newTable("loca")

    font.sfntVersion = "\x00\x01\x00\x00"

    for tag in ("CFF ", "VORG"):
        if tag in font:
            del font[tag]

    # post v2.0 stores glyph names; CFF fonts typically ship v3.0 (no names),
    # so we have to seed extraNames/mapping when switching formats.
    if "post" in font:
        post = font["post"]
        post.formatType = 2.0
        if not hasattr(post, "extraNames"):
            post.extraNames = []
        if not hasattr(post, "mapping"):
            post.mapping = {}

    # TTF requires maxp v1.0 with hinting-related fields populated.  The
    # converted font has no hints, so zero is a safe value for everything
    # except the composite-depth/element counts (which fontTools recomputes
    # from the glyf table during save).
    if "maxp" in font:
        maxp = font["maxp"]
        maxp.tableVersion = 0x00010000
        maxp.maxZones = 1
        maxp.maxTwilightPoints = 0
        maxp.maxStorage = 0
        maxp.maxFunctionDefs = 0
        maxp.maxInstructionDefs = 0
        maxp.maxStackElements = 0
        maxp.maxSizeOfInstructions = 0
        maxp.maxComponentElements = 0
        maxp.maxComponentDepth = 0


def fontforge_otf_to_ttf(font_path: str) -> TTFont:
    """Convert an OTF/CFF font to a fully loaded TTF using FontForge.

    FontForge is intentionally used here instead of the in-process fontTools
    cubic-to-quadratic path: Kobo's renderer is sensitive to some generated TTF
    details, and FontForge's save path has proven more conservative for CFF OTF
    sources.
    """
    fontforge = shutil.which("fontforge")
    if fontforge is None:
        raise RuntimeError("fontforge is required for OTF/CFF inputs")

    script = (
        "import fontforge, sys; "
        "font = fontforge.open(sys.argv[1]); "
        "font.generate(sys.argv[2], flags=('opentype', 'round')); "
        "font.close()"
    )

    with tempfile.TemporaryDirectory(prefix="kobofix-otf-") as tmpdir:
        output_path = os.path.join(tmpdir, "converted.ttf")
        try:
            subprocess.run(
                [fontforge, "-lang=py", "-c", script, font_path, output_path],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            message = (e.stderr or str(e)).strip()
            raise RuntimeError(message) from e

        converted = TTFont(output_path)
        converted.ensureDecompiled()
        logger.info("  Converted OTF to TrueType with FontForge")
        return converted


def check_dependencies(
    require_font_line: bool = True,
    require_pathops: bool = True,
    require_ttfautohint: bool = True,
    require_fontforge: bool = False,
) -> None:
    """Check that all required external tools are available before processing."""
    missing = []
    if require_fontforge and shutil.which("fontforge") is None:
        missing.append("fontforge")
    if require_font_line and shutil.which("font-line") is None:
        missing.append("font-line")
    if require_ttfautohint and shutil.which("ttfautohint") is None:
        missing.append("ttfautohint")
    if FontProcessor._find_available_ots() is None:
        missing.append("ots-sanitize")
    if require_pathops:
        try:
            import pathops  # noqa: F401
        except ImportError:
            missing.append("skia-pathops (pip install skia-pathops)")
    if missing:
        logger.error(f"Missing required dependencies: {', '.join(missing)}")
        logger.error("Please install them before running this script.")
        sys.exit(1)


def validate_font_files(font_paths: List[str]) -> Tuple[List[str], List[str]]:
    """Validate font files for processing."""
    valid_files = []
    invalid_files = []
    
    for path in font_paths:
        if not os.path.isfile(path):
            logger.warning(f"File not found: {path}")
            continue
        if not path.lower().endswith((".ttf", ".otf")):
            logger.error(f"Unsupported file type: {path} (only .ttf and .otf files are supported)")
            invalid_files.append(os.path.basename(path))
            continue
        
        has_valid_suffix = any(
            key.lower() in os.path.basename(path).lower() for key in STYLE_MAP
        )
        
        if has_valid_suffix:
            valid_files.append(path)
        else:
            invalid_files.append(os.path.basename(path))
    
    return valid_files, invalid_files


def main():
    """Main entry point."""
    preset_names = ", ".join(PRESETS.keys())

    parser = argparse.ArgumentParser(
        description="Process fonts for Kobo e-readers: add prefix, kern table, "
                   "PANOSE validation, and line adjustments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Presets:
  nv    Prepare fonts for the ebook-fonts repository. Applies NV prefix,
        20%% line spacing. Does not modify kerning or outlines.
  kf    Prepare KF fonts from NV fonts. Applies KF prefix, replaces NV
        prefix, adds legacy kern table, simplifies outlines. No line spacing changes.

Examples:
  Using a preset:
  %(prog)s --preset nv *.ttf
  %(prog)s --preset kf *.ttf

  Custom processing:
  %(prog)s --prefix KF --name="Fonty" --line-percent 20 --kern add-legacy-kern --outline apply *.ttf
  %(prog)s --preset nv --name="Clara" --scale 1.08 *.otf

  If no preset or flags are provided, you will be prompted to choose a preset.
        """
    )

    parser.add_argument("fonts", nargs="+",
        help="Font files to process (*.ttf, *.otf). You can use a wildcard (glob).")
    parser.add_argument("--preset", type=str, choices=PRESETS.keys(),
        help=f"Use a preset configuration ({preset_names}).")
    parser.add_argument("--name", type=str,
        help="Optional new family name for all fonts. Other font metadata like copyright info is unaffected.")
    parser.add_argument("--prefix", type=str,
        help="Prefix to add to font names. Set to empty string to omit prefix.")
    parser.add_argument("--line-percent", type=int,
        help="Line spacing adjustment percentage. Set to 0 to make no changes to line spacing.")
    parser.add_argument("--scale", type=float,
        help="Scale glyph outlines, advances, and kerning inside the current UPM. "
             "Use 1.0 for no scaling, 1.08 for 108%%, etc.")
    parser.add_argument("--kern", type=str,
        choices=["add-legacy-kern", "legacy-kern-only", "skip"],
        help="Kerning mode: 'add-legacy-kern' extracts GPOS pairs into a legacy kern table, "
             "'legacy-kern-only' does the same but removes the GPOS table afterwards, "
             "'skip' leaves kerning untouched.")
    parser.add_argument("--dry-run", action="store_true",
        help="Report what would change without modifying any files.")
    parser.add_argument("--stamp", action="store_true",
        help="When using the KF prefix, append a dated kobo-font-fix patch note "
             "to the copyright (name ID 0). No effect for other prefixes.")
    parser.add_argument("--verbose", action="store_true",
        help="Enable verbose output.")
    parser.add_argument("--remove-prefix", type=str,
        help="Remove a leading prefix from font names before applying the new prefix. Only works if `--name` is not used. (e.g., --remove-prefix=\"NV\")")
    parser.add_argument("--outline", type=str,
        choices=["apply", "skip"],
        help="Outline mode: 'apply' removes overlaps and cleans degenerate contours (requires skia-pathops), "
             "'skip' leaves outlines untouched.")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Determine which flags were explicitly set by the user
    manual_flags = {k for k in ("prefix", "line_percent", "kern", "outline", "remove_prefix", "name", "scale")
                    if getattr(args, k) is not None}

    # If no preset and no manual flags, prompt the user to choose a preset
    if args.preset is None and not manual_flags:
        logger.info("No preset or flags specified. Available presets:")
        for name, values in PRESETS.items():
            logger.info(f"  {name}")
        choice = input("\nChoose a preset: ").strip().lower()
        if choice not in PRESETS:
            logger.error(f"Unknown preset '{choice}'. Available: {preset_names}")
            sys.exit(1)
        args.preset = choice

    # Apply preset values as defaults, then let explicit flags override
    if args.preset:
        preset = PRESETS[args.preset]
        for key, value in preset.items():
            if key not in manual_flags:
                setattr(args, key, value)

    # Fill in remaining defaults for any unset flags
    if args.prefix is None:
        parser.error("--prefix is required when not using a preset.")
    if args.line_percent is None:
        parser.error("--line-percent is required when not using a preset.")
    if args.kern is None:
        args.kern = "skip"
    if args.outline is None:
        args.outline = "apply"
    if args.scale is None:
        args.scale = 1.0
    if args.scale <= 0:
        parser.error("--scale must be greater than zero.")

    if args.name and args.remove_prefix:
        logger.warning("--name and --remove-prefix were both specified. --name takes precedence; --remove-prefix will be ignored.")
        args.remove_prefix = None

    check_dependencies(
        require_font_line=args.line_percent != 0,
        require_pathops=args.outline == "apply",
        require_ttfautohint=args.prefix != "KF" and args.outline == "apply",
        require_fontforge=any(path.lower().endswith(".otf") for path in args.fonts),
    )

    valid_files, invalid_files = validate_font_files(args.fonts)

    if invalid_files:
        logger.error("\nERROR: The following fonts have invalid filenames:")
        logger.error(f"(Must contain one of the following: {', '.join(STYLE_MAP.keys())})")
        for filename in invalid_files:
            logger.error(f"  {filename}")

        if not valid_files:
            sys.exit(1)

        response = input("\nContinue with valid files only? [y/N]: ")
        if response.lower() != 'y':
            sys.exit(1)

    if not valid_files:
        logger.error("No valid font files to process.")
        sys.exit(1)

    processor = FontProcessor(
        prefix=args.prefix,
        line_percent=args.line_percent,
    )

    success_count = 0
    for font_path in valid_files:
        if processor.process_font(
            args.kern,
            font_path,
            args.name,
            args.remove_prefix,
            args.dry_run,
            args.outline,
            args.stamp,
            args.scale,
        ):
            success_count += 1

    logger.info(f"\n{'='*50}")
    if args.dry_run:
        logger.info(f"Checked {success_count}/{len(valid_files)} fonts.")
    else:
        logger.info(f"Processed {success_count}/{len(valid_files)} fonts successfully.")

    if success_count < len(valid_files):
        sys.exit(1)

if __name__ == "__main__":
    main()
