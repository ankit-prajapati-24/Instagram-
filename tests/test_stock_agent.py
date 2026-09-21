"""Unit and integration tests for AI-Powered Stock Video Matcher Agent."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stock_agent import (
    ClipDownloader,
    ClipMatch,
    ClipQuery,
    PexelsFetcher,
    PexelsVideo,
    StockMatcherResult,
    StockVideoMatcherAgent,
    VideoFileCandidate,
    VisualQueryGenerator,
)


class TestVisualQueryGenerator:
    """Test LLM prompt construction, pacing math, and query sanitization."""

    @pytest.fixture
    def generator(self):
        with patch("stock_agent.OpenAI"):
            return VisualQueryGenerator(
                base_url="http://mock-gateway/v1",
                api_key="mock-key",
                model="mock-model",
            )

    def test_calculate_clip_count_fast_cuts(self, generator):
        """Verify FactoHolic/Shorts style clip count: ceil(duration / 2.5)."""
        assert generator.calculate_clip_count(2.0) == 1
        assert generator.calculate_clip_count(2.5) == 1
        assert generator.calculate_clip_count(2.6) == 2
        assert generator.calculate_clip_count(5.0) == 2
        assert generator.calculate_clip_count(7.5) == 3
        assert generator.calculate_clip_count(10.0) == 4
        assert generator.calculate_clip_count(12.0) == 5

    def test_sanitize_query_bans_abstract_words(self, generator):
        """Ensure banned emotional and abstract words are removed and word length is 3-5."""
        # Query with banned words: "expensive crazy mansion"
        sanitized = generator._sanitize_query("expensive crazy mansion aerial view")
        words = sanitized.split()
        assert "expensive" not in words
        assert "crazy" not in words
        assert 3 <= len(words) <= 5

    def test_sanitize_query_word_length_enforcement(self, generator):
        """Enforces minimum 3 words and maximum 5 words."""
        # Long query > 5 words
        long_q = generator._sanitize_query("drone camera flying above modern glass villa poolside patio")
        assert len(long_q.split()) <= 5

        # Short query < 3 words
        short_q = generator._sanitize_query("vault door")
        assert len(short_q.split()) >= 3

    def test_parse_json_response_with_markdown_fences(self, generator):
        """Test extraction from markdown ```json ``` codeblocks."""
        raw_response = """
