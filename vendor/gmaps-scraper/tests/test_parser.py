from __future__ import annotations

import copy
import json
import unittest

from gmaps_scraper.parser import ParseError, parse_saved_list_artifacts

_LIST_URL = (
    "https://www.google.com/maps/@35.6501307,139.6868459,15z/"
    "data=!4m3!11m2!2sTESTLISTABC123456789!3e3"
)
_SHORT_URL = "https://maps.app.goo.gl/TestSavedListShortUrl"
_REDIRECT_URL = (
    "https://www.google.com/maps/@30.5370705,125.4120472,6z/"
    "data=!4m3!11m2!2sTESTLISTABC123456789!3e3?entry=ttu"
)
_NORTHWIND_CELL_ID = "7451636382641713350"
_NORTHWIND_CID = "9055794338847426964"
_HARBOR_CELL_ID = "1234567890123456789"
_HARBOR_CID = "5678901234567890123"
_LIST_NODE = [
    ["TESTLISTABC123456789", 1, None, 1, 1],
    4,
    "https://www.google.com/maps/placelists/list/TESTLISTABC123456789",
    [
        "Fixture Owner",
        "https://lh3.googleusercontent.com/a-/fixture-owner",
        "104356373423434804635",
    ],
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
                [_NORTHWIND_CELL_ID, _NORTHWIND_CID],
                "/g/11northwind",
            ],
            "Northwind Cafe",
            "Try the seasonal sampler.",
            None,
            None,
            None,
            [[[[3, None, "104356373423434804635", "❤️", [1776133481, 81561000]]]]],
            [[1], [_NORTHWIND_CELL_ID, _NORTHWIND_CID]],
            [1776063335, 302383000],
            [1776132745, 850748000],
            None,
            [
                "Fixture Owner",
                "https://lh3.googleusercontent.com/a-/fixture-owner",
                "104356373423434804635",
            ],
        ],
        [
            None,
            [
                None,
                None,
                "",
                None,
                "Market Square",
                [None, None, 35.6915776, 139.7836109],
                [_HARBOR_CELL_ID, _HARBOR_CID],
                "/g/11harborbakery",
            ],
            "Harbor Bakery",
            None,
            None,
            None,
            None,
            [],
            [[1], [_HARBOR_CELL_ID, _HARBOR_CID]],
            [1776063335, 302383000],
            [1776132745, 850748000],
            None,
            [
                "Fixture Collaborator",
                "https://lh3.googleusercontent.com/a-/fixture-collaborator",
                "205678901234567890123",
            ],
        ],
    ],
]


