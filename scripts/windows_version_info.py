"""Generate PyInstaller VERSIONINFO from the single version in pyproject.toml."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

PRODUCT_NAME = "TeslaParser"
COMPANY_NAME = "Misha20062006"
COPYRIGHT = "Copyright (c) 2026 Misha20062006"
KINDS = {
    "gui": (
        "TeslaParserGUI",
        "TeslaParserGUI.exe",
        "TeslaParser graphical interface",
    ),
    "cli": (
        "TeslaParser",
        "TeslaParser.exe",
        "TeslaParser command-line interface",
    ),
}


def project_version(pyproject_path: Path) -> str:
    with pyproject_path.open("rb") as source:
        data = tomllib.load(source)
    return str(data["project"]["version"])


def fixed_version(version: str) -> tuple[int, int, int, int]:
    parts = [int(part) for part in version.split(".")]
    if len(parts) > 4:
        raise ValueError(f"Unsupported Windows version: {version}")
    return tuple([*parts, *([0] * (4 - len(parts)))])  # type: ignore[return-value]


def render_version_info(version: str, *, kind: str) -> str:
    internal_name, original_filename, description = KINDS[kind]
    numeric = fixed_version(version)
    dotted_file_version = ".".join(str(part) for part in numeric)
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numeric!r},
    prodvers={numeric!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', '{COMPANY_NAME}'),
          StringStruct('FileDescription', '{description}'),
          StringStruct('FileVersion', '{dotted_file_version}'),
          StringStruct('InternalName', '{internal_name}'),
          StringStruct('LegalCopyright', '{COPYRIGHT}'),
          StringStruct('OriginalFilename', '{original_filename}'),
          StringStruct('ProductName', '{PRODUCT_NAME}'),
          StringStruct('ProductVersion', '{version}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def generate_version_info(
    pyproject_path: Path,
    output_path: Path,
    *,
    kind: str,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_version_info(project_version(pyproject_path), kind=kind),
        encoding="utf-8",
    )
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pyproject", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kind", choices=sorted(KINDS), required=True)
    arguments = parser.parse_args()
    generate_version_info(
        arguments.pyproject,
        arguments.output,
        kind=arguments.kind,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
