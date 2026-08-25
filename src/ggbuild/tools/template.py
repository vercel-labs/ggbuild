# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from typing import Any

import string


class Template(string.Template):
    delimiter = "@@"


def format_template(tpltext: str, **kwargs: Any) -> str:
    template = Template(tpltext)
    return template.substitute(kwargs)