class ParserTests(unittest.TestCase):
    def test_parses_runtime_state_with_list_id(self) -> None:
        runtime_state = ["noise", _LIST_NODE]

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(parsed.list_id, "TESTLISTABC123456789")
        self.assertEqual(parsed.title, "Sample Coffee Stops")
        self.assertEqual(parsed.description, "Curated fixture data for parser tests")
        self.assertEqual(
            parsed.owner.to_dict() if parsed.owner else None,
            {
                "name": "Fixture Owner",
                "photo_url": "https://lh3.googleusercontent.com/a-/fixture-owner",
                "profile_id": "104356373423434804635",
            },
        )
        self.assertEqual(
            [owner.to_dict() for owner in parsed.collaborators],
            [
                {
                    "name": "Fixture Collaborator",
                    "photo_url": "https://lh3.googleusercontent.com/a-/fixture-collaborator",
                    "profile_id": "205678901234567890123",
                }
            ],
        )
        self.assertEqual(len(parsed.places), 2)
        self.assertEqual(parsed.places[0].name, "Northwind Cafe")
        self.assertEqual(
            parsed.places[0].note,
            "Try the seasonal sampler.",
        )
        self.assertEqual(
            parsed.places[0].added_by.to_dict() if parsed.places[0].added_by else None,
            {
                "name": "Fixture Owner",
                "photo_url": "https://lh3.googleusercontent.com/a-/fixture-owner",
                "profile_id": "104356373423434804635",
            },
        )
        self.assertTrue(parsed.places[0].is_favorite)
        self.assertEqual(
            parsed.places[1].added_by.to_dict() if parsed.places[1].added_by else None,
            {
                "name": "Fixture Collaborator",
                "photo_url": "https://lh3.googleusercontent.com/a-/fixture-collaborator",
                "profile_id": "205678901234567890123",
            },
        )
        self.assertFalse(parsed.places[1].is_favorite)

    def test_keeps_header_owner_first_when_collecting_collaborators(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        list_node = runtime_state[1]
        assert isinstance(list_node, list)
        list_node.append(
            [
                [
                    "Late Collaborator",
                    "https://lh3.googleusercontent.com/a-/late-collaborator",
                    "305678901234567890123",
                ]
            ]
        )

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(
            parsed.owner.to_dict() if parsed.owner else None,
            {
                "name": "Fixture Owner",
                "photo_url": "https://lh3.googleusercontent.com/a-/fixture-owner",
                "profile_id": "104356373423434804635",
            },
        )
        self.assertEqual(
            [owner.to_dict() for owner in parsed.collaborators],
            [
                {
                    "name": "Late Collaborator",
                    "photo_url": "https://lh3.googleusercontent.com/a-/late-collaborator",
                    "profile_id": "305678901234567890123",
                },
                {
                    "name": "Fixture Collaborator",
                    "photo_url": "https://lh3.googleusercontent.com/a-/fixture-collaborator",
                    "profile_id": "205678901234567890123",
                },
            ],
        )
        self.assertEqual(parsed.places[0].cid, _NORTHWIND_CID)
        self.assertEqual(
            parsed.places[0].maps_url,
            "https://www.google.com/maps/search/?api=1&query=Northwind+Cafe%2C+Example+District",
        )

    def test_falls_back_to_placelist_marker_without_list_id(self) -> None:
        runtime_state = ["noise", _LIST_NODE]

        parsed = parse_saved_list_artifacts(
            "https://www.google.com/maps",
            runtime_state=runtime_state,
        )

        self.assertEqual(parsed.list_id, "TESTLISTABC123456789")
        self.assertEqual(parsed.title, "Sample Coffee Stops")
        self.assertEqual(parsed.places[1].name, "Harbor Bakery")
        self.assertFalse(parsed.places[1].is_favorite)

    def test_prefers_list_id_from_resolved_redirect_url(self) -> None:
        runtime_state = ["noise", _LIST_NODE]

        parsed = parse_saved_list_artifacts(
            _SHORT_URL,
            resolved_url=_REDIRECT_URL,
            runtime_state=runtime_state,
        )

        self.assertEqual(parsed.source_url, _SHORT_URL)
        self.assertEqual(parsed.resolved_url, _REDIRECT_URL)
        self.assertEqual(parsed.list_id, "TESTLISTABC123456789")
        self.assertEqual(parsed.title, "Sample Coffee Stops")

    def test_decodes_embedded_xssi_blob(self) -> None:
        blob = ")]}'\\n" + json.dumps(_LIST_NODE)

        parsed = parse_saved_list_artifacts(_LIST_URL, script_texts=[blob])

        self.assertEqual(parsed.resolved_url, None)
        self.assertEqual(parsed.title, "Sample Coffee Stops")
        self.assertEqual(len(parsed.places), 2)

    def test_decodes_app_initialization_state_assignment(self) -> None:
        blob = f"window.APP_INITIALIZATION_STATE={json.dumps(_LIST_NODE)};"

        parsed = parse_saved_list_artifacts(_LIST_URL, script_texts=[blob])

        self.assertEqual(parsed.list_id, "TESTLISTABC123456789")
        self.assertEqual(parsed.title, "Sample Coffee Stops")

    def test_builds_search_query_url_when_cid_is_missing(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        place_metadata = runtime_state[1][8][0][1]
        assert isinstance(place_metadata, list)
        place_metadata[6] = [None]

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(parsed.places[0].cid, None)
        self.assertEqual(parsed.places[0].google_id, "/g/11northwind")
        self.assertEqual(
            parsed.places[0].maps_url,
            "https://www.google.com/maps/search/?api=1&query=Northwind+Cafe%2C+Example+District",
        )

    def test_uses_fingerprint_not_positive_s2_cell_from_slot_6(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(parsed.places[0].cid, _NORTHWIND_CID)
        self.assertNotEqual(parsed.places[0].cid, _NORTHWIND_CELL_ID)

    def test_normalizes_negative_slot_6_fingerprint(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        place_metadata = runtime_state[1][8][0][1]
        assert isinstance(place_metadata, list)
        place_metadata[6] = ["3765761194353288769", "-782808945063765017"]

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(parsed.places[0].cid, "17663935128645786599")

    def test_ignores_timestamp_like_fallback_values_when_cid_is_missing(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        place_metadata = runtime_state[1][8][0][1]
        assert isinstance(place_metadata, list)
        place_metadata[6] = [None]
        place_metadata.append(1776063335)

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(parsed.places[0].cid, None)

    def test_builds_coordinate_query_url_only_when_no_name_or_address_exist(self) -> None:
        runtime_state = [
            "noise",
            [
                ["LIST123", 1, None, 1, 1],
                4,
                "https://www.google.com/maps/placelists/list/LIST123",
                "Owner",
                "Untitled",
                None,
                None,
                None,
                [
                    [
                        None,
                        [
                            None,
                            None,
                            None,
                            None,
                            None,
                            [None, None, 35.6501307, 139.6868459],
                            [None],
                            None,
                        ],
                        None,
                        None,
                    ]
                ],
            ],
        ]

        parsed = parse_saved_list_artifacts(
            "https://www.google.com/maps/@0,0,3z/data=!4m3!11m2!2sLIST123!3e3",
            runtime_state=runtime_state,
        )

        self.assertEqual(len(parsed.places), 1)
        self.assertEqual(
            parsed.places[0].maps_url,
            "https://www.google.com/maps/search/?api=1&query=35.6501307%2C139.6868459",
        )

    def test_dedupes_places_with_same_cid(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        duplicate_place = copy.deepcopy(runtime_state[1][8][0])
        runtime_state[1][8].append(duplicate_place)

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(len(parsed.places), 2)
        self.assertEqual(parsed.places[0].cid, _NORTHWIND_CID)
        self.assertEqual(parsed.places[1].cid, _HARBOR_CID)

    def test_keeps_distinct_places_that_share_a_cid(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        second_place = runtime_state[1][8][1]
        assert isinstance(second_place, list)
        second_metadata = second_place[1]
        assert isinstance(second_metadata, list)

        second_metadata[5] = [None, None, 35.7000000, 139.7800000]
        second_metadata[6] = ["7451636382641713350", _NORTHWIND_CID]
        second_metadata[7] = None

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(len(parsed.places), 2)
        self.assertEqual(parsed.places[0].cid, _NORTHWIND_CID)
        self.assertEqual(parsed.places[1].cid, _NORTHWIND_CID)
        self.assertEqual(parsed.places[1].lat, 35.7)

    def test_does_not_use_owner_profile_id_as_place_cid(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        first_place = runtime_state[1][8][0]
        second_place = runtime_state[1][8][1]
        assert isinstance(first_place, list)
        assert isinstance(second_place, list)

        first_metadata = first_place[1]
        second_metadata = second_place[1]
        assert isinstance(first_metadata, list)
        assert isinstance(second_metadata, list)

        first_metadata[6] = ["-7451636382641713350", "-8451636382641713350"]
        second_metadata[6] = ["-1234567890123456789", "-2234567890123456789"]
        first_place.append(
            ["Owner", "https://example.com/avatar.jpg", "104356373423434804635"]
        )
        second_place.append(
            ["Owner", "https://example.com/avatar.jpg", "104356373423434804635"]
        )

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(len(parsed.places), 2)
        self.assertEqual(parsed.places[0].cid, "9995107691067838266")
        self.assertEqual(parsed.places[1].cid, "16212176183586094827")
        self.assertNotEqual(parsed.places[0].cid, "104356373423434804635")
        self.assertNotEqual(parsed.places[1].cid, "104356373423434804635")
        self.assertEqual(
            parsed.places[0].maps_url,
            "https://www.google.com/maps/search/?api=1&query=Northwind+Cafe%2C+Example+District",
        )

    def test_does_not_use_owner_payload_inside_metadata_as_place_cid(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        first_place = runtime_state[1][8][0]
        assert isinstance(first_place, list)
        first_metadata = first_place[1]
        assert isinstance(first_metadata, list)

        first_metadata[6] = [
            "Fixture Owner",
            "https://lh3.googleusercontent.com/a-/fixture-owner",
            "104356373423434804635",
        ]

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(len(parsed.places), 2)
        self.assertEqual(parsed.places[0].cid, None)
        self.assertEqual(parsed.places[0].google_id, "/g/11northwind")
        self.assertNotEqual(parsed.places[0].cid, "104356373423434804635")

    def test_extracts_favorite_and_note_from_user_payload_shape(self) -> None:
        runtime_state = [
            "noise",
            [
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
                            (
                                "100 Example Ave, Suite 1"
                            ),
                            [None, None, 35.6426886, 139.6988208],
                            ["6924437575605096209", "-782808945063765017"],
                            "/g/1pty5xgj1",
                        ],
                        "Fixture Bistro",
                        "Try the tasting menu.",
                        None,
                        None,
                        None,
                        [[[[3, None, "104356373423434804635", "❤️", [1776133481, 81561000]]]]],
                        [[1], ["6924437575605096209", "-782808945063765017"]],
                        [1776063335, 302383000],
                        [1776132745, 850748000],
                        None,
                    ]
                ],
            ],
        ]

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(len(parsed.places), 1)
        self.assertEqual(parsed.places[0].name, "Fixture Bistro")
        self.assertEqual(
            parsed.places[0].note,
            "Try the tasting menu.",
        )
        self.assertTrue(parsed.places[0].is_favorite)

    def test_extracts_favorite_when_place_name_is_missing(self) -> None:
        runtime_state = [
            "noise",
            [
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
                            "123 Central Plaza",
                            [None, None, 35.6426886, 139.6988208],
                            ["6924437575605096209", "-782808945063765017"],
                            "/g/1pty5xgj1",
                        ],
                        None,
                        None,
                        None,
                        None,
                        None,
                        [[[[3, None, "104356373423434804635", "❤️", [1776133481, 81561000]]]]],
                    ]
                ],
            ],
        ]

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(len(parsed.places), 1)
        self.assertEqual(parsed.places[0].address, "123 Central Plaza")
        self.assertTrue(parsed.places[0].is_favorite)

    def test_does_not_treat_heart_in_note_as_favorite(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        first_place = runtime_state[1][8][0]
        assert isinstance(first_place, list)

        first_place[3] = "Loved it ❤️"
        first_place[7] = []

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(parsed.places[0].note, "Loved it ❤️")
        self.assertFalse(parsed.places[0].is_favorite)

    def test_extracts_favorite_marker_from_non_primary_place_record_slot(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        first_place = runtime_state[1][8][0]
        assert isinstance(first_place, list)

        first_place[7] = []
        first_place.append([[[[3, None, "104356373423434804635", "❤️", [1776133481, 81561000]]]]])

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertTrue(parsed.places[0].is_favorite)

    def test_preserves_note_with_inline_url(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        first_place = runtime_state[1][8][0]
        assert isinstance(first_place, list)

        first_place[3] = "Try this menu: https://example.com/menu"

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(parsed.places[0].note, "Try this menu: https://example.com/menu")

    def test_prefers_enclosing_place_name_over_metadata_string(self) -> None:
        runtime_state = [
            "noise",
            [
                ["TESTLISTABC123456789", 1, None, 1, 1],
                4,
                "https://www.google.com/maps/placelists/list/TESTLISTABC123456789",
                "Owner",
                "Sample Dumplings",
                None,
                None,
                None,
                [
                    [
                        None,
                        [
                            None,
                            None,
                            (
                                "100 Example Ave Sample Dumpling House Suite 1"
                            ),
                            None,
                            "100 Example Ave, Suite 1",
                            [None, None, 25.0417836, 121.5479306],
                            ["3765761194353288769", "4471733103496006465"],
                            "/g/1tlcywsb",
                        ],
                        "Sample Dumpling House",
                        "",
                        None,
                        None,
                        None,
                        [],
                        [["3765761194353288769", "4471733103496006465"]],
                    ]
                ],
            ],
        ]

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(len(parsed.places), 1)
        self.assertEqual(parsed.places[0].name, "Sample Dumpling House")
        self.assertEqual(
            parsed.places[0].address,
            "100 Example Ave, Suite 1",
        )

    def test_does_not_promote_list_title_into_place_name(self) -> None:
        runtime_state = [
            "noise",
            [
                ["LIST123", 1, None, 1, 1],
                4,
                "https://www.google.com/maps/placelists/list/LIST123",
                "Owner",
                (
                    "Coastal weekend stops. See https://example.com/field-notes "
                    "for more, or follow @sampleguide for updates"
                ),
                None,
                None,
                None,
                [
                    [
                        None,
                        [
                            None,
                            None,
                            None,
                            None,
                            "Harbor Studio",
                            [None, None, -41.157461399999995, 146.1758303],
                            ["104356373423434804635"],
                            None,
                        ],
                        None,
                        None,
                    ]
                ],
            ],
        ]

        parsed = parse_saved_list_artifacts(
            "https://www.google.com/maps/@0,0,3z/data=!4m3!11m2!2sLIST123!3e3",
            runtime_state=runtime_state,
        )

        self.assertEqual(len(parsed.places), 1)
        self.assertEqual(parsed.places[0].name, "Harbor Studio")
        self.assertEqual(parsed.places[0].address, None)
        self.assertEqual(parsed.places[0].cid, "104356373423434804635")

    def test_uses_structured_place_record_for_sparse_real_payload_shape(self) -> None:
        owner = [
            "List Owner",
            (
                "https://lh3.googleusercontent.com/a-/ALV-UjW_i8-Eyr6conUhZ6tzGGlFe76mQTGeURI9N"
                "KDlca0FzlN0GY0Kjg"
            ),
            "104356373423434804635",
        ]
        runtime_state = [
            "noise",
            [
                ["LIST123", 1, None, 1, 1],
                4,
                "https://www.google.com/maps/placelists/list/LIST123",
                "Owner",
                "Coastal Weekenders",
                (
                    "Coastal weekend stops. See https://example.com/field-notes "
                    "for more, or follow @sampleguide for updates"
                ),
                None,
                None,
                [
                    [
                        None,
                        [
                            None,
                            None,
                            (
                                "Signal House, Pier Building, 10 Ocean Ave, "
                                "Sample Bay CA 94000"
                            ),
                            None,
                            "Pier Building, 10 Ocean Ave, Sample Bay CA 94000",
                            [None, None, -42.811949, 147.2614472],
                            ["-6165976776628271961", "-3752281006438109761"],
                            "/g/11g_1pnk5",
                        ],
                        "Signal House",
                        "",
                        None,
                        None,
                        None,
                        [],
                        [[1], ["-6165976776628271961", "-3752281006438109761"]],
                        [1749896974, 425450000],
                        [1749896974, 425450000],
                        None,
                        owner,
                    ],
                    [
                        None,
                        [
                            None,
                            None,
                            "",
                            None,
                            "",
                            [None, None, -41.157461399999995, 146.1758303],
                            ["-6162110142486463501", "-7368952120126222420"],
                        ],
                        "Harbor Studio",
                        "",
                        None,
                        None,
                        None,
                        [],
                        [[1], ["-6162110142486463501", "-7368952120126222420"]],
                        [1749696774, 839942000],
                        [1749696774, 839942000],
                        None,
                        owner,
                    ],
                ],
            ],
        ]

        parsed = parse_saved_list_artifacts(
            "https://www.google.com/maps/@0,0,3z/data=!4m3!11m2!2sLIST123!3e3",
            runtime_state=runtime_state,
        )

        self.assertEqual(len(parsed.places), 2)
        self.assertEqual(parsed.places[0].name, "Signal House")
        self.assertEqual(
            parsed.places[0].address,
            "Pier Building, 10 Ocean Ave, Sample Bay CA 94000",
        )
        self.assertEqual(parsed.places[0].cid, "14694463067271441855")
        self.assertEqual(parsed.places[0].google_id, "/g/11g_1pnk5")
        self.assertEqual(parsed.places[1].name, "Harbor Studio")
        self.assertEqual(parsed.places[1].address, None)
        self.assertEqual(parsed.places[1].cid, "11077791953583329196")
        self.assertEqual(parsed.places[1].google_id, None)
        self.assertNotIn(
            "104356373423434804635",
            {place.cid for place in parsed.places if place.cid is not None},
        )

    def test_keeps_owner_none_when_header_owner_is_missing(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        runtime_state[1][3] = None

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertIsNone(parsed.owner)
        self.assertEqual(
            [owner.to_dict() for owner in parsed.collaborators],
            [
                {
                    "name": "Fixture Owner",
                    "photo_url": "https://lh3.googleusercontent.com/a-/fixture-owner",
                    "profile_id": "104356373423434804635",
                },
                {
                    "name": "Fixture Collaborator",
                    "photo_url": "https://lh3.googleusercontent.com/a-/fixture-collaborator",
                    "profile_id": "205678901234567890123",
                },
            ],
        )

    def test_accepts_name_only_owner_records(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        runtime_state[1][3] = ["Name Only Owner"]
        second_place = runtime_state[1][8][1]
        assert isinstance(second_place, list)
        second_place[12] = ["Name Only Collaborator"]

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(
            parsed.owner.to_dict() if parsed.owner else None,
            {"name": "Name Only Owner"},
        )
        self.assertEqual(
            [owner.to_dict() for owner in parsed.collaborators],
            [
                {
                    "name": "Fixture Owner",
                    "photo_url": "https://lh3.googleusercontent.com/a-/fixture-owner",
                    "profile_id": "104356373423434804635",
                },
                {"name": "Name Only Collaborator"},
            ],
        )
        self.assertEqual(
            parsed.places[1].added_by.to_dict() if parsed.places[1].added_by else None,
            {"name": "Name Only Collaborator"},
        )

    def test_filters_sparse_owner_from_collaborators(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        first_place = runtime_state[1][8][0]
        assert isinstance(first_place, list)
        first_place[12] = [
            "Fixture Owner",
            None,
            "104356373423434804635",
        ]

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(
            [owner.to_dict() for owner in parsed.collaborators],
            [
                {
                    "name": "Fixture Collaborator",
                    "photo_url": "https://lh3.googleusercontent.com/a-/fixture-collaborator",
                    "profile_id": "205678901234567890123",
                }
            ],
        )

    def test_keeps_same_name_collaborator_when_owner_lacks_second_identifier(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        runtime_state[1][3] = ["Shared Name"]
        second_place = runtime_state[1][8][1]
        assert isinstance(second_place, list)
        second_place[12] = [
            "Shared Name",
            None,
            "205678901234567890123",
        ]

        parsed = parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)

        self.assertEqual(
            parsed.owner.to_dict() if parsed.owner else None,
            {"name": "Shared Name"},
        )
        self.assertEqual(
            [owner.to_dict() for owner in parsed.collaborators],
            [
                {
                    "name": "Fixture Owner",
                    "photo_url": "https://lh3.googleusercontent.com/a-/fixture-owner",
                    "profile_id": "104356373423434804635",
                },
                {"name": "Shared Name", "profile_id": "205678901234567890123"},
            ],
        )

    def test_raises_when_no_place_records_are_found(self) -> None:
        runtime_state = copy.deepcopy(["noise", _LIST_NODE])
        runtime_state[1][8] = []

        with self.assertRaises(ParseError):
            parse_saved_list_artifacts(_LIST_URL, runtime_state=runtime_state)


if __name__ == "__main__":
    unittest.main()
