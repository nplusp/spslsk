"""Tests for the search-quality improvements in app/downloader.py.

Covers Units 1-3 of the search quality plan:
- Unit 1: _build_search_queries (single-char collapse, longest word, ASCII-fold)
- Unit 2: _matches_track (artist check, scaled threshold, full path, short-artist fallback)
- Unit 3: _score_file (phrase match wins over format)
"""
import pytest

from app.downloader import (
    _build_search_queries,
    _matches_track,
    _score_file,
)


# ---------------------------------------------------------------------------
# Unit 1: _build_search_queries
# ---------------------------------------------------------------------------


class TestBuildSearchQueriesSingleCharCollapse:
    """R5: collapse runs of single-character whitespace-separated tokens."""

    def test_three_single_chars_collapse(self):
        # The S L F diagnostic case. slskd rejects single-char-only queries
        # with HTTP 400; collapsing to "SLF" produces a valid query.
        queries = _build_search_queries("S L F", "Tag Team")
        assert queries[0] == "SLF"
        # Narrowing query uses the longest title word ("Team" vs "Tag")
        assert queries[1] == "SLF Team"

    def test_partial_collapse_preserves_normal_tokens(self):
        queries = _build_search_queries("S L F Merkin", "Tag Team")
        assert queries[0] == "SLF Merkin"

    def test_four_single_chars_collapse(self):
        queries = _build_search_queries("A B C D Something", "X")
        assert queries[0] == "ABCD Something"

    def test_single_isolated_one_char_token_is_kept(self):
        # A SOLO single-char token (not in a run) is left alone — only
        # ≥2 consecutive single-char tokens are collapsed.
        queries = _build_search_queries("Mr X Foo", "Track")
        assert queries[0] == "Mr X Foo"

    def test_two_single_char_run_in_middle(self):
        queries = _build_search_queries("Mr X Y Foo", "Track")
        # "X Y" → "XY", surrounding "Mr" and "Foo" untouched
        assert queries[0] == "Mr XY Foo"

    def test_artist_with_no_collapse_needed_unchanged(self):
        queries = _build_search_queries("Bob Sinclar", "Gym Tonic")
        assert queries[0] == "Bob Sinclar"

    def test_letters_and_digits_in_run(self):
        # "U 2" is a real-ish case (band name written with weird spacing)
        queries = _build_search_queries("U 2", "Sunday Bloody Sunday")
        assert queries[0] == "U2"


class TestBuildSearchQueriesLongestWord:
    """R6: pick the longest significant title word for the narrowing query."""

    def test_picks_longest_over_first(self):
        # "Karma" (5) vs "Police" (6) — Police wins by length
        queries = _build_search_queries("Radiohead", "Karma Police")
        assert queries[1] == "Radiohead Police"

    def test_ties_broken_by_first_occurrence(self):
        # "Always" (6) vs "Randy" (5) — Always wins
        queries = _build_search_queries("Severed Heads", "Always Randy")
        assert queries[1] == "Severed Heads Always"

    def test_filler_words_skipped(self):
        # "the" is filler, "Wall" is the only significant word
        queries = _build_search_queries("Pink Floyd", "The Wall")
        assert queries[1] == "Pink Floyd Wall"

    def test_short_words_skipped(self):
        # "Be" (2 chars) fails > 2, "Mine" (4) is the only significant word
        queries = _build_search_queries("Artist", "Be Mine")
        assert queries[1] == "Artist Mine"

    def test_all_words_filtered_no_narrow_query(self):
        # All title words are too short or filler — no narrow query produced
        queries = _build_search_queries("Artist", "It")
        assert queries == ["Artist"]


