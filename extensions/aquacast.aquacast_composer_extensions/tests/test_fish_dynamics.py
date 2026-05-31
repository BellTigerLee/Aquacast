from pathlib import Path
import sys

import numpy as np

EXT_ROOT = Path(__file__).resolve().parents[1]
if str(EXT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXT_ROOT))

import fish_dynamics as fish


def _naive_flock_vectors(positions, directions, separation_radius, eps=1e-6):
    positions = np.asarray(positions, dtype=np.float64)
    directions = np.asarray(directions, dtype=np.float64)
    n = len(positions)
    separation = np.zeros((n, 3), dtype=np.float64)
    alignment = np.zeros((n, 3), dtype=np.float64)
    cohesion = np.zeros((n, 3), dtype=np.float64)
    counts = np.zeros((n,), dtype=np.int64)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            offset = positions[i] - positions[j]
            distance = float(np.linalg.norm(offset))
            if distance <= eps or distance > separation_radius:
                continue
            separation[i] += (offset / distance) * (1.0 - distance / separation_radius)
            alignment[i] += directions[j]
            cohesion[i] += positions[j]
            counts[i] += 1
    return separation, alignment, cohesion, counts


def _assert_matches_naive(positions, directions, radius):
    actual = fish.compute_flock_vectors(positions, directions, radius)
    expected = _naive_flock_vectors(positions, directions, radius)
    for actual_arr, expected_arr in zip(actual[:3], expected[:3]):
        np.testing.assert_allclose(actual_arr, expected_arr, atol=1e-10, rtol=0.0)
    np.testing.assert_array_equal(actual[3], expected[3])


def test_compute_flock_vectors_n_zero_returns_empty():
    sep, align, coh, counts = fish.compute_flock_vectors(np.empty((0, 3)), np.empty((0, 3)), 1.0)
    assert sep.shape == (0, 3)
    assert align.shape == (0, 3)
    assert coh.shape == (0, 3)
    assert counts.shape == (0,)


def test_compute_flock_vectors_n_one_returns_zeros():
    sep, align, coh, counts = fish.compute_flock_vectors([[1, 2, 3]], [[1, 0, 0]], 1.0)
    np.testing.assert_array_equal(sep, np.zeros((1, 3)))
    np.testing.assert_array_equal(align, np.zeros((1, 3)))
    np.testing.assert_array_equal(coh, np.zeros((1, 3)))
    np.testing.assert_array_equal(counts, np.zeros((1,), dtype=np.int64))


def test_compute_flock_vectors_two_far_apart_no_neighbors():
    positions = np.array([[0.0, 0.0, 0.0], [5.0, 0.0, 0.0]])
    directions = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
    _assert_matches_naive(positions, directions, 1.0)


def test_compute_flock_vectors_two_within_radius_mutual_pair():
    positions = np.array([[0.0, 0.0, 0.0], [0.5, 0.0, 0.0]])
    directions = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    sep, align, coh, counts = fish.compute_flock_vectors(positions, directions, 1.0)
    np.testing.assert_allclose(sep, [[-0.5, 0.0, 0.0], [0.5, 0.0, 0.0]], atol=1e-12)
    np.testing.assert_allclose(align, [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0]], atol=1e-12)
    np.testing.assert_allclose(coh, [[0.5, 0.0, 0.0], [0.0, 0.0, 0.0]], atol=1e-12)
    np.testing.assert_array_equal(counts, [1, 1])


def test_compute_flock_vectors_excludes_self_diagonal():
    positions = np.array([[0.0, 0.0, 0.0], [0.25, 0.0, 0.0], [0.5, 0.0, 0.0]])
    directions = np.eye(3)
    sep, align, coh, counts = fish.compute_flock_vectors(positions, directions, 1.0)
    np.testing.assert_array_equal(counts, [2, 2, 2])
    _assert_matches_naive(positions, directions, 1.0)


def test_compute_flock_vectors_excludes_overlapping_positions():
    positions = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.5, 0.0, 0.0]])
    directions = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    _assert_matches_naive(positions, directions, 1.0)


def test_compute_flock_vectors_neighbor_count_matches_naive_loop():
    rng = np.random.default_rng(123)
    positions = rng.normal(size=(20, 3))
    directions = rng.normal(size=(20, 3))
    directions /= np.maximum(np.linalg.norm(directions, axis=1, keepdims=True), 1e-12)
    _assert_matches_naive(positions, directions, 1.25)


def test_compute_flock_vectors_separation_weight_drops_to_zero_at_radius_edge():
    positions = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    directions = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
    sep, _align, _coh, counts = fish.compute_flock_vectors(positions, directions, 1.0)
    np.testing.assert_allclose(sep, np.zeros((2, 3)), atol=1e-12)
    np.testing.assert_array_equal(counts, [1, 1])


def test_compute_flock_vectors_matches_naive_loop_n10_random():
    rng = np.random.default_rng(10)
    positions = rng.uniform(-1.0, 1.0, size=(10, 3))
    directions = rng.normal(size=(10, 3))
    directions /= np.maximum(np.linalg.norm(directions, axis=1, keepdims=True), 1e-12)
    _assert_matches_naive(positions, directions, 0.9)


def test_compute_flock_vectors_matches_naive_loop_n50_random():
    rng = np.random.default_rng(50)
    positions = rng.uniform(-2.0, 2.0, size=(50, 3))
    directions = rng.normal(size=(50, 3))
    directions /= np.maximum(np.linalg.norm(directions, axis=1, keepdims=True), 1e-12)
    _assert_matches_naive(positions, directions, 1.1)
