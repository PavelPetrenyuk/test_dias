from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from article_masks.core import (
    MaskGeneratorError,
    Settings,
    ValidationError,
    build_plan,
    generate,
)

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="article-masks",
        description="Генерация возможных артикулов из масок в CSV-файле.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan", help="Проверить CSV и рассчитать объём результата."
    )
    plan_parser.add_argument("input", type=Path)
    _add_limit_arguments(plan_parser)

    generate_parser = subparsers.add_parser(
        "generate", help="Проверить CSV и сформировать файлы с артикулами."
    )
    generate_parser.add_argument("input", type=Path)
    generate_parser.add_argument("--output", type=Path, required=True)
    generate_parser.add_argument("--shard-size", type=_positive_int, default=5_000_000)
    generate_parser.add_argument(
        "--compression", choices=("gzip", "none"), default="gzip"
    )
    generate_parser.add_argument("--buffer-records", type=_positive_int, default=10_000)
    _add_limit_arguments(generate_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    _configure_logging()
    settings = Settings(
        shard_size=getattr(arguments, "shard_size", 5_000_000),
        max_mask_combinations=arguments.max_mask_combinations,
        max_combinations=arguments.max_combinations,
        compression=getattr(arguments, "compression", "gzip"),
        buffer_records=getattr(arguments, "buffer_records", 10_000),
    )

    try:
        if arguments.command == "plan":
            logger.info("Проверяем файл %s", arguments.input)
            plan = build_plan(arguments.input, settings)
            _print_plan(plan)
        else:
            logger.info("Проверяем файл %s", arguments.input)
            manifest = generate(arguments.input, arguments.output, settings)
            logger.info(
                "Готово: создано %s артикулов",
                f"{manifest['generated_articles']:,}",
            )
            logger.info("Результат сохранён в %s", arguments.output)
    except ValidationError as exc:
        logger.error("Проверка входных данных завершилась с ошибками")
        for message in exc.messages:
            logger.error(message)
        return 2
    except MaskGeneratorError as exc:
        logger.error(str(exc))
        return 2

    return 0


def _add_limit_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-mask-combinations", type=_positive_int, default=100_000_000
    )
    parser.add_argument("--max-combinations", type=_positive_int, default=500_000_000)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("значение должно быть больше нуля")
    return parsed


def _configure_logging() -> None:
    logging.addLevelName(logging.INFO, "ИНФО")
    logging.addLevelName(logging.ERROR, "ОШИБКА")
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
    )


def _print_plan(plan) -> None:
    logger.info("План успешно построен")
    logger.info("Групп: %s", plan.group_count)
    logger.info("Масок: %s", len(plan.masks))
    logger.info("Пропущено точных дубликатов: %s", plan.duplicate_masks)
    logger.info("Будет создано артикулов: %s", f"{plan.expected_articles:,}")
    logger.info("Оценка несжатого размера: %s байт", f"{plan.estimated_size:,}")


if __name__ == "__main__":
    raise SystemExit(main())