class TestBuildSearchQueriesAsciiFold:
    """ASCII-fold defensive transform — works around slskd's Unicode bugs
    AND improves recall (peer files mostly use ASCII filenames)."""

    def test_diaeresis_folded(self):
        # Björk → Bjork
        queries = _build_search_queries("Björk", "Hyperballad")
        assert queries[0] == "Bjork"
        assert queries[1] == "Bjork Hyperballad"

    def test_acute_accent_folded(self):
        queries = _build_search_queries("Céline Dion", "Power")
        assert queries[0] == "Celine Dion"

    def test_ae_ligature_folded(self):
        # Æ is indivisible in NFKD — needs explicit mapping. The user's
        # DHÆÜR diagnostic case caused slskd to throw a database error.
        queries = _build_search_queries("DHÆÜR", "Track")
        assert "Æ" not in queries[0]
        assert "Ü" not in queries[0]
        # Æ → AE, Ü → U
        assert queries[0] == "DHAEUR"

    def test_o_slash_folded(self):
        queries = _build_search_queries("Mötley Crüe", "Kickstart Heart")
        assert "Mötley" not in queries[0]
        assert "Crüe" not in queries[0]
        assert queries[0] == "Motley Crue"

    def test_mixed_case_preserved(self):
        # Case preservation is desirable — slskd searches are case-insensitive
        # but file paths often preserve case for display.
        queries = _build_search_queries("Sigur Rós", "Hoppípolla")
        assert queries[0] == "Sigur Ros"

    def test_pure_ascii_unchanged(self):
        queries = _build_search_queries("Bob Sinclar", "Gym Tonic")
        assert queries[0] == "Bob Sinclar"


# ---------------------------------------------------------------------------
# Unit 2: _matches_track
# ---------------------------------------------------------------------------


class TestMatchesTrackArtistCheck:
    """R1: at least one significant artist word must appear in the path."""

    def test_artist_in_filename_passes(self):
        assert _matches_track(
            "Radiohead - Karma Police.flac", "Radiohead", "Karma Police"
        )

    def test_artist_in_directory_passes(self):
        # R4: full-path matching, not just basename. Real peer libraries
        # often store artist as a parent directory.
        assert _matches_track(
            "Radiohead\\OK Computer\\01 - Karma Police.flac",
            "Radiohead",
            "Karma Police",
        )

    def test_artist_missing_rejects(self):
        # The Severed Heads case: filename has the right title words but
        # no significant artist word in the path. This is "wrong band, right
        # title fragment" — must be filtered out.
        assert not _matches_track(
            "Coldplay - Always.flac", "Severed Heads", "Always Randy"
        )

    def test_one_artist_word_is_enough(self):
        # Only "Bob" is in the filename, "Sinclar" is not — passes because
        # ≥1 significant artist word is sufficient.
        assert _matches_track(
            "Bob - Paradise - 03 Gym Tonic.flac", "Bob Sinclar", "Gym Tonic"
        )

    def test_filler_artist_words_dont_count(self):
        # "The" alone in the path is not enough — needs a real artist word
        assert not _matches_track(
            "Various - The Best Of Always Randy.flac",
            "Severed Heads",
            "Always Randy",
        )


class TestMatchesTrackScaledThreshold:
    """R2: title match threshold scales with title length: ceil(N/2)."""

    def test_two_word_title_one_match_passes(self):
        # 2 words → ceil(2/2) = 1 required. Title has both, passes.
        assert _matches_track(
            "Radiohead - Karma Police.flac", "Radiohead", "Karma Police"
        )

    def test_two_word_title_only_one_word_present_passes(self):
        # 2 significant words, threshold 1, only "karma" in filename: passes
        # (artist check also satisfied)
        assert _matches_track(
            "Radiohead - Karma Other.flac", "Radiohead", "Karma Police"
        )

    def test_six_word_title_only_one_match_rejects(self):
        # The S L F diagnostic case: 6 significant title words, threshold
        # ceil(6/2)=3, only "Tag" in filename → rejected (1 < 3).
        assert not _matches_track(
            "SLF - Tag elsewhere.flac",
            "SLF",
            "Tag Team Triangle Moodymann Edit Mixed",
        )

    def test_six_word_title_three_matches_passes(self):
        # 3 ≥ 3 → passes
        assert _matches_track(
            "SLF - Tag Team Triangle.flac",
            "SLF",
            "Tag Team Triangle Moodymann Edit Mixed",
        )

    def test_three_word_title_two_matches_passes(self):
        # ceil(3/2)=2, two of three present
        assert _matches_track(
            "Artist - One Two Other.flac", "Artist", "One Two Three"
        )

    def test_three_word_title_one_match_rejects(self):
        # ceil(3/2)=2, only one of three present (One)
        assert not _matches_track(
            "Artist - One Four Five.flac", "Artist", "One Two Three"
        )


