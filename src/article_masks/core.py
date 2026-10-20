from __future__ import annotations

import csv
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice, product
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
REQUIRED_COLUMNS = ("manufacturer", "part_group", "masks")
VALID_MASK = re.compile(r"^[A-Z0-9*-]+$")
MASK_SEPARATOR = re.compile(r",|\r?\n")
ASCII_UPPER = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
)
UNICODE_HYPHENS = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2212": "-",
    }
)


class MaskGeneratorError(Exception):
    ""


class ValidationError(MaskGeneratorError):
    #Набор ошибок, найденных до начала генерации.

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


@dataclass(frozen=True, slots=True)
class Settings:
    shard_size: int = 5_000_000
    max_mask_combinations: int = 100_000_000
    max_combinations: int = 500_000_000
    compression: str = "gzip"
    buffer_records: int = 10_000


@dataclass(frozen=True, slots=True)
class Mask:
    manufacturer: str
    part_group: str
    value: str
    source_row: int
    combinations: int


@dataclass(frozen=True, slots=True)
class Plan:
    source_path: Path
    masks: tuple[Mask, ...]
    duplicate_masks: int
    settings: Settings

    @property
    def group_count(self) -> int:
        return len({(mask.manufacturer, mask.part_group) for mask in self.masks})

    @property
    def expected_articles(self) -> int:
        return sum(mask.combinations for mask in self.masks)

    @property
    def estimated_size(self) -> int:
        return sum(mask.combinations * (len(mask.value) + 1) for mask in self.masks)


def parse_masks_cell(value: str) -> list[str]:
    #Разделяет одну CSV-ячейку на нормализованные маски.
    normalized = value.replace("\u00a0", " ").translate(UNICODE_HYPHENS).strip()
    if normalized.endswith("."):
        normalized = normalized[:-1].rstrip()

    return [
        item.strip().translate(ASCII_UPPER)
        for item in MASK_SEPARATOR.split(normalized)
        if item.strip()
    ]


def build_plan(input_path: Path, settings: Settings | None = None) -> Plan:
    #Читает весь CSV, проверяет маски и рассчитывает объём результата.
    selected_settings = settings or Settings()
    errors: list[str] = []
    masks: list[Mask] = []
    seen: set[tuple[str, str, str]] = set()
    duplicate_masks = 0

    try:
        source = input_path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise MaskGeneratorError(f"Не удалось открыть файл: {input_path}") from exc

    with source:
        reader = csv.DictReader(source, delimiter=";")
        if reader.fieldnames is None:
            raise MaskGeneratorError("В CSV отсутствует строка заголовков")

        columns = [column.strip() for column in reader.fieldnames if column]
        missing = [column for column in REQUIRED_COLUMNS if column not in columns]
        if missing:
            raise MaskGeneratorError(
                "В CSV отсутствуют обязательные колонки: " + ", ".join(missing)
            )
        reader.fieldnames = columns

        for row_number, row in enumerate(reader, start=2):
            if None in row:
                errors.append(
                    f"Строка {row_number}: слишком много полей; "
                    "значения с ';' необходимо заключать в кавычки"
                )
                continue

            manufacturer = (row.get("manufacturer") or "").strip()
            part_group = (row.get("part_group") or "").strip()
            masks_cell = (row.get("masks") or "").strip()

            if not manufacturer:
                errors.append(f"Строка {row_number}: не указан производитель")
            if not part_group:
                errors.append(f"Строка {row_number}: не указана группа запчастей")
            if not masks_cell:
                errors.append(f"Строка {row_number}: не указаны маски")
            if not manufacturer or not part_group or not masks_cell:
                continue

            parsed_masks = parse_masks_cell(masks_cell)
            if not parsed_masks:
                errors.append(f"Строка {row_number}: не найдено ни одной маски")
                continue

            for value in parsed_masks:
                error = validate_mask(value)
                if error:
                    errors.append(f"Строка {row_number}, маска {value!r}: {error}")
                    continue

                key = (manufacturer, part_group, value)
                if key in seen:
                    duplicate_masks += 1
                    continue
                seen.add(key)

                combinations = len(ALPHABET) ** value.count("*")
                if combinations > selected_settings.max_mask_combinations:
                    errors.append(
                        f"Строка {row_number}, маска {value!r}: "
                        f"{combinations:,} вариантов превышают лимит одной маски "
                        f"{selected_settings.max_mask_combinations:,}"
                    )
                    continue

                masks.append(
                    Mask(
                        manufacturer=manufacturer,
                        part_group=part_group,
                        value=value,
                        source_row=row_number,
                        combinations=combinations,
                    )
                )

    if not masks and not errors:
        errors.append("В CSV не найдено ни одной маски")

    for index, left in enumerate(masks):
        for right in masks[index + 1 :]:
            same_group = (
                left.manufacturer == right.manufacturer
                and left.part_group == right.part_group
            )
            if same_group and masks_overlap(left.value, right.value):
                errors.append(
                    f"Маски {left.value!r} и {right.value!r} пересекаются для "
                    f"{left.manufacturer!r} / {left.part_group!r}"
                )

    plan = Plan(
        source_path=input_path,
        masks=tuple(masks),
        duplicate_masks=duplicate_masks,
        settings=selected_settings,
    )
    if plan.expected_articles > selected_settings.max_combinations:
        errors.append(
            f"План содержит {plan.expected_articles:,} вариантов, что превышает "
            f"общий лимит {selected_settings.max_combinations:,}"
        )

    if errors:
        raise ValidationError(errors)
    return plan


