"""Tests for stateguard.core.models.config."""

from __future__ import annotations

import pytest

from stateguard.core.models.config import GuardConfig, RepairConfig


# ---------------------------------------------------------------------------
# RepairConfig
# ---------------------------------------------------------------------------


class TestRepairConfig:

    # --- Defaults -------------------------------------------------------------

    def test_default_max_attempts(self) -> None:
        assert RepairConfig().max_attempts == 3

    def test_default_min_confidence_threshold(self) -> None:
        assert RepairConfig().min_confidence_threshold == 0.7

    def test_default_score_collision_margin(self) -> None:
        assert RepairConfig().score_collision_margin == 0.15

    def test_default_allow_partial_repair(self) -> None:
        assert RepairConfig().allow_partial_repair is True

    def test_default_include_values_in_log(self) -> None:
        assert RepairConfig().include_values_in_log is False

    # --- Custom construction --------------------------------------------------

    def test_custom_max_attempts(self) -> None:
        c = RepairConfig(max_attempts=10)
        assert c.max_attempts == 10

    def test_custom_confidence_threshold(self) -> None:
        c = RepairConfig(min_confidence_threshold=0.9)
        assert c.min_confidence_threshold == 0.9

    def test_custom_collision_margin(self) -> None:
        c = RepairConfig(score_collision_margin=0.05)
        assert c.score_collision_margin == 0.05

    def test_disable_partial_repair(self) -> None:
        c = RepairConfig(allow_partial_repair=False)
        assert c.allow_partial_repair is False

    def test_enable_value_logging(self) -> None:
        c = RepairConfig(include_values_in_log=True)
        assert c.include_values_in_log is True

    def test_all_defaults_together(self) -> None:
        c = RepairConfig()
        assert c == RepairConfig(
            max_attempts=3,
            min_confidence_threshold=0.7,
            score_collision_margin=0.15,
            allow_partial_repair=True,
            include_values_in_log=False,
        )

    # --- max_attempts validation ----------------------------------------------

    def test_max_attempts_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must be >= 1"):
            RepairConfig(max_attempts=0)

    def test_max_attempts_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must be >= 1"):
            RepairConfig(max_attempts=-5)

    def test_max_attempts_one_is_valid(self) -> None:
        c = RepairConfig(max_attempts=1)
        assert c.max_attempts == 1

    def test_max_attempts_large_is_valid(self) -> None:
        c = RepairConfig(max_attempts=100)
        assert c.max_attempts == 100

    # --- min_confidence_threshold validation ----------------------------------

    def test_confidence_threshold_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="min_confidence_threshold"):
            RepairConfig(min_confidence_threshold=0.0)

    def test_confidence_threshold_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="min_confidence_threshold"):
            RepairConfig(min_confidence_threshold=-0.1)

    def test_confidence_threshold_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="min_confidence_threshold"):
            RepairConfig(min_confidence_threshold=1.01)

    def test_confidence_threshold_exactly_one_is_valid(self) -> None:
        c = RepairConfig(min_confidence_threshold=1.0)
        assert c.min_confidence_threshold == 1.0

    def test_confidence_threshold_small_positive_is_valid(self) -> None:
        c = RepairConfig(min_confidence_threshold=0.01)
        assert c.min_confidence_threshold == 0.01

    # --- score_collision_margin validation ------------------------------------

    def test_collision_margin_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="score_collision_margin"):
            RepairConfig(score_collision_margin=0.0)

    def test_collision_margin_one_raises(self) -> None:
        with pytest.raises(ValueError, match="score_collision_margin"):
            RepairConfig(score_collision_margin=1.0)

    def test_collision_margin_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="score_collision_margin"):
            RepairConfig(score_collision_margin=-0.1)

    def test_collision_margin_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="score_collision_margin"):
            RepairConfig(score_collision_margin=1.1)

    def test_collision_margin_small_positive_is_valid(self) -> None:
        c = RepairConfig(score_collision_margin=0.01)
        assert c.score_collision_margin == 0.01

    def test_collision_margin_just_below_one_is_valid(self) -> None:
        c = RepairConfig(score_collision_margin=0.99)
        assert c.score_collision_margin == 0.99

    # --- Equality + repr ------------------------------------------------------

    def test_equality(self) -> None:
        assert RepairConfig() == RepairConfig()

    def test_inequality_on_max_attempts(self) -> None:
        assert RepairConfig(max_attempts=3) != RepairConfig(max_attempts=5)

    def test_inequality_on_threshold(self) -> None:
        assert (
            RepairConfig(min_confidence_threshold=0.7)
            != RepairConfig(min_confidence_threshold=0.8)
        )

    def test_repr_contains_class_name(self) -> None:
        assert "RepairConfig" in repr(RepairConfig())

    def test_repr_contains_max_attempts(self) -> None:
        assert "max_attempts" in repr(RepairConfig())

    # --- Mutability (non-frozen) ----------------------------------------------

    def test_fields_are_mutable(self) -> None:
        c = RepairConfig()
        c.max_attempts = 7
        assert c.max_attempts == 7

    def test_mutation_bypasses_post_init(self) -> None:
        """
        Direct field mutation skips __post_init__.
        This is a known dataclass limitation; callers should not mutate
        config objects to invalid states.
        """
        c = RepairConfig()
        # This assignment is invalid but dataclass does not prevent it.
        c.max_attempts = -1  # noqa: a valid mutation for test purposes
        assert c.max_attempts == -1  # documents the behaviour, not endorses it


