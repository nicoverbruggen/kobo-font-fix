#!/usr/bin/env python3
"""
Font processing utility for Kobo e-readers.

This script processes TrueType fonts to improve compatibility with
Kobo e-readers by:
- Adding a custom prefix to font names
- Updating the font name if necessary, including PS name
- Extracting GPOS kerning data and creating legacy 'kern' tables
- Validating and correcting PANOSE metadata
- Adjusting font metrics for better line spacing
- Updating font weight metadata (OS/2 usWeightClass and PostScript weight string)

Requirements:
- fontTools (pip install fonttools)
- font-line utility (https://github.com/source-foundry/font-line)
"""

import sys
import os
import subprocess
import argparse
import logging
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Tuple, Optional, List

from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._k_e_r_n import KernTable_format_0

# -------------
# DEFAULTS
# -------------
#
DEFAULT_PREFIX = "KF"
DEFAULT_LINE_PERCENT = 20

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

# -------------
# SUPPORTED EXTENSIONS
# -------------
# Sadly, due to issues, OTF files are not supported.
# Not a big loss, given that Kobo plays well with TTF files,
# not so much with OTF files that have been modified by this
# slightly bananas script.
#
SUPPORTED_EXTENSIONS = (".ttf")

# Configure logging for clear output
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


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
        prefix: str = DEFAULT_PREFIX,
        line_percent: int = DEFAULT_LINE_PERCENT
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
                elif name_record.platformID == 3:  # Windows
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

        ps_name = f"{self.prefix}_{family_name.replace(' ', '-')}"
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
        """
        kern_value = 0
        if value1 is not None:
            kern_value += getattr(value1, "XAdvance", 0) or 0
        if value2 is not None:
            kern_value += getattr(value2, "XAdvance", 0) or 0

        if kern_value == 0:
            if value1 is not None:
                kern_value += getattr(value1, "XPlacement", 0) or 0
            if value2 is not None:
                kern_value += getattr(value2, "XPlacement", 0) or 0

        return int(kern_value)

    def _extract_format1_pairs(self, subtable) -> Dict[Tuple[str, str], int]:
        """Extract kerning pairs from PairPos Format 1 (per-glyph PairSets)."""
        pairs = defaultdict(int)
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
                    pairs[(left_glyph, right_glyph)] += kern_value
        return pairs

    def _extract_format2_pairs(self, subtable) -> Dict[Tuple[str, str], int]:
        """Extract kerning pairs from PairPos Format 2 (class-based)."""
        pairs = defaultdict(int)
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
                        pairs[(left, right)] += kern_value
        return pairs

    def extract_kern_pairs(self, font: TTFont) -> Dict[Tuple[str, str], int]:
        """
        Extract all kerning pairs from GPOS PairPos lookups.
        GPOS (Glyph Positioning) is the modern standard for kerning in OpenType fonts.
        This function iterates through the GPOS tables to find all kerning pairs
        before we convert them to the legacy 'kern' table format.
        """
        pairs = defaultdict(int)
        if "GPOS" in font:
            gpos = font["GPOS"].table
            lookup_list = getattr(gpos, "LookupList", None)
            if lookup_list and lookup_list.Lookup:
                for lookup in lookup_list.Lookup:
                    # Only process Pair Adjustment lookups (type 2)
                    if getattr(lookup, "LookupType", None) == 2:
                        for subtable in getattr(lookup, "SubTable", []):
                            fmt = getattr(subtable, "Format", None)
                            if fmt == 1:
                                format1_pairs = self._extract_format1_pairs(subtable)
                                for key, value in format1_pairs.items():
                                    pairs[key] += value
                            elif fmt == 2:
                                format2_pairs = self._extract_format2_pairs(subtable)
                                for key, value in format2_pairs.items():
                                    pairs[key] += value
        return dict(pairs)

    @staticmethod
    def add_legacy_kern(font: TTFont, kern_pairs: Dict[Tuple[str, str], int]) -> int:
        """
        Create or replace a legacy 'kern' table with the supplied pairs.
        Splits into multiple subtables if there are more than 10,000 pairs.
        """
        if not kern_pairs:
            return 0

        kern_table = newTable("kern")
        kern_table.version = 0
        kern_table.kernTables = []

        # Max pairs per subtable
        MAX_PAIRS = 10000
        items = [(tuple(k), int(v)) for k, v in kern_pairs.items() if v]

        for i in range(0, len(items), MAX_PAIRS):
            chunk = dict(items[i:i + MAX_PAIRS])
            subtable = KernTable_format_0()
            subtable.version = 0
            subtable.length = None
            subtable.coverage = 1
            subtable.kernTable = chunk
            kern_table.kernTables.append(subtable)

        font["kern"] = kern_table

        return len(items)

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
        else:
            logger.info("  Renaming the font to: " + f"{self.prefix} {metadata.full_name}")

        # Update Family Name
        self._set_name_records(font, 1, f"{self.prefix} {metadata.family_name}")
        # Update Subfamily
        self._set_name_records(font, 2, metadata.style_name)
        # Update Full Name
        self._set_name_records(font, 4, f"{self.prefix} {metadata.full_name}")

        # Update Typographic Family
        self._set_name_records(font, 16, f"{self.prefix} {metadata.family_name}")
        # Update Preferred Subfamily
        self._set_name_records(font, 17, metadata.style_name)
        # Update Preferred Family
        self._set_name_records(font, 18, f"{self.prefix} {metadata.family_name}")

        # Update Unique ID (ID 3)
        try:
            current_unique = font["name"].getName(3, 3, 1).toUnicode()
            parts = current_unique.split("Version")
            version_info = f"Version{parts[1]}" if len(parts) == 2 else "Version 1.000"
            new_unique_id = f"{self.prefix} {metadata.family_name.strip()}:{version_info}"
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

            name_mapping = {
                "FullName": f"{self.prefix} {metadata.full_name}",
                "FamilyName": f"{self.prefix} {metadata.family_name.replace(' ', '_')}"
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
        expected = style_specs.get(style_name)
        if not expected:
            logger.warning(f"  No PANOSE specification for style '{style_name}'; skipping.")
            return

        changes = []
        if panose.bWeight != expected["weight"]:
            panose.bWeight = expected["weight"]
            changes.append(f"bWeight {panose.bWeight}->{expected['weight']}")

        if panose.bLetterForm != expected["letterform"]:
            panose.bLetterForm = expected["letterform"]
            changes.append(f"bLetterForm {panose.bLetterForm}->{expected['letterform']}")

        if changes:
            logger.info(f"  PANOSE corrected: {', '.join(changes)}")
        else:
            logger.info("  PANOSE check passed, no modifications required.")

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
            if subprocess.run(["which", "font-line"], capture_output=True).returncode != 0:
                logger.error("  font-line utility not found. Please install it first.")
                return False

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

    def process_font(self, 
        kern: bool, 
        remove_gpos: bool, 
        font_path: str, 
        new_name: Optional[str] = None,
        remove_prefix: Optional[str] = None,
        skip_renaming: bool = False,
    ) -> bool:
        """
        Process a single font file.
        This function orchestrates the entire process, calling the various
        helper methods in the correct order.
        """
        logger.info(f"\nProcessing: {font_path}")

        try:
            font = TTFont(font_path)
        except Exception as e:
            logger.error(f"  Failed to open font: {e}")
            return False

        # Remove WWS family names (IDs 21 and 22) to prevent confusion when determining best family name
        if font["name"]:
            old_names_list = font["name"].names
            names_to_remove = [21, 22]
            new_names_list = [n for n in old_names_list if n.nameID not in names_to_remove]
            if len(new_names_list) < len(old_names_list):
                font["name"].names = new_names_list
                logger.info("  Removed WWS Family Name (ID 21) and WWS Subfamily Name (ID 22).")

        # Determine the effective font name, checking for `--remove-prefix` first
        effective_name = new_name
        if new_name is None:
            # If no --name argument is provided, get the font's best family name
            current_family_name = font["name"].getBestFamilyName()
            # If --remove-prefix is used and the name starts with the specified prefix, remove it
            if remove_prefix and current_family_name.startswith(remove_prefix + " "):
                effective_name = current_family_name[len(remove_prefix + " "):]
                logger.info(f"  --remove-prefix enabled: using '{effective_name}' as the new family name.")

        metadata = self._get_font_metadata(font, font_path, effective_name)
        if not metadata:
            return False

        try:
            if not skip_renaming:
                self.rename_font(font, metadata)
            else:
                logger.info("  Skipping font renaming as requested.")
            self.check_and_fix_panose(font, font_path)
            self.update_weight_metadata(font, font_path)

            if kern:
                kern_pairs = self.extract_kern_pairs(font)
                if kern_pairs:
                    written = self.add_legacy_kern(font, kern_pairs)
                    logger.info(f"  Kerning: extracted {len(kern_pairs)} pairs; wrote {written} to legacy 'kern' table.")
                else:
                    logger.info("  Kerning: no GPOS kerning found.")
            else:
                logger.info("  Skipping `kern` step.")

            # The GPOS table is removed after the kerning data has been extracted
            # and written to the `kern` table. This ensures the information is not lost.
            if remove_gpos and kern and "GPOS" in font:
                del font["GPOS"]
                logger.info("  Removed GPOS table from the font.")

            output_path = self._generate_output_path(font_path, metadata)
            font.save(output_path)
            logger.info(f"  Saved: {output_path}")

            if self.line_percent != 0:
                self.apply_line_adjustment(output_path)
            else:
                logger.info("  Skipping line adjustment step.")
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

        base_name = f"{self.prefix}_{metadata.family_name.replace(' ', '_')}{style_part}"

        return os.path.join(dirname, f"{base_name}{ext.lower()}")


def validate_font_files(font_paths: List[str]) -> Tuple[List[str], List[str]]:
    """Validate font files for processing."""
    valid_files = []
    invalid_files = []

    for path in font_paths:
        if not os.path.isfile(path):
            logger.warning(f"File not found: {path}")
            continue
        if not path.lower().endswith(SUPPORTED_EXTENSIONS):
            logger.warning(f"Unsupported file type: {path}")
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
    parser = argparse.ArgumentParser(
        description="Process fonts for Kobo e-readers: add prefix, kern table, "
                   "PANOSE validation, and line adjustments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  For a default experience, which will prefix the font with KF, add `kern` table and adjust line-height:
  %(prog)s *.ttf

  If you want to rename the font:
  %(prog)s --prefix KF --name="Fonty" --line-percent 20 *.ttf

  If you want to keep the line-height because for a given font the default was fine:
  %(prog)s --line-percent 0 *.ttf

  For improved legacy support, you can remove the GPOS table (not recommended):
  %(prog)s --prefix KF --name="Fonty" --line-percent 20 --remove-gpos *.ttf

  To remove a specific prefix, like "NV", before applying a new one:
  %(prog)s --prefix KF --remove-prefix="NV" *.ttf
        """
    )

    parser.add_argument("fonts", nargs="+",
        help="Font files to process (*.ttf). You can use a wildcard (glob).")
    parser.add_argument("--name", type=str,
        help="Optional new family name for all fonts. Other font metadata like copyright info is unaffected.")
    parser.add_argument("--prefix", type=str, default=DEFAULT_PREFIX,
        help=f"Prefix to add to font names. Required. (Default: {DEFAULT_PREFIX})")
    parser.add_argument("--line-percent", type=int, default=DEFAULT_LINE_PERCENT,
        help=f"Line spacing adjustment percentage. Set to 0 to make no changes to line spacing. (Default: {DEFAULT_LINE_PERCENT})")
    parser.add_argument("--skip-kobo-kern", action="store_true",
        help="Skip the creation of the legacy 'kern' table from GPOS data.")
    parser.add_argument("--remove-gpos", action="store_true",
        help="Remove the GPOS table after converting kerning to a 'kern' table. Does not work if `--skip-kobo-kern` is set.")
    parser.add_argument("--verbose", action="store_true",
        help="Enable verbose output.")
    parser.add_argument("--remove-prefix", type=str,
        help="Remove a leading prefix from font names before applying the new prefix. Only works if `--name` is not used. (e.g., --remove-prefix=\"NV\")")
    parser.add_argument("--skip-renaming", action="store_true", help="Skip the font renaming process.")


    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

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
            not args.skip_kobo_kern,
            args.remove_gpos,
            font_path, 
            args.name,
            args.remove_prefix,
            skip_renaming=args.skip_renaming,
        ):
            success_count += 1

    logger.info(f"\n{'='*50}")
    logger.info(f"Processed {success_count}/{len(valid_files)} fonts successfully.")

    if success_count < len(valid_files):
        sys.exit(1)

if __name__ == "__main__":
    main()
