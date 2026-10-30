from app.config import Settings
from app.resources import ResourcePreflight


def test_preflight_thresholds_are_advisory_and_sustained_pressure_needs_consecutive_samples(tmp_path):
    settings = Settings(
        _env_file=None,
        allowed_path_roots=str(tmp_path),
        preflight_max_swap_gb=2,
        preflight_min_disk_gb=5,
        preflight_pressure_free_percent_max=15,
        preflight_pressure_consecutive_samples=3,
        preflight_pressure_sample_interval_seconds=10,
    )
    monitor = ResourcePreflight(settings)
    risky = {
        "swap": {"used_bytes": 3 * 1024**3},
        "disk_free_bytes": 4 * 1024**3,
        "memory_pressure": {"free_percent": 10, "level": "high"},
    }
    first = monitor.assess(risky, observed_at=0)
    second = monitor.assess(risky, observed_at=10)
    third = monitor.assess(risky, observed_at=20)
    assert any("Swap use" in warning for warning in first)
    assert any("Free disk" in warning for warning in first)
    assert not any("Memory pressure" in warning for warning in first + second)
    assert any("3 consecutive samples" in warning for warning in third)


def test_preflight_clears_sustained_pressure_after_a_normal_sample(tmp_path):
    settings = Settings(
        _env_file=None, allowed_path_roots=str(tmp_path),
        preflight_pressure_free_percent_max=15,
        preflight_pressure_consecutive_samples=2,
        preflight_pressure_sample_interval_seconds=10,
    )
    monitor = ResourcePreflight(settings)
    low = {"swap": {}, "memory_pressure": {"free_percent": 8, "level": "high"}}
    normal = {"swap": {}, "memory_pressure": {"free_percent": 30, "level": "normal"}}
    monitor.assess(low, observed_at=0)
    assert not any("Memory pressure" in warning for warning in monitor.assess(normal, observed_at=10))
    assert not any("Memory pressure" in warning for warning in monitor.assess(low, observed_at=20))


def test_memory_pressure_requires_elapsed_sample_intervals(tmp_path):
    settings = Settings(
        _env_file=None, allowed_path_roots=str(tmp_path),
        preflight_pressure_free_percent_max=15,
        preflight_pressure_consecutive_samples=3,
        preflight_pressure_sample_interval_seconds=10,
    )
    monitor = ResourcePreflight(settings)
    low = {"swap": {}, "memory_pressure": {"free_percent": 8, "level": "high"}}
    assert not any("Memory pressure" in warning for warning in monitor.assess(low, observed_at=0))
    for now in (1, 2, 9):
        assert not any("Memory pressure" in warning for warning in monitor.assess(low, observed_at=now))
    assert not any("Memory pressure" in warning for warning in monitor.assess(low, observed_at=10))
    assert any("3 consecutive samples" in warning for warning in monitor.assess(low, observed_at=20))