class TestMatchesTrackShortArtistFallback:
    """R3: when artist has zero significant words after filter, fall back to
    requiring a full phrase match of the title."""

    def test_short_artist_phrase_match_passes(self):
        # "U2" is 2 chars, fails > 2 filter → no significant artist words.
        # Fallback: require the title phrase as a contiguous substring.
        assert _matches_track(
            "U2 - Sunday Bloody Sunday.flac", "U2", "Sunday Bloody Sunday"
        )

    def test_short_artist_phrase_match_fails(self):
        # No significant artist words → fallback to phrase. Phrase not in path.
        assert not _matches_track(
            "Random Band - Something Else.flac", "U2", "Sunday Bloody Sunday"
        )

    def test_short_artist_partial_title_overlap_rejected(self):
        # Only "sunday" in path, but the FULL phrase "sunday bloody sunday"
        # is required by the fallback path.
        assert not _matches_track(
            "Coldplay - Sunday Morning.flac", "U2", "Sunday Bloody Sunday"
        )


class TestMatchesTrackEdgeCases:
    def test_case_insensitive(self):
        assert _matches_track(
            "RADIOHEAD - karma police.FLAC", "radiohead", "Karma Police"
        )

    def test_both_degenerate_returns_true(self):
        # Pathological: no significant artist words AND no significant title
        # words. Conservative true (matches old behavior, lets scoring filter).
        assert _matches_track("X - Y.flac", "X", "Y")


# ---------------------------------------------------------------------------
# Unit 3: _score_file
# ---------------------------------------------------------------------------


def _f(filename: str, ext_size_kbps=(0, 0), size=1000) -> dict:
    """Build a minimal file_info dict for scoring tests."""
    bitrate = ext_size_kbps[1] if isinstance(ext_size_kbps, tuple) else 0
    return {"filename": filename, "size": size, "bitRate": bitrate}


class TestScoreFile:
    def test_phrase_match_beats_format(self):
        """R7: full phrase match outranks format priority. The Severed Heads
        diagnostic case — wrong-band FLAC vs right-track MP3."""
        right_mp3 = _f("Bob Sinclair\\Paradise\\03 - Gym Tonic.mp3", size=8000)
        right_mp3["bitRate"] = 320
        wrong_flac = _f("Random\\Compilation\\track01.flac", size=50000)

        # Both go through scoring with "Gym Tonic" as the title query
        score_right = _score_file(right_mp3, "Bob Sinclar", "Gym Tonic")
        score_wrong = _score_file(wrong_flac, "Bob Sinclar", "Gym Tonic")
        # Smaller tuple wins under ascending sort
        assert score_right < score_wrong

    def test_more_matches_beats_format(self):
        """R8: when neither has full phrase, more matched words wins."""
        two_match_mp3 = _f("Artist\\Album\\One Two.mp3")
        two_match_mp3["bitRate"] = 320
        one_match_flac = _f("Artist\\Album\\One.flac")
        # Title has 3 significant words: "One", "Two", "Three"
        s_better = _score_file(two_match_mp3, "Artist", "One Two Three")
        s_worse = _score_file(one_match_flac, "Artist", "One Two Three")
        assert s_better < s_worse

    def test_format_breaks_tie_when_match_equal(self):
        """When both have the same phrase rank and match count, format wins."""
        flac = _f("Artist - Song.flac")
        mp3 = _f("Artist - Song.mp3")
        mp3["bitRate"] = 320
        s_flac = _score_file(flac, "Artist", "Song")
        s_mp3 = _score_file(mp3, "Artist", "Song")
        # Both phrase-match. Both 1 match. FLAC wins on format.
        assert s_flac < s_mp3

    def test_bitrate_breaks_tie_within_format(self):
        a = _f("Artist - Song.mp3")
        a["bitRate"] = 320
        b = _f("Artist - Song.mp3")
        b["bitRate"] = 128
        s_a = _score_file(a, "Artist", "Song")
        s_b = _score_file(b, "Artist", "Song")
        assert s_a < s_b

    def test_phrase_match_single_word_title(self):
        # Single-word title: phrase match = word match
        present = _f("Artist - Creep.flac")
        absent = _f("Artist - Other.flac")
        s_present = _score_file(present, "Artist", "Creep")
        s_absent = _score_file(absent, "Artist", "Creep")
        assert s_present < s_absent

    def test_empty_title_falls_through_to_format(self):
        # Pathological: empty cleaned title — neither phrase nor word match
        # is meaningful. Should fall through to format-based ordering without
        # crashing.
        flac = _f("track.flac")
        mp3 = _f("track.mp3")
        mp3["bitRate"] = 320
        s_flac = _score_file(flac, "Artist", "")
        s_mp3 = _score_file(mp3, "Artist", "")
        # FLAC still beats MP3 because format takes over when match is moot
        assert s_flac < s_mp3
