#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Static W3C-widget validation for the Tizen app's config.xml.

This is the documented fallback CI runs when the real Tizen Studio CLI
(`tizen package`) cannot be installed headlessly on the runner (a ~1-2 GB,
license-gated download not designed for unattended CI — see
.github/workflows/ci-ott-apps.yml and tizen/README.md for exactly which
path a given run took). It is a "tizen package"-equivalent CONTRACT check,
not a substitute for a real packaging pass: it verifies config.xml is
well-formed and carries every element/attribute `tizen package` and the
Tizen TV runtime require, and that the files config.xml references
(content src, icon) actually exist. It cannot catch what only a real build
or on-device run would (e.g. actual .wgt signing, runtime API misuse).
"""

from __future__ import annotations

import sys
from pathlib import Path

from defusedxml import ElementTree

W3C_NS = "http://www.w3.org/ns/widgets"
TIZEN_NS = "http://tizen.org/ns/widgets"


def fail(problems: list[str]) -> int:
    print("tizen config.xml validation: FAIL")
    for p in problems:
        print(f"  - {p}")
    return 1


def main() -> int:
    root_dir = Path(__file__).resolve().parent
    config_path = root_dir / "config.xml"
    problems: list[str] = []

    if not config_path.is_file():
        return fail([f"{config_path} does not exist"])

    try:
        tree = ElementTree.parse(config_path)
    except ElementTree.ParseError as exc:
        return fail([f"config.xml is not well-formed XML: {exc}"])

    widget = tree.getroot()
    if widget is None:
        return fail(["config.xml has no root element"])

    if widget.tag != f"{{{W3C_NS}}}widget":
        problems.append(
            f"root element is {widget.tag!r}, expected {{{W3C_NS}}}widget "
            "(check the xmlns default namespace declaration)"
        )

    widget_id = widget.get("id")
    widget_version = widget.get("version")
    if not widget_id:
        problems.append("<widget> is missing required attribute 'id'")
    if not widget_version:
        problems.append("<widget> is missing required attribute 'version'")

    app = widget.find(f"{{{TIZEN_NS}}}application")
    app_id = app.get("id") if app is not None else None
    app_package = app.get("package") if app is not None else None
    if app is None:
        problems.append("missing required <tizen:application> element")
    else:
        for attr_name, attr_value in (
            ("id", app_id),
            ("package", app_package),
            ("required_version", app.get("required_version")),
        ):
            if not attr_value:
                problems.append(f"<tizen:application> is missing required attribute '{attr_name}'")
        package_id = app_id or ""
        if "." not in package_id:
            problems.append(
                f'<tizen:application id="{package_id}"> must be "<10-char-package-id>.<app-name>"'
            )
        else:
            pkg_prefix = package_id.split(".", 1)[0]
            if len(pkg_prefix) != 10 or not pkg_prefix.isalnum():
                problems.append(
                    f"<tizen:application> package-id prefix '{pkg_prefix}' must be "
                    "exactly 10 alphanumeric characters"
                )

    content = widget.find(f"{{{W3C_NS}}}content")
    content_src = content.get("src") if content is not None else None
    if not content_src:
        problems.append('missing required <content src="..."> element')
    else:
        content_path = root_dir / content_src
        if not content_path.is_file():
            problems.append(f'<content src="{content_src}"> does not exist: {content_path}')

    icon = widget.find(f"{{{W3C_NS}}}icon")
    icon_src = icon.get("src") if icon is not None else None
    if not icon_src:
        problems.append('missing required <icon src="..."> element')
    else:
        icon_path = root_dir / icon_src
        if not icon_path.is_file():
            problems.append(f'<icon src="{icon_src}"> does not exist: {icon_path}')

    profile = widget.find(f"{{{TIZEN_NS}}}profile")
    profile_name = profile.get("name") if profile is not None else None
    if profile_name != "tv":
        problems.append(
            'missing or incorrect <tizen:profile name="tv"> (this app targets Tizen TV)'
        )

    if problems:
        return fail(problems)

    print("tizen config.xml validation: PASS")
    print(f"  id={widget_id} version={widget_version}")
    print(f"  tizen:application id={app_id} package={app_package}")
    print(f"  content src={content_src} icon src={icon_src}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
