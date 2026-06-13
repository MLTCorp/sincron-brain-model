from sincron_brain.tags import normalize_tag, normalize_tags


def test_normalize_tag_uses_snake_case_ascii_and_simple_singular():
    assert normalize_tag("API Keys") == "api_key"
    assert normalize_tag("Env File") == "env_file"
    assert normalize_tag("Memórias") == "memoria"


def test_normalize_tags_deduplicates_plural_variants_and_limits():
    tags = normalize_tags(
        ["api_key", "api_keys", "env-file", "env file", "matheus_massari"],
        limit=3,
    )

    assert tags == ["api_key", "env_file", "matheus_massari"]
