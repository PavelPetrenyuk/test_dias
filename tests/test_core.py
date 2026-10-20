import gzip
import json
from pathlib import Path

import pytest

from article_masks.core import (
    MaskGeneratorError,
    Settings,
    ValidationError,
    build_plan,
    generate,
    generate_articles,
    parse_masks_cell,
)


def write_csv(path: Path, rows: str) -> None:
    path.write_text(
        "manufacturer;part_group;masks\n" + rows,
        encoding="utf-8",
    )


def test_parses_several_masks_from_one_cell() -> None:
    assert parse_masks_cell(" ab**, AB-*\nA*-B. ") == [
        "AB**",
        "AB-*",
        "A*-B",
    ]


def test_normalizes_spaces_and_hyphens() -> None:
    assert parse_masks_cell("AB\u2011**,\u00a0CD*") == ["AB-**", "CD*"]


def test_generates_articles_in_order() -> None:
    articles = list(generate_articles("A*"))

    assert len(articles) == 36
    assert articles[:3] == ["A0", "A1", "A2"]
    assert articles[10:13] == ["AA", "AB", "AC"]
    assert articles[-1] == "AZ"


def test_preserves_hyphens() -> None:
    articles = generate_articles("A-*")
    assert [next(articles), next(articles)] == ["A-0", "A-1"]


def test_merges_rows_and_ignores_exact_duplicates(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    write_csv(source, 'Maker;Group;"AB*, CD*"\nMaker;Group;"AB*, EF*"\n')

    plan = build_plan(source)

    assert len(plan.masks) == 3
    assert plan.duplicate_masks == 1
    assert plan.expected_articles == 3 * 36


def test_rejects_overlapping_masks(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    write_csv(source, 'Maker;Group;"AB**, AB1*"\n')

    with pytest.raises(ValidationError, match="пересекаются"):
        build_plan(source)


def test_rejects_too_large_mask(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    write_csv(source, "Maker;Group;AB******\n")

    with pytest.raises(ValidationError, match="превышают лимит"):
        build_plan(source, Settings(max_mask_combinations=100_000_000))


def test_rejects_unquoted_column_delimiter(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    write_csv(source, "Maker;Group;AB*;CD*\n")

    with pytest.raises(ValidationError, match="слишком много полей"):
        build_plan(source)


def test_does_not_replace_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    write_csv(source, "Maker;Group;A*\n")
    output = tmp_path / "result"
    output.mkdir()

    with pytest.raises(MaskGeneratorError, match="уже существует"):
        generate(source, output)


def test_generates_shards_and_manifest(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    write_csv(source, 'Maker;Group;"A*, B-*"\n')
    output = tmp_path / "result"
    settings = Settings(shard_size=20, compression="gzip", buffer_records=7)

    manifest = generate(source, output, settings)

    assert manifest["expected_articles"] == 72
    assert manifest["generated_articles"] == 72
    assert json.loads((output / "manifest.json").read_text("utf-8")) == manifest

    shards = sorted((output / "articles").rglob("*.txt.gz"))
    assert len(shards) == 4
    with gzip.open(shards[0], "rt", encoding="ascii") as generated:
        assert generated.read().splitlines() == [
            "A0",
            "A1",
            "A2",
            "A3",
            "A4",
            "A5",
            "A6",
            "A7",
            "A8",
            "A9",
            "AA",
            "AB",
            "AC",
            "AD",
            "AE",
            "AF",
            "AG",
            "AH",
            "AI",
            "AJ",
        ]
