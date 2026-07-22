from __future__ import annotations

import numpy as np
import pytest

from grab_app.spatial import MosaicComposer, estimate_adjacent_translation


def test_phase_correlation_uses_expected_horizontal_overlap() -> None:
    rng = np.random.default_rng(42)
    scene = rng.normal(128, 30, size=(80, 180)).astype(np.float32)
    reference = scene[:, :100]
    moving = scene[:, 80:180]

    result = estimate_adjacent_translation(reference, moving, direction="right", overlap=0.2)

    assert result.success
    assert not result.used_fallback
    assert result.translation_px == pytest.approx((80.0, 0.0), abs=0.15)
    assert result.confidence > 0.5


def test_phase_correlation_safely_falls_back_for_blank_tiles() -> None:
    blank = np.zeros((50, 100), dtype=np.uint8)
    result = estimate_adjacent_translation(blank, blank, direction="right", overlap=0.25)

    assert not result.success
    assert result.used_fallback
    assert result.translation_px == (75.0, 0.0)
    assert result.confidence == 0.0


def test_feather_composer_tracks_canvas_coverage_and_quality() -> None:
    composer = MosaicComposer((6, 8), mode="feather")
    assert composer.add_tile(np.full((4, 4), 50, np.uint8), (0, 1), quality=0.8)
    assert composer.add_tile(np.full((4, 4), 150, np.uint8), (2, 1), quality=0.4)

    assert composer.coverage[2, 0] == 1
    assert composer.coverage[2, 2] == 2
    assert 50 < composer.canvas[2, 2] < 150
    assert 0.4 < composer.quality[2, 2] < 0.8
    assert composer.image().dtype == np.uint8


def test_stable_composer_does_not_replace_with_lower_quality() -> None:
    composer = MosaicComposer((4, 4), mode="stable")
    composer.add_tile(np.full((4, 4), 200, np.uint8), (0, 0), quality=0.9)
    composer.add_tile(np.full((4, 4), 20, np.uint8), (0, 0), quality=0.2)

    assert np.all(composer.canvas == 200)
    assert np.all(composer.coverage == 2)
    assert np.all(composer.quality == pytest.approx(0.9))


def test_composer_clips_tiles_at_canvas_edges_and_can_downsample() -> None:
    composer = MosaicComposer((10, 10), downsample=2)
    assert composer.add_tile(np.full((6, 6), 100, np.uint8), (-2, -2))
    assert composer.canvas.shape == (5, 5)
    assert np.count_nonzero(composer.coverage) == 4