# ---------------------------------------------------------------------------
# GuardConfig
# ---------------------------------------------------------------------------


class TestGuardConfig:

    # --- Defaults -------------------------------------------------------------

    def test_default_repair_is_repair_config(self) -> None:
        c = GuardConfig()
        assert isinstance(c.repair, RepairConfig)

    def test_default_repair_equals_default_repair_config(self) -> None:
        assert GuardConfig().repair == RepairConfig()

    def test_default_strict_mode_is_false(self) -> None:
        assert GuardConfig().strict_mode is False

    # --- Custom construction --------------------------------------------------

    def test_custom_repair_config(self) -> None:
        repair = RepairConfig(max_attempts=5)
        c = GuardConfig(repair=repair)
        assert c.repair.max_attempts == 5

    def test_strict_mode_true(self) -> None:
        c = GuardConfig(strict_mode=True)
        assert c.strict_mode is True

    def test_both_custom(self) -> None:
        repair = RepairConfig(allow_partial_repair=False)
        c = GuardConfig(repair=repair, strict_mode=True)
        assert c.repair.allow_partial_repair is False
        assert c.strict_mode is True

    # --- default_factory isolation --------------------------------------------

    def test_each_instance_owns_its_repair_config(self) -> None:
        """
        GuardConfig uses default_factory=RepairConfig, so each instance
        gets a distinct RepairConfig object.  Mutating one must not affect
        another.
        """
        c1 = GuardConfig()
        c2 = GuardConfig()
        c1.repair.max_attempts = 99
        assert c2.repair.max_attempts == 3

    def test_repair_configs_are_different_objects(self) -> None:
        c1 = GuardConfig()
        c2 = GuardConfig()
        assert c1.repair is not c2.repair

    # --- Equality + repr ------------------------------------------------------

    def test_equality(self) -> None:
        assert GuardConfig() == GuardConfig()

    def test_inequality_on_strict_mode(self) -> None:
        assert GuardConfig(strict_mode=False) != GuardConfig(strict_mode=True)

    def test_inequality_on_repair_config(self) -> None:
        assert (
            GuardConfig(repair=RepairConfig(max_attempts=1))
            != GuardConfig(repair=RepairConfig(max_attempts=3))
        )

    def test_repr_contains_class_name(self) -> None:
        assert "GuardConfig" in repr(GuardConfig())

    # --- Mutability -----------------------------------------------------------

    def test_strict_mode_is_mutable(self) -> None:
        c = GuardConfig()
        c.strict_mode = True
        assert c.strict_mode is True

    def test_repair_field_is_replaceable(self) -> None:
        c = GuardConfig()
        new_repair = RepairConfig(max_attempts=10)
        c.repair = new_repair
        assert c.repair.max_attempts == 10