Here is the visual shot breakdown:
```json
{
  "clips": [
    {
      "clip_index": 1,
      "search_query": "drone aerial mountain fortress",
      "shot_intent": "Establishing shot of remote Alps base"
    },
    {
      "clip_index": 2,
      "search_query": "heavy steel vault door",
      "shot_intent": "Reveal of the impenetrable entrance"
    }
  ]
}
```
Hope this helps!
"""
        clips = generator._parse_json_response(raw_response, expected_count=2)
        assert len(clips) == 2
        assert clips[0]["search_query"] == "drone aerial mountain fortress"
        assert clips[1]["search_query"] == "heavy steel vault door"

    def test_generate_queries_mocked_llm(self, generator):
        """Test full generate_queries pipeline with mocked OpenAI completion."""
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "clips": [
                {
                    "clip_index": 1,
                    "search_query": "drone aerial luxury mansion",
                    "shot_intent": "Establishing shot of wealth"
                },
                {
                    "clip_index": 2,
                    "search_query": "counting dollar bills hands",
                    "shot_intent": "Visual of extreme cash flow"
                }
            ]
        })
        generator.client.chat.completions.create = MagicMock(
            return_value=MagicMock(choices=[mock_choice])
        )

        queries, count = generator.generate_queries("Voiceover text", duration_seconds=5.0)
        assert count == 2
        assert len(queries) == 2
        assert queries[0].search_query == "drone aerial luxury mansion"
        assert queries[1].search_query == "counting dollar bills hands"


class TestPexelsFetcher:
    """Test Pexels search, portrait preference, quality re-ranking, and landscape fallback."""

    @pytest.fixture
    def fetcher(self):
        return PexelsFetcher(api_key="mock_pexels_key")

    def test_select_best_video_prefers_portrait_hd(self, fetcher):
        """Ensure 1080x1920 portrait MP4 is prioritized over landscape or lower res."""
        candidate_videos = [
            {
                "id": 101,
                "url": "https://pexels.com/video/101",
                "duration": 15,
                "video_files": [
                    {
                        "id": 1,
                        "quality": "sd",
                        "file_type": "video/mp4",
                        "width": 540,
                        "height": 960,
                        "fps": 30.0,
                        "link": "https://video.mp4/sd_portrait.mp4",
                    },
                    {
                        "id": 2,
                        "quality": "hd",
                        "file_type": "video/mp4",
                        "width": 1080,
                        "height": 1920,
                        "fps": 30.0,
                        "link": "https://video.mp4/hd_portrait.mp4",
                    },
                ],
            },
            {
                "id": 102,
                "url": "https://pexels.com/video/102",
                "duration": 20,
                "video_files": [
                    {
                        "id": 3,
                        "quality": "hd",
                        "file_type": "video/mp4",
                        "width": 1920,
                        "height": 1080,
                        "fps": 30.0,
                        "link": "https://video.mp4/hd_landscape.mp4",
                    }
                ],
            },
        ]

        best_video = fetcher._select_best_video(candidate_videos, target_portrait=True)
        assert best_video is not None
        assert best_video.id == 101
        assert best_video.selected_file is not None
        assert best_video.selected_file.width == 1080
        assert best_video.selected_file.height == 1920
        assert best_video.selected_file.is_portrait is True
        assert best_video.selected_file.link == "https://video.mp4/hd_portrait.mp4"

    @staticmethod
    def _video_with_files(video_id, files, quality="hd"):
        """Build one Pexels-shaped video dict from a list of (width, height) tuples."""
        return {
            "id": video_id,
            "url": f"https://pexels.com/video/{video_id}",
            "duration": 15,
            "video_files": [
                {
                    "id": idx + 1,
                    "quality": quality,
                    "file_type": "video/mp4",
                    "width": width,
                    "height": height,
                    "fps": 30.0,
                    "link": f"https://video.mp4/{video_id}_{width}x{height}.mp4",
                }
                for idx, (width, height) in enumerate(files)
            ],
        }

    def test_select_best_video_prefers_smallest_covering_resolution(self, fetcher):
        """Among files that all cover the 1080x1920 render target, the smallest
        covering one (closest from above) should win, not the largest."""
        candidate_videos = [
            self._video_with_files(301, [(720, 1280), (1080, 1920), (2160, 3840)])
        ]

        best_video = fetcher._select_best_video(candidate_videos, target_portrait=True)
        assert best_video is not None
        assert best_video.selected_file is not None
        assert (best_video.selected_file.width, best_video.selected_file.height) == (1080, 1920)

    def test_select_best_video_prefers_smaller_of_two_covering_files(self, fetcher):
        """Given only two oversized candidates, prefer the one closer to (but
        still at/above) the render target rather than the larger one."""
        candidate_videos = [
            self._video_with_files(302, [(2160, 3840), (1440, 2560)])
        ]

        best_video = fetcher._select_best_video(candidate_videos, target_portrait=True)
        assert best_video is not None
        assert best_video.selected_file is not None
        assert (best_video.selected_file.width, best_video.selected_file.height) == (1440, 2560)

    def test_select_best_video_undersized_only_picks_largest(self, fetcher):
        """When nothing covers the render target, fall back to the largest
        undersized file available (least upscaling)."""
        candidate_videos = [
            self._video_with_files(303, [(480, 854), (720, 1280)])
        ]

        best_video = fetcher._select_best_video(candidate_videos, target_portrait=True)
        assert best_video is not None
        assert best_video.selected_file is not None
        assert (best_video.selected_file.width, best_video.selected_file.height) == (720, 1280)

    def test_select_best_video_orientation_beats_huge_wrong_orientation_file(self, fetcher):
        """A correctly-oriented, undersized file must beat a huge file with the
        wrong orientation. This pins the fix for the latent bug where the old
        raw pixel-count resolution term (up to ~8.3M for a 2160x3840 file)
        could exceed the 5,000,000 orientation bonus, letting a wrongly
        oriented file win outright."""
        candidate_videos = [
            self._video_with_files(304, [(720, 1280)]),  # correct portrait, small
            self._video_with_files(305, [(3840, 2160)]),  # wrong orientation, huge
        ]

        best_video = fetcher._select_best_video(candidate_videos, target_portrait=True)
        assert best_video is not None
        assert best_video.id == 304
        assert best_video.selected_file is not None
        assert (best_video.selected_file.width, best_video.selected_file.height) == (720, 1280)
        assert best_video.selected_file.is_portrait is True

    def test_search_video_fallback_to_landscape(self, fetcher):
        """When portrait returns 0 results, fallback to landscape."""
        def mock_execute(query, orientation=None, per_page=5):
            if orientation == "portrait":
                return [], False
            if orientation == "landscape":
                return [
                    {
                        "id": 202,
                        "url": "https://pexels.com/video/202",
                        "duration": 10,
                        "video_files": [
                            {
                                "id": 10,
                                "quality": "hd",
                                "file_type": "video/mp4",
                                "width": 1920,
                                "height": 1080,
                                "fps": 25.0,
                                "link": "https://video.mp4/landscape_backup.mp4",
                            }
                        ],
                    }
                ], False
            return [], False

        with patch.object(fetcher, "_execute_search", side_effect=mock_execute):
            video, was_fallback = fetcher.search_video("rare footage query", orientation="portrait")
            assert video is not None
            assert video.id == 202
            assert was_fallback is True
            assert video.was_fallback is True


class TestClipDownloader:
    """Test sequential chunked video downloading and atomic rename."""

    def test_download_clip_atomic_write(self, tmp_path):
        downloader = ClipDownloader(output_dir=str(tmp_path))

        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_response.headers = {"content-length": "100"}
        mock_response.iter_content.return_value = [b"chunk_1", b"chunk_2"]
        mock_response.raise_for_status = MagicMock()

        with patch.object(downloader.session, "get", return_value=mock_response):
            saved_file = downloader.download_clip("https://video.mp4/sample.mp4", clip_index=1)
            assert saved_file.exists()
            assert saved_file.name == "clip_01.mp4"
            assert saved_file.read_bytes() == b"chunk_1chunk_2"
            # Ensure temporary file was cleanly renamed and does not linger
            assert not (tmp_path / "clip_01.mp4.tmp").exists()


class TestStockVideoMatcherAgent:
    """End-to-end agent orchestration test with mocks."""

    def test_full_match_pipeline(self, tmp_path):
        with patch("stock_agent.OpenAI"):
            agent = StockVideoMatcherAgent(
                omniroute_base_url="http://mock/v1",
                omniroute_api_key="mock",
                pexels_api_key="mock",
            )

        # Mock query generation
        mock_queries = [
            ClipQuery(clip_index=1, search_query="drone aerial mansion", shot_intent="Wealth establishing"),
            ClipQuery(clip_index=2, search_query="hands counting money", shot_intent="Financial power"),
        ]
        agent.query_generator.generate_queries = MagicMock(return_value=(mock_queries, 2))

        # Mock Pexels fetcher
        mock_video_1 = PexelsVideo(
            id=111,
            url="https://pexels.com/video/111",
            duration=12,
            tags=["mansion", "aerial"],
            user_name="DronePilot",
            selected_file=VideoFileCandidate(
                id=1,
                quality="hd",
                file_type="video/mp4",
                width=1080,
                height=1920,
                fps=30.0,
                link="https://videos.pexels.com/v1.mp4",
            ),
        )
        mock_video_2 = PexelsVideo(
            id=222,
            url="https://pexels.com/video/222",
            duration=8,
            tags=["money", "hands"],
            user_name="Cinematographer",
            selected_file=VideoFileCandidate(
                id=2,
                quality="hd",
                file_type="video/mp4",
                width=1080,
                height=1920,
                fps=30.0,
                link="https://videos.pexels.com/v2.mp4",
            ),
        )

        agent.pexels_fetcher.search_video = MagicMock(side_effect=[
            (mock_video_1, False),
            (mock_video_2, False),
        ])

        # Mock downloader
        agent.downloader.download_clip = MagicMock(side_effect=[
            tmp_path / "clip_01.mp4",
            tmp_path / "clip_02.mp4",
        ])

        result = agent.match(
            script_segment="Inside the ultra-exclusive mansion, fortunes change hands daily.",
            duration_seconds=5.0,
            download=True,
            output_dir=str(tmp_path),
        )

        assert isinstance(result, StockMatcherResult)
        assert result.clip_count == 2
        assert len(result.matches) == 2
        assert result.matches[0].query.search_query == "drone aerial mansion"
        assert result.matches[0].video.id == 111
        assert result.matches[0].download_path == str(tmp_path / "clip_01.mp4")
        assert result.matches[1].video.id == 222
        assert result.matches[1].download_path == str(tmp_path / "clip_02.mp4")

        # Verify JSON serialization
        json_output = result.to_json()
        assert "drone aerial mansion" in json_output
        parsed = json.loads(json_output)
        assert parsed["clip_count"] == 2
        assert len(parsed["matches"]) == 2

    def test_async_match(self):
        import asyncio

        with patch("stock_agent.OpenAI"):
            agent = StockVideoMatcherAgent(
                omniroute_base_url="http://mock/v1",
                omniroute_api_key="mock",
                pexels_api_key="mock",
            )

        agent.match = MagicMock(return_value=StockMatcherResult(
            script_segment="test",
            duration_seconds=2.5,
            clip_count=1,
            model_used="test-model",
            matches=[],
        ))

        res = asyncio.run(agent.match_async("test", 2.5))
        assert res.clip_count == 1
        agent.match.assert_called_once_with(
            script_segment="test",
            duration_seconds=2.5,
            download=False,
            output_dir="output_clips",
            mock=False,
        )

    def test_mock_match(self):
        """Test full mock matching pipeline without API calls."""
        agent = StockVideoMatcherAgent()
        result = agent.match(
            script_segment="The deepest vault in Zurich contains billions in bullion.",
            duration_seconds=5.0,
            mock=True,
        )
        assert result.clip_count == 2
        assert len(result.matches) == 2
        assert result.matches[0].video is not None
        assert result.matches[0].video.selected_file.is_portrait is True
        assert result.matches[0].video.selected_file.resolution == "1080x1920"
