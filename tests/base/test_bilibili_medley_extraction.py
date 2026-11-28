import unittest
from unittest.mock import patch

from supysonic.bilibili_discovery.extractor import extract_history
from supysonic.openai_recommendations import OpenAIRecommendationError
from tests.base.test_bilibili_discovery import REQUEST, observation, result


class MedleyExtractionTestCase(unittest.TestCase):
    def test_cover_medley_keeps_two_works_one_performer_and_one_source(self) -> None:
        title = "11年后女王归来！CL再唱2NE1神曲《FIRE + I AM THE BEST》｜2026 MAMA"
        works = [{"title": song, "artist": "2NE1", "performer": "CL"}
                 for song in ("FIRE", "I AM THE BEST", "FIRE")]
        payload = {"results": [result(2, "non_music"), {"id": 1, "evidence": title, "works": works}]}
        with patch(REQUEST, return_value=payload) as model:
            output = extract_history({}, [observation(1, title), observation(2, "周末出游")])
        self.assertEqual(model.call_args.kwargs["reasoning_effort"], "high")
        self.assertEqual([(row.number, row.kind, row.title, row.artist, row.performer) for row in output],
                         [(1, "work_lead", "FIRE", "2NE1", "CL"),
                          (1, "work_lead", "I AM THE BEST", "2NE1", "CL"),
                          (2, "non_music", "", "", "")])
        self.assertTrue(all(row.version == "unknown" and not row.collaborator for row in output))

    def test_no_fixed_work_count_and_no_hallucinated_role_or_song(self) -> None:
        names = [f"Song {n}" for n in range(16)]
        title = "Eva翻唱：" + "+".join(names)
        works = [{"title": name, "artist": "Eva", "performer": "Eva"} for name in names]
        works.extend([{"title": "Imagined", "artist": "Someone", "performer": "Eva"},
                      {"title": names[0], "artist": "Not In Title", "performer": "Eva"}])
        with patch(REQUEST, return_value={"results": [{"id": 1, "works": works, "evidence": title}]}):
            output = extract_history({}, [observation(1, title)])
        self.assertEqual([row.title for row in output if row.kind == "work_lead"], names)
        self.assertTrue(all(row.artist == "" for row in output))
        self.assertEqual({row.performer for row in output if row.kind == "work_lead"}, {"Eva"})
        self.assertEqual([row.reason for row in output if row.kind != "work_lead"], ["source_work_unverified"])

    def test_artist_lead_requires_literal_performance_and_rejects_original_owner(self) -> None:
        titles = ["CL现场独唱", "CL舞台点评", "CL再唱2NE1神曲", "音乐分享", "CL《Song》现场"]
        rows = [result(n, "artist_lead", performer=name, evidence=titles[n-1])
                for n, name in enumerate(("CL", "CL", "2NE1", "Fixture Uploader"), 1)]
        rows.append(result(5, "song", artist="CL", performer="CL", title="Song", version="live",
                           evidence=titles[4]))
        with patch(REQUEST, return_value={"results": rows}):
            output = extract_history({}, [observation(n, text) for n, text in enumerate(titles, 1)])
        self.assertEqual([row.kind for row in output], ["artist_lead", "uncertain", "uncertain",
                                                        "uncertain", "song"])
        self.assertEqual([row.performer for row in output], ["CL", "", "", "", "CL"])

    def test_truncation_splits_the_batch_and_retries_a_dense_single_source(self) -> None:
        truncated = OpenAIRecommendationError("output truncated", reason="output_limit")
        calls = [truncated, {"results": [result(1, "non_music")]}, truncated,
                 {"results": [result(2, "non_music")]}]
        with patch(REQUEST, side_effect=calls) as model:
            output = extract_history({}, [observation(1, "Testing One"), observation(2, "Testing Two")])
        self.assertEqual([row.number for row in output], [1, 2])
        self.assertEqual([call.kwargs["max_output_tokens"] for call in model.call_args_list],
                         [8192, 8192, 8192, 16384])

    def test_unconfirmed_recording_preserves_literal_performer_and_work(self) -> None:
        title = "Eva 打歌舞台《Fire》"
        payload = {"results": [result(1, "song", artist="Eva", performer="Eva",
                                       title="Fire", version="studio", evidence=title)]}
        with patch(REQUEST, return_value=payload):
            row, = extract_history({}, [observation(1, title)])
        self.assertEqual((row.kind, row.title, row.artist, row.performer, row.version),
                         ("work_lead", "Fire", "", "Eva", "unknown"))

    def test_malformed_or_missing_source_cannot_commit_the_batch(self) -> None:
        work = {"title": "Song", "artist": "A", "performer": "A"}
        for payload in (
            [{"id": 1, "works": [work], "evidence": "A Song现场"}],
            [{"id": 1, "works": [], "evidence": ""}, result(2, "non_music")],
            [{"id": 2, "works": [work], "evidence": "A Song现场"}, result(2, "non_music")],
        ):
            with self.subTest(payload=payload), patch(REQUEST, return_value={"results": payload}):
                with self.assertRaises(OpenAIRecommendationError):
                    extract_history({}, [observation(1, "A Song现场"), observation(2, "无关标题")])