def validate_mask(mask: str) -> str | None:
    #Возвращает текст ошибки или None, если маска корректна.
    if not VALID_MASK.fullmatch(mask):
        invalid = sorted(set(re.sub(r"[A-Z0-9*-]", "", mask)))
        return "найдены недопустимые символы: " + ", ".join(map(repr, invalid))
    if "*" not in mask:
        return "должна содержать хотя бы одну звёздочку"
    if not any(character.isascii() and character.isalnum() for character in mask):
        return "должна содержать хотя бы одну фиксированную букву или цифру"
    return None


def masks_overlap(left: str, right: str) -> bool:
    #Проверяет, могут ли две маски породить одинаковый артикул
    if len(left) != len(right):
        return False

    for left_character, right_character in zip(left, right, strict=True):
        if left_character == "*" or right_character == "*":
            fixed_character = (
                right_character if left_character == "*" else left_character
            )
            if fixed_character == "-":
                return False
        elif left_character != right_character:
            return False
    return True


def generate_articles(mask: str) -> Iterator[str]:
    #Лениво выдаёт все варианты одной маски
    choices = [ALPHABET if character == "*" else character for character in mask]
    for combination in product(*choices):
        yield "".join(combination)


def generate(
    input_path: Path,
    output_directory: Path,
    settings: Settings | None = None,
) -> dict[str, object]:
    #Проверяет входной файл и формирует полный набор выходных файлов.
    plan = build_plan(input_path, settings)
    if output_directory.exists():
        raise MaskGeneratorError(f"Выходной каталог уже существует: {output_directory}")
    if not output_directory.parent.exists():
        raise MaskGeneratorError(
            f"Родительский каталог не существует: {output_directory.parent}"
        )

    temporary_directory = output_directory.with_name(
        f".{output_directory.name}.{uuid.uuid4().hex}.partial"
    )
    temporary_directory.mkdir()

    try:
        _write_masks_csv(plan, temporary_directory / "masks.csv")
        _write_errors_csv(temporary_directory / "errors.csv")
        manifest = _generate_files(plan, temporary_directory)
        _write_json(manifest, temporary_directory / "manifest.json")
        os.replace(temporary_directory, output_directory)
    except Exception:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise

    return manifest


