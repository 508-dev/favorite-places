from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gmaps_scraper.debug_dump import write_debug_dump, write_place_debug_dump
from gmaps_scraper.models import PlaceDetails, PlaceExtractionDiagnostics

_LIST_URL = (
    "https://www.google.com/maps/@35.6501307,139.6868459,15z/"
    "data=!4m3!11m2!2sTESTLISTABC123456789!3e3"
)
_LIST_NODE = [
    ["TESTLISTABC123456789", 1, None, 1, 1],
    4,
    "https://www.google.com/maps/placelists/list/TESTLISTABC123456789",
    "Owner",
    "Sample Coffee Stops",
    "Curated fixture data for parser tests",
    None,
    None,
    [
        [
            None,
            [
                None,
                None,
                "",
                None,
                "Example District",
                [None, None, 35.6501307, 139.6868459],
                ["7451636382641713350", "aux"],
                "/g/11northwind",
                "Fixture note: order the sampler",
            ],
            "Northwind Cafe",
        ]
    ],
]


class DebugDumpTests(unittest.TestCase):
    def test_writes_summary_candidates_and_place_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            summary_path = write_debug_dump(
                _LIST_URL,
                runtime_state=["noise", _LIST_NODE],
                script_texts=[],
                html="<html></html>",
                output_dir=Path(tmp_dir),
            )

            summary = json.loads(summary_path.read_text(encoding="utf-8"))

            self.assertEqual(summary["list_id"], "TESTLISTABC123456789")
            self.assertGreaterEqual(summary["candidate_count"], 1)
            self.assertEqual(len(summary["places"]), 1)

            candidate_path = Path(tmp_dir) / summary["candidates"][0]["file"]
            place_summary_path = Path(tmp_dir) / summary["places"][0]["summary_file"]

            self.assertTrue(candidate_path.exists())
            self.assertTrue(place_summary_path.exists())

            place_summary = json.loads(place_summary_path.read_text(encoding="utf-8"))
            self.assertEqual(place_summary["name"], "Northwind Cafe")
            self.assertIn("Fixture note: order the sampler", place_summary["strings"])

    def test_writes_place_debug_dump_with_runtime_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            details = PlaceDetails(
                source_url="https://www.google.com/maps/place/Fiamma",
                resolved_url="https://www.google.com/maps/place/Fiamma/@1.2492482,103.8244513,17z",
                name="Fiamma",
                category="イタリア料理店",
                category_display_en="Italian restaurant",
                category_display_en_source="translation_memory",
                category_display_en_confidence="high",
                rating=4.8,
                review_count=832,
                address="1 The Knolls, Singapore 098297",
            )
            diagnostics = PlaceExtractionDiagnostics(
                field_sources={"name": "dom", "category_display_en": "translation_memory"},
                confidence=1.0,
                evidence_hash="fixture",
            )
            details.diagnostics = diagnostics

            summary_path = write_place_debug_dump(
                details.source_url,
                resolved_url=details.resolved_url,
                snapshot={"dom": {"name": "Fiamma"}, "preview": {}},
                merged_snapshot={"name": "Fiamma"},
                details=details,
                evidence={"text_lines": ["Fiamma"], "review_topic_candidates": []},
                diagnostics=diagnostics,
                output_dir=Path(tmp_dir),
            )

            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["place"]["category_display_en"], "Italian restaurant")
            self.assertEqual(
                summary["artifact_files"],
                {
                    "place_output": "place-output.json",
                    "diagnostics": "diagnostics.json",
                    "llm_evidence": "llm-evidence.json",
                    "snapshot": "artifacts/snapshot.json",
                    "merged_snapshot": "artifacts/merged-snapshot.json",
                },
            )
            for file in summary["artifact_files"].values():
                self.assertTrue((Path(tmp_dir) / file).exists())
            self.assertFalse((Path(tmp_dir) / "selector-recipe.json").exists())
