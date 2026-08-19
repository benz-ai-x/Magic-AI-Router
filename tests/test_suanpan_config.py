"""Tests for suanpan/config.py — schema defaults and YAML round-trip."""
import tempfile
import unittest

import yaml

from suanpan.config import AppConfig, ProviderConfig, dump_config, load_config


def _cfg_dict(**provider_kw):
    return {
        "providers": {
            "deepseek": {"base_url": "https://api.deepseek.com", **provider_kw},
        },
    }


def _round_trip(raw):
    """load_config → dump_config → load_config, via temp files."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.dump(raw, f)
        in_path = f.name
    cfg = load_config(in_path)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(dump_config(cfg))
        out_path = f.name
    return load_config(out_path)


class TestProviderModels(unittest.TestCase):
    def test_models_default_empty(self):
        p = ProviderConfig(base_url="https://api.deepseek.com")
        self.assertEqual(p.models, [])

    def test_models_round_trip_yaml(self):
        cfg = _round_trip(_cfg_dict(models=["deepseek-v4-flash", "deepseek-v4-pro"]))
        self.assertEqual(
            cfg.providers["deepseek"].models,
            ["deepseek-v4-flash", "deepseek-v4-pro"],
        )

    def test_route_target_validation_unaffected_by_models(self):
        ok = AppConfig.model_validate(
            {**_cfg_dict(models=["m1"]), "router": {"default": "deepseek/m1"}}
        )
        self.assertEqual(ok.router.default, "deepseek/m1")
        with self.assertRaises(Exception):
            AppConfig.model_validate(
                {**_cfg_dict(models=["m1"]), "router": {"default": "unknown/m1"}}
            )


class TestAnthropicNative(unittest.TestCase):
    """#5: providers that natively accept Anthropic bodies opt out of
    compatibility stripping (preserves cache_control for prompt caching)."""

    def test_default_false(self):
        p = ProviderConfig(base_url="https://api.deepseek.com")
        self.assertIs(p.anthropic_native, False)

    def test_accepts_true_and_round_trips_yaml(self):
        cfg = _round_trip(_cfg_dict(anthropic_native=True))
        self.assertIs(cfg.providers["deepseek"].anthropic_native, True)


class TestCommaTargetValidation(unittest.TestCase):
    """Regression: comma-separated targets like "glm,glm-4.6" must validate
    against the provider before the comma, not the whole string."""

    def test_comma_target_known_provider_accepted(self):
        ok = AppConfig.model_validate(
            {**_cfg_dict(), "router": {"default": "deepseek,m1"}}
        )
        self.assertEqual(ok.router.default, "deepseek,m1")

    def test_comma_target_unknown_provider_rejected(self):
        with self.assertRaises(Exception):
            AppConfig.model_validate(
                {**_cfg_dict(), "router": {"default": "ghost,m1"}}
            )

    def test_slash_target_takes_precedence_over_comma(self):
        ok = AppConfig.model_validate(
            {**_cfg_dict(), "router": {"default": "deepseek/m,y"}}
        )
        self.assertEqual(ok.router.default, "deepseek/m,y")


if __name__ == "__main__":
    unittest.main()