def _generate_files(plan: Plan, output_directory: Path) -> dict[str, object]:
    groups: dict[tuple[str, str], list[Mask]] = {}
    for mask in plan.masks:
        groups.setdefault((mask.manufacturer, mask.part_group), []).append(mask)

    manifest_groups: list[dict[str, object]] = []
    generated_total = 0

    for group_number, ((manufacturer, part_group), masks) in enumerate(
        groups.items(), start=1
    ):
        logger.info(
            "Обрабатываем группу «%s» производителя «%s»",
            part_group,
            manufacturer,
        )
        manifest_masks: list[dict[str, object]] = []

        for mask_number, mask in enumerate(masks, start=1):
            logger.info(
                "Генерируем маску %s: %s вариантов",
                mask.value,
                f"{mask.combinations:,}",
            )
            relative_directory = (
                Path("articles")
                / f"group-{group_number:04d}"
                / f"mask-{mask_number:04d}"
            )
            shards = _write_mask_files(
                mask,
                output_directory / relative_directory,
                relative_directory,
                plan.settings,
            )
            generated = sum(shard["article_count"] for shard in shards)
            if generated != mask.combinations:
                raise MaskGeneratorError(
                    f"Для маски {mask.value!r} создано {generated:,} строк "
                    f"вместо {mask.combinations:,}"
                )

            generated_total += generated
            manifest_masks.append(
                {
                    "mask": mask.value,
                    "expected_articles": mask.combinations,
                    "generated_articles": generated,
                    "shards": shards,
                }
            )

        manifest_groups.append(
            {
                "manufacturer": manufacturer,
                "part_group": part_group,
                "expected_articles": sum(mask.combinations for mask in masks),
                "masks": manifest_masks,
            }
        )

    if generated_total != plan.expected_articles:
        raise MaskGeneratorError(
            f"Создано {generated_total:,} строк вместо {plan.expected_articles:,}"
        )

    return {
        "status": "completed",
        "source_file": plan.source_path.name,
        "alphabet": ALPHABET,
        "wildcard": "*",
        "mask_count": len(plan.masks),
        "duplicate_masks_ignored": plan.duplicate_masks,
        "expected_articles": plan.expected_articles,
        "generated_articles": generated_total,
        "compression": plan.settings.compression,
        "groups": manifest_groups,
    }


def _write_mask_files(
    mask: Mask,
    output_directory: Path,
    relative_directory: Path,
    settings: Settings,
) -> list[dict[str, object]]:
    output_directory.mkdir(parents=True, exist_ok=True)
    articles = generate_articles(mask.value)
    written_total = 0
    part_number = 1
    shards: list[dict[str, object]] = []

    while written_total < mask.combinations:
        records_in_part = min(
            settings.shard_size,
            mask.combinations - written_total,
        )
        extension = ".txt.gz" if settings.compression == "gzip" else ".txt"
        filename = f"part-{part_number:05d}{extension}"
        final_path = output_directory / filename
        partial_path = final_path.with_name(filename + ".partial")
        digest = hashlib.sha256()
        part_count = 0
        uncompressed_bytes = 0

        try:
            with partial_path.open("wb") as raw_output:
                output = _open_output(raw_output, settings.compression)
                try:
                    while part_count < records_in_part:
                        batch_size = min(
                            settings.buffer_records,
                            records_in_part - part_count,
                        )
                        batch = list(islice(articles, batch_size))
                        if not batch:
                            break
                        chunk = ("\n".join(batch) + "\n").encode("ascii")
                        output.write(chunk)
                        digest.update(chunk)
                        part_count += len(batch)
                        uncompressed_bytes += len(chunk)
                finally:
                    if output is not raw_output:
                        output.close()
                raw_output.flush()
                os.fsync(raw_output.fileno())
            os.replace(partial_path, final_path)
        except OSError as exc:
            partial_path.unlink(missing_ok=True)
            raise MaskGeneratorError(f"Не удалось записать файл: {final_path}") from exc

        shards.append(
            {
                "path": (relative_directory / filename).as_posix(),
                "article_count": part_count,
                "uncompressed_bytes": uncompressed_bytes,
                "sha256_uncompressed": digest.hexdigest(),
            }
        )
        written_total += part_count
        part_number += 1

    return shards


def _open_output(raw_output: BinaryIO, compression: str) -> BinaryIO:
    if compression == "none":
        return raw_output
    if compression == "gzip":
        return gzip.GzipFile(
            filename="",
            fileobj=raw_output,
            mode="wb",
            compresslevel=1,
            mtime=0,
        )
    raise MaskGeneratorError(f"Неизвестный формат сжатия: {compression}")


def _write_masks_csv(plan: Plan, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, delimiter=";")
        writer.writerow(
            [
                "manufacturer",
                "part_group",
                "mask",
                "wildcards",
                "expected_articles",
                "source_row",
            ]
        )
        for mask in plan.masks:
            writer.writerow(
                [
                    mask.manufacturer,
                    mask.part_group,
                    mask.value,
                    mask.value.count("*"),
                    mask.combinations,
                    mask.source_row,
                ]
            )


def _write_errors_csv(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        csv.writer(output, delimiter=";").writerow(["row_number", "mask", "reason"])


def _write_json(data: dict[str, object], path: Path) -> None:
    partial_path = path.with_name(path.name + ".partial")
    with partial_path.open("w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial_path, path)
