# -*- coding: utf-8 -*-
"""Valida ou copia o projeto Android canônico sem regenerar fontes divergentes.

O código-fonte oficial vive em ``android_app_project``. Este utilitário deixou
de escrever strings de Kotlin/Manifest por cima do projeto e, quando recebe um
destino, cria uma cópia limpa para empacotamento.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


REQUIRED_FILES = (
    "settings.gradle",
    "build.gradle",
    "gradle.properties",
    "gradlew",
    "gradlew.bat",
    "gradle/wrapper/gradle-wrapper.properties",
    "gradle/wrapper/gradle-wrapper.jar",
    "app/build.gradle",
    "app/proguard-rules.pro",
    "app/src/main/AndroidManifest.xml",
    "app/src/main/java/br/com/casadocampo/logistica/MainActivity.kt",
)


def canonical_project() -> Path:
    return Path(__file__).resolve().parents[1] / "android_app_project"


def validate_android_project(project: Path | None = None) -> Path:
    source = (project or canonical_project()).resolve()
    missing = [relative for relative in REQUIRED_FILES if not (source / relative).is_file()]
    if missing:
        raise FileNotFoundError("Projeto Android incompleto; faltando: " + ", ".join(missing))
    duplicate_source = source / "src" / "main"
    if duplicate_source.exists() and any(duplicate_source.rglob("*.*")):
        raise RuntimeError("Fonte duplicada encontrada fora do módulo app/. Remova android_app_project/src.")
    return source


def generate_android_project(output_dir: Path | None = None) -> Path:
    """Mantém compatibilidade: valida o canônico ou cria uma cópia nova."""
    source = validate_android_project()
    if output_dir is None:
        print(f"[OK] Projeto Android canônico validado em: {source}")
        return source
    destination = output_dir.resolve()
    if destination == source:
        raise ValueError("O destino não pode ser o projeto canônico.")
    if destination.exists():
        raise FileExistsError(f"O destino já existe e não será sobrescrito: {destination}")
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("build", ".gradle", "local.properties", "*.apk", "*.aab"),
    )
    validate_android_project(destination)
    print(f"[OK] Cópia limpa do projeto criada em: {destination}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Pasta nova para receber uma cópia limpa do projeto")
    args = parser.parse_args()
    generate_android_project(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
